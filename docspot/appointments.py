# -*- coding: utf-8 -*-
"""הלוגיקה של התורים: חיפוש בכל היישובים, סינון, השוואה לתור שלך, ניסוח ההודעה, ובדיקה אחת."""

import time
from datetime import date, timedelta

from config import (ALERT_IF_NO_CURRENT_APPOINTMENT, CITIES, EXCLUDED_PLACES, MAX_SLOTS_IN_MESSAGE,
                    MIN_DAYS_AHEAD, PROFESSION_NAME, SECONDS_BETWEEN_CITIES)
from .clalit import ZIMUN_URL, read_current, search_city
from .common import log, parse_date
from .telegram import notify


def excluded_place(it):
    """מחזיר את המקום הפוסל שנמצא בתור, או None אם התור בסדר."""
    text = f'{it.get("clinic", "")} {it.get("address", "")}'
    return next((p for p in EXCLUDED_PLACES if p in text), None)


def search_all(fr, report=None):
    """מחפש בכל היישובים ומסנן מקומות פוסלים. report(city, status) - אופציונלי, לדיווח התקדמות.
    מחזיר {key: תור}, בלי כפילויות בין יישובים."""
    report = report or (lambda city, status: None)
    found = {}
    for city in CITIES:
        try:
            res = search_city(fr, city)
            if not res["header"]:
                log.warning("%s: היישוב לא זוהה באתר - בדוק את השם ברשימת CITIES", city)
                report(city, "היישוב לא זוהה באתר")
                continue
            log.info("%s: %s | %d ברשימה המוקדמת", city, res["header"], len(res["items"]))
            report(city, "OK")
            for it in res["items"]:
                if excluded_place(it):
                    continue
                it["specialty"] = PROFESSION_NAME
                key = f'{it["doctor"]}|{it["clinic"]}|{it["date"]}'
                found[key] = it
        except Exception as e:
            log.error("חיפוש ב-%s נכשל: %s", city, e)
            report(city, f"שגיאה: {e}")
        time.sleep(SECONDS_BETWEEN_CITIES)
    return found


def format_current(current):
    if not current:
        return None
    return f'{current["date"]} {current["time"]} – {PROFESSION_NAME} – {current["doctor"]}, {current["clinic"]}'


def find_better(found, current):
    """תורים מוקדמים מהתור הנוכחי, ממוינים מהמוקדם. רשימה של (key, item)."""
    earliest_ok = date.today() + timedelta(days=MIN_DAYS_AHEAD)
    limit = parse_date(current["date"]) if current else None
    if limit is None and not ALERT_IF_NO_CURRENT_APPOINTMENT:
        return []
    better = [
        (k, v) for k, v in found.items()
        if parse_date(v["date"]) >= earliest_ok and (limit is None or parse_date(v["date"]) < limit)
    ]
    better.sort(key=lambda kv: parse_date(kv[1]["date"]))
    return better if limit is not None else better[:1]


def build_message(items, cur_str):
    """הודעת ההתראה על תורים מוקדמים. items = רשימת תורים (dict)."""
    lines = [f"🩺 נמצא תור מוקדם יותר ל{PROFESSION_NAME}!"]
    lines.append(f"התור שלך: {cur_str}" if cur_str else "אין לך כרגע תור בהתמחות הזו.")
    lines.append("")
    for v in items[:MAX_SLOTS_IN_MESSAGE]:
        lines.append(f"📅 *{v['date']}* – {v.get('specialty', PROFESSION_NAME)} – {v['doctor']}")
        lines.append(f"   {v['clinic']}" + (f", {v['address']}" if v['address'] else ""))
        if v["phone"]:
            lines.append(f"   ☎️ {v['phone']}")
    if len(items) > MAX_SLOTS_IN_MESSAGE:
        lines.append(f"…ועוד {len(items) - MAX_SLOTS_IN_MESSAGE}")
    lines += ["", f"להזמנה: {ZIMUN_URL}"]
    return "\n".join(lines)


def check_once(browser, env, state):
    """בדיקה אחת מלאה: התחברות, קריאת התור שלך, חיפוש, ושליחת תורים חדשים בטלגרם.
    מחזיר (כמה תורים מוקדמים, כמה מהם חדשים)."""
    send = lambda t: notify(env, t)
    fr = browser.get_frame(env, send)

    current = read_current(fr)
    cur_str = format_current(current)
    log.info("התור הנוכחי: %s", cur_str or "אין")

    if state.get("last_current") and cur_str != state["last_current"]:
        send(f"ℹ️ זיהיתי שינוי בתור ל{PROFESSION_NAME}.\nעכשיו: {cur_str or 'אין תור'}\nממשיך לחפש תור מוקדם יותר.")
    state["last_current"] = cur_str

    better = find_better(search_all(fr), current)
    new = [(k, v) for k, v in better if k not in state["notified"]]
    log.info("נמצאו %d תורים מוקדמים יותר, מתוכם %d חדשים", len(better), len(new))
    if new and send(build_message([v for _, v in new], cur_str)):
        state["notified"].extend(k for k, _ in new)
    return len(better), len(new)
