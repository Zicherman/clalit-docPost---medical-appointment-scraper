# -*- coding: utf-8 -*-
"""
הלולאה הראשית במצב ריצה רציפה. בכל סבב:
  1. long polling לטלגרם (עד 30 שניות) וטיפול בפקודות.
  2. חזרה מהשהיה שהסתיימה, בדיקה מתוזמנת, או keep-alive לאתר.
הכל ב-thread אחד, כי Playwright sync לא תומך בכמה threads.
"""

import time
from datetime import date, datetime, timedelta

from config import ACTIVE_HOURS, CHECK_EVERY_MIN, KEEPALIVE_MIN
from .appointments import check_once
from .common import log, save_state
from .telegram import notify, tg_call

POLL_TIMEOUT_S = 30   # long polling: כמה זמן getUpdates מחכה להודעה חדשה
POLL_RETRY_S = 15     # המתנה אחרי כשלון של getUpdates (למשל אין אינטרנט)

HELP_TEXT = (
    "פקודות:\n"
    "/status - מצב המעקב\n"
    "/check - בדיקה עכשיו (גם בזמן השהיה)\n"
    "/pause - השהיה עד /resume\n"
    "/pause N - השהיה ל-N שעות\n"
    "/resume - ביטול ההשהיה"
)


# --------------------------------------------------------------- זמנים

def in_active_hours(t=None):
    h = (t or datetime.now()).hour
    return ACTIVE_HOURS[0] <= h < ACTIVE_HOURS[1]


def shift_to_active_hours(t):
    """אם t מחוץ לשעות הפעילות - מזיז אותו לתחילת חלון הפעילות הבא."""
    start, end = ACTIVE_HOURS
    if end - start >= 24 or in_active_hours(t):
        return t
    day = t if t.hour < start else t + timedelta(days=1)
    return day.replace(hour=start, minute=0, second=0, microsecond=0)


def fmt_time(t):
    if t is None:
        return "אין"
    return t.strftime("%H:%M") if t.date() == date.today() else t.strftime("%d.%m %H:%M")


def parse_ts(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


# --------------------------------------------------------------- Watcher

class Watcher:
    """מטפל בפקודות טלגרם, ומריץ בדיקות ו-keep-alive בזמן."""

    def __init__(self, browser, env, state):
        self.browser, self.env, self.state = browser, env, state
        last = parse_ts(state.get("last_check_time"))
        interval = timedelta(minutes=CHECK_EVERY_MIN)
        self.next_check = max(datetime.now(), last + interval) if last else datetime.now()
        self.last_activity = datetime.now()

    # ---- עזרים

    def reply(self, text):
        notify(self.env, text)

    def save(self):
        save_state(self.state)

    def pause_info(self):
        """(מושהה?, עד מתי). עד מתי = None כשההשהיה ללא הגבלה."""
        p = self.state.get("paused_until")
        if not p:
            return False, None
        if p == "forever":
            return True, None
        until = parse_ts(p)
        return (until is not None), until

    def next_check_at(self):
        """מתי הבדיקה המתוזמנת הבאה תרוץ בפועל. None = מושהה ללא הגבלה."""
        paused, until = self.pause_info()
        if paused and until is None:
            return None
        t = max(self.next_check, datetime.now())
        if until:
            t = max(t, until)
        return shift_to_active_hours(t)

    # ---- בדיקה

    def run_check(self):
        """בדיקה אחת. מחזיר (מוקדמים, חדשים), או זורק את השגיאה אחרי שנרשמה."""
        self.state["last_check_time"] = datetime.now().isoformat(timespec="seconds")
        try:
            result = check_once(self.browser, self.env, self.state)
            self.state["fail_count"] = 0
            self.state["last_check_error"] = None
            return result
        except Exception as e:
            self.state["fail_count"] = self.state.get("fail_count", 0) + 1
            self.state["last_check_error"] = str(e)[:200]
            log.exception("הבדיקה נכשלה (%d ברצף)", self.state["fail_count"])
            if self.state["fail_count"] == 3:
                notify(self.env, f"⚠️ מעקב התורים נכשל 3 פעמים ברצף. השגיאה האחרונה: {e}")
            raise
        finally:
            self.save()
            self.browser.back_to_background()
            self.next_check = datetime.now() + timedelta(minutes=CHECK_EVERY_MIN)
            self.last_activity = datetime.now()

    # ---- פקודות

    def poll_commands(self):
        """long polling ל-getUpdates. חוזר אחרי הודעה חדשה או אחרי POLL_TIMEOUT_S."""
        params = {"timeout": POLL_TIMEOUT_S, "allowed_updates": '["message"]'}
        if self.state.get("tg_offset"):
            params["offset"] = self.state["tg_offset"]
        try:
            resp = tg_call(self.env, "getUpdates", params, timeout=POLL_TIMEOUT_S + 15)
            if not resp.get("ok"):
                raise RuntimeError(resp.get("description"))
        except Exception as e:
            log.warning("getUpdates נכשל: %s. מנסה שוב בעוד %d שניות", e, POLL_RETRY_S)
            time.sleep(POLL_RETRY_S)
            return
        for upd in resp.get("result", []):
            # מקדמים את ה-offset לפני הטיפול, כדי שפקודה שנכשלה לא תחזור שוב ושוב
            self.state["tg_offset"] = upd["update_id"] + 1
            self.save()
            msg = upd.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(self.env["TG_CHAT_ID"]):
                log.warning("התעלמתי מהודעה מצ'אט לא מורשה")
                continue
            try:
                self.handle_command((msg.get("text") or "").strip())
            except Exception:
                log.exception("טיפול בפקודה נכשל")

    def handle_command(self, text):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else None
        log.info("פקודה מטלגרם: %s", text[:50])

        if cmd == "/pause":
            self.cmd_pause(arg)
        elif cmd == "/resume":
            self.cmd_resume()
        elif cmd == "/status":
            self.reply(self.status_text())
        elif cmd == "/check":
            self.cmd_check()
        else:
            self.reply(HELP_TEXT)

    def cmd_pause(self, arg):
        if arg is None:
            self.state["paused_until"] = "forever"
            self.save()
            self.reply("⏸️ המעקב מושהה עד שתשלח /resume.")
            return
        try:
            hours = float(arg)
            if not 0 < hours <= 24 * 365:
                raise ValueError
        except ValueError:
            self.reply("מספר השעות לא תקין. דוגמה: /pause 3")
            return
        until = datetime.now() + timedelta(hours=hours)
        self.state["paused_until"] = until.isoformat(timespec="seconds")
        self.save()
        self.reply(f"⏸️ המעקב מושהה עד {fmt_time(until)}. אחר כך הוא יחזור לבד.")

    def cmd_resume(self):
        if not self.pause_info()[0]:
            self.reply(f"המעקב כבר פעיל. הבדיקה הבאה: {fmt_time(self.next_check_at())}")
            return
        self.state["paused_until"] = None
        self.save()
        self.reply(f"▶️ המעקב חזר לפעול. הבדיקה הבאה: {fmt_time(self.next_check_at())}")

    def cmd_check(self):
        self.reply("🔎 מריץ בדיקה עכשיו...")
        try:
            n_better, n_new = self.run_check()
        except Exception as e:
            self.reply(f"⚠️ הבדיקה נכשלה: {str(e)[:200]}")
            return
        if n_better == 0:
            summary = "אין תור מוקדם מהתור שלך."
        elif n_new:
            summary = f"{n_better} תורים מוקדמים, מתוכם {n_new} חדשים (נשלחו בהודעה נפרדת)."
        else:
            summary = f"{n_better} תורים מוקדמים, כולם כבר דווחו לך קודם."
        current = self.state.get("last_current") or "לא נמצא תור עתידי בהתמחות הזו"
        self.reply(f"✅ הבדיקה הסתיימה. {summary}\n\nהתור הנוכחי שלך:\n{current}")

    def status_text(self):
        paused, until = self.pause_info()
        if not paused:
            mode = "▶️ פעיל"
        elif until is None:
            mode = "⏸️ מושהה עד /resume"
        else:
            mode = f"⏸️ מושהה עד {fmt_time(until)}"
        last = parse_ts(self.state.get("last_check_time"))
        last_str = fmt_time(last)
        if last and self.state.get("last_check_error"):
            last_str += " (נכשלה)"
        nxt = self.next_check_at()
        return "\n".join([
            f"מצב: {mode}",
            f"בדיקה אחרונה: {last_str}",
            f"התור שלך: {self.state.get('last_current') or 'לא ידוע / אין'}",
            f"בדיקה הבאה: {fmt_time(nxt) if nxt else 'אחרי /resume'}",
        ])

    # ---- תזמון

    def tick(self):
        """אחרי טיפול בפקודות: חזרה מהשהיה, בדיקה מתוזמנת, או keep-alive."""
        now = datetime.now()
        paused, until = self.pause_info()
        if paused and until and now >= until:
            self.state["paused_until"] = None
            self.save()
            paused = False
            log.info("ההשהיה הסתיימה")
            self.reply(f"▶️ ההשהיה הסתיימה, המעקב חזר לפעול. הבדיקה הבאה: {fmt_time(self.next_check_at())}")
        if paused or not in_active_hours(now):
            return
        if now >= self.next_check:
            try:
                self.run_check()
            except Exception:
                pass  # כבר נרשם ללוג ב-run_check
            return
        if now - self.last_activity >= timedelta(minutes=KEEPALIVE_MIN):
            # keep-alive שקט כדי שה-session לא יפוג בין בדיקות
            self.last_activity = now
            self.browser.keep_alive()

    def step(self):
        self.poll_commands()
        self.tick()

    def loop(self):
        log.info("המעקב פועל ומאזין לפקודות בטלגרם")
        while True:
            self.step()
