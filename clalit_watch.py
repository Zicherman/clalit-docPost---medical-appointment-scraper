# -*- coding: utf-8 -*-
"""
מעקב תורים מוקדמים בכללית -> התראה בטלגרם

הרצה:
    python clalit_watch.py                 # ריצה רציפה (בדיקה כל CHECK_EVERY_MIN דקות)
    python clalit_watch.py --once          # בדיקה אחת ויציאה (לבדיקה ראשונה)
    python clalit_watch.py --test-notify   # שולח הודעת בדיקה לטלגרם ויוצא (לא מתחבר לכללית)

הסקריפט רק קורא נתונים. הוא לא קובע, לא מעדכן ולא מבטל תורים.
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# =====================================================================
# הגדרות - כאן ממלאים
# =====================================================================

# יישובי עוגן. החיפוש משתמש ב"כולל יישובים בסביבה", אז כל יישוב מכסה גם את סביבתו.
# הוסף/מחק שורות לפי הצורך. השם צריך להיות כמו שהוא מופיע באתר של כללית,
# בלי מקפים (למשל "תל אביב יפו" ולא "תל אביב - יפו", אחרת היישוב לא מזוהה).
CITIES = [
    "פתח תקווה",
    "תל אביב יפו",
    "ראשון לציון",
    "ראש העין",
    "לוד",
    "רמת גן",
    "רחובות",
    "נתניה",
    "מודיעין מכבים רעות",
    "אשדוד",
    "חדרה",
    "אריאל"
]

# מקומות פוסלים: תור שבשם המרפאה או בכתובת שלו מופיע אחד מאלה - מתעלמים ממנו.
# ההתאמה היא לפי חלק מהטקסט, כך ש"טייבה" תופס גם "טייבה - מרכז מקצועי".
EXCLUDED_PLACES = [
    "קלנסווה",
    "אום אל פחם",
    "טירה",
    "טירה צפון",
    "טייבה",
    "טייבה - מרכז",
    "כפר קאסם",
    "כפר קסם",
    "אלדיר",
    "דבוריה",
    "דבורייה",
    "מגדל העמק",
    "ג'ת",
]

# ההתמחות לחיפוש. נוירולוגיה = קבוצה 32, קוד 32 (נמצא במיפוי האתר).
SPECIALIZATION_GROUP = "32"
SPECIALIZATION_CODE = "32"
PROFESSION_NAME = "נוירולוגיה"  # כך מזהים את התור הקיים שלך ברשימת "התורים העתידיים"

CHECK_EVERY_MIN = 60          # כל כמה דקות לבדוק
ACTIVE_HOURS = (9, 18)        # לבדוק רק בין השעות האלה (שעון המחשב). (0, 24) = כל היממה
MIN_DAYS_AHEAD = 1            # להתעלם מתורים שקרובים מדי (1 = לא היום, מחר והלאה זה בסדר)
MAX_SLOTS_IN_MESSAGE = 6      # כמה תורים לכל היותר להציג בהודעה אחת
SECONDS_BETWEEN_CITIES = 3    # השהיה בין חיפושים, כדי לא להעמיס על האתר
KEEPALIVE_MIN = 10            # רענון שקט של החיבור בין בדיקות, כדי לא להתחבר מחדש כל שעה

# אם אין לך תור קיים בהתמחות הזו - להתריע על התור הכי מוקדם שנמצא? (True/False)
ALERT_IF_NO_CURRENT_APPOINTMENT = True

# =====================================================================
# מכאן והלאה אין צורך לשנות
# =====================================================================

BASE = "https://e-services.clalit.co.il"
LOGIN_URL = f"{BASE}/OnlineWeb/General/Login.aspx"
ZIMUN_URL = f"{BASE}/OnlineWeb/Services/Tamuz/TamuzTransfer.aspx"

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "browser_profile"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "watch.log"
ENV_FILES = [HERE / ".env", HERE / "env.txt"]  # הראשון שקיים
LOGIN_KEYS = ("CLALIT_ID", "CLALIT_CODE", "CLALIT_PASSWORD")
NOTIFY_KEYS = ("TG_TOKEN", "TG_CHAT_ID")
ALL_KEYS = LOGIN_KEYS + NOTIFY_KEYS

SEL_ID = "#ctl00_cphBody__loginView_tbUserId"
SEL_CODE = "#ctl00_cphBody__loginView_tbUserName"
SEL_PASS = "#ctl00_cphBody__loginView_tbPassword"
SEL_CAPTCHA = "#ctl00_cphBody__loginView_tbCaptchaLogin"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("clalit")


# ---------------------------------------------------------------- utils

def load_env(required=ALL_KEYS):
    env_file = next((f for f in ENV_FILES if f.exists()), None)
    if env_file is None:
        sys.exit(f"חסר קובץ הגדרות ({' או '.join(f.name for f in ENV_FILES)}).")
    env = {}
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in required if not env.get(k)]
    if missing:
        sys.exit(f"חסרים ב-{env_file.name}: {', '.join(missing)}")
    return env


def excluded_place(it):
    """מחזיר את המקום הפוסל שנמצא בתור, או None אם התור בסדר."""
    text = f'{it.get("clinic", "")} {it.get("address", "")}'
    return next((p for p in EXCLUDED_PLACES if p in text), None)


def parse_date(s):
    return datetime.strptime(s, "%d.%m.%Y").date()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"notified": [], "last_current": None, "fail_count": 0}


def save_state(st):
    st["notified"] = st["notified"][-500:]
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def notify(env, text):
    """שולח הודעה בטלגרם. מחזיר True אם הטלגרם אישר (ok=true)."""
    url = f"https://api.telegram.org/bot{env['TG_TOKEN']}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": env["TG_CHAT_ID"], "text": text.replace("*", "")}).encode()
    try:
        try:
            with urllib.request.urlopen(url, data=data, timeout=30) as r:
                body = r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:  # טלגרם מחזיר JSON עם הסבר גם בשגיאות
            body = e.read().decode("utf-8", "ignore")
        resp = json.loads(body)
        if resp.get("ok"):
            log.info("Telegram נשלח")
            return True
        log.error("Telegram נכשל: %s", resp.get("description", body[:200]))
        return False
    except Exception as e:
        # בלי להדפיס את ה-URL, כי הטוקן נמצא בו
        log.error("Telegram נכשל: %s", type(e).__name__)
        return False


# ------------------------------------------------- JS שרץ בתוך עמוד הזימון

SEARCH_JS = r"""
async ({group, code, city}) => {
  const H = {'X-Requested-With': 'XMLHttpRequest'};
  const toDoc = async r => {
    const t = await r.text(); let h = t;
    try { h = JSON.parse(t).data } catch (e) {}
    return new DOMParser().parseFromString(h || '', 'text/html');
  };
  const body = new URLSearchParams({
    SelectedGroupCode: group, SelectedSpecializationCode: code,
    SelectedCityName: city, IsSearchDiariesByDistricts: 'true', SelectedDoctorName: ''
  });
  const r1 = await fetch('/Zimunet/Diary/SearchDiaries', {method: 'POST', credentials: 'include',
    headers: {...H, 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, body});
  if (!r1.ok) throw new Error('search HTTP ' + r1.status);
  const d1 = await toDoc(r1);
  const header = (d1.querySelector('h2')?.textContent || '').replace(/\s+/g, ' ').trim();
  // שם יישוב לא מוכר: האתר לא מחזיר כותרת, והמיון היה מחזיר את תוצאות החיפוש הקודם
  if (!header) return {header, items: []};
  // מיון לפי "תור ראשון פנוי" - העמוד הראשון מכיל את הכי מוקדמים
  const r2 = await fetch('/Zimunet/Diary/SortDiaries?sortValue=2&sortDirection=1', {credentials: 'include', headers: H});
  const d = r2.ok ? await toDoc(r2) : d1;
  const items = [...d.querySelectorAll('li.diary')].map(li => {
    const det = [...li.querySelectorAll('.clinicDetails')].map(e => e.textContent.replace(/\s+/g, ' ').trim());
    return {
      doctor: (li.querySelector('.doctorName')?.textContent || '').trim(),
      date: (li.querySelector('span.visitDateTime')?.textContent || '').trim(),
      clinic: (li.querySelector('a.clinicDetails')?.textContent || '').trim(),
      address: (det.find(t => t.startsWith('כתובת')) || '').replace(/^כתובת:\s*/, ''),
      phone: (li.querySelector('.clinicTelephone')?.textContent || '').trim().replace(/\s+/g, ', '),
    };
  }).filter(x => x.date);
  return {header, items};
}
"""

CURRENT_JS = r"""
async (profession) => {
  const t = await fetch('/Zimunet/', {credentials: 'include'}).then(r => r.text());
  if (!t.includes('newVisitTypeMenu') && !t.includes('futureVisitsSection')) return null;  // לא מחובר
  const d = new DOMParser().parseFromString(t, 'text/html');
  return [...d.querySelectorAll('#futureVisitsSection li.visit')]
    .map(li => li.textContent.replace(/\s+/g, ' ').trim())
    .filter(s => s.includes(profession));
}
"""

KEEPALIVE_JS = "async () => (await fetch('/OnlineWeb/Services/tamuz/SyncSession.aspx', {credentials: 'include'})).status"


# ------------------------------------------------------------- התחברות

class NeedManualLogin(Exception):
    pass


class CaptchaRequired(Exception):
    """האתר מבקש CAPTCHA והדפדפן רץ ברקע - צריך לפתוח חלון גלוי."""


def find_zimunet_frame(page, timeout_s=40):
    end = time.time() + timeout_s
    while time.time() < end:
        for fr in page.frames:
            if "/Zimunet" in fr.url:
                return fr
        if "Login" in page.url:
            return None
        page.wait_for_timeout(1000)
    return None


def login(page, env, notify, interactive):
    log.info("מתחבר לכללית...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_selector(SEL_ID, timeout=30000)

    if page.locator(SEL_CAPTCHA).is_visible():
        if not interactive:
            raise CaptchaRequired()
        # לא פותרים CAPTCHA אוטומטית. מבקשים ממך להתחבר ידנית בחלון שנפתח.
        log.warning("האתר מבקש CAPTCHA - נדרשת התחברות ידנית")
        notify("🔐 מעקב התורים: כללית מבקשת התחברות ידנית (CAPTCHA). "
               "התחבר בחלון הדפדפן של הסקריפט במחשב, והמעקב ימשיך לבד.")
        page.fill(SEL_ID, env["CLALIT_ID"])
        page.fill(SEL_CODE, env["CLALIT_CODE"])
        page.fill(SEL_PASS, env["CLALIT_PASSWORD"])
        page.bring_to_front()
        # מחכים עד 30 דקות שתסיים
        end = time.time() + 30 * 60
        while time.time() < end and "Login" in page.url:
            page.wait_for_timeout(5000)
        if "Login" in page.url:
            raise NeedManualLogin("לא בוצעה התחברות ידנית בזמן")
        return

    page.fill(SEL_ID, env["CLALIT_ID"])
    page.fill(SEL_CODE, env["CLALIT_CODE"])
    page.fill(SEL_PASS, env["CLALIT_PASSWORD"])
    page.press(SEL_PASS, "Enter")
    try:
        page.wait_for_url(lambda u: "Login" not in u, timeout=45000)
    except PWTimeout:
        if page.locator(SEL_CAPTCHA).is_visible():
            return login(page, env, notify, interactive)  # יעבור למסלול הידני
        raise RuntimeError("ההתחברות נכשלה - בדוק ת\"ז/קוד/סיסמה ב-env.txt")
    log.info("התחברות הצליחה")


def get_frame(page, env, notify, interactive=True):
    """מחזיר את ה-frame של מערכת הזימון, ומתחבר אם צריך."""
    for _ in range(2):
        page.goto(ZIMUN_URL, wait_until="domcontentloaded")
        fr = find_zimunet_frame(page)
        if fr is not None:
            try:
                fr.wait_for_load_state("domcontentloaded", timeout=30000)
            except PWTimeout:
                pass
            return fr
        login(page, env, notify, interactive)
    raise RuntimeError("לא הצלחתי להגיע לעמוד זימון התורים")


class Browser:
    """דפדפן שרץ ברקע. אם האתר מבקש CAPTCHA, נפתח זמנית חלון גלוי להתחברות ידנית."""

    def __init__(self, p, show=False):
        self.p, self.show, self.ctx = p, show, None
        self._open(visible=show)

    def _open(self, visible):
        self.close()
        self.ctx = self.p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=not visible, locale="he-IL",
            viewport={"width": 1200, "height": 900},
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.visible = visible

    def get_frame(self, env, notify):
        try:
            return get_frame(self.page, env, notify, interactive=self.visible)
        except CaptchaRequired:
            log.warning("האתר מבקש CAPTCHA - פותח חלון דפדפן להתחברות ידנית")
            self._open(visible=True)
            return get_frame(self.page, env, notify, interactive=True)

    def back_to_background(self):
        """אחרי התחברות ידנית - חוזרים לריצה ברקע."""
        if self.visible and not self.show:
            self._open(visible=False)

    def close(self):
        if self.ctx is not None:
            try:
                self.ctx.close()
            except Exception:
                pass
            self.ctx = None


# ----------------------------------------------------------------- בדיקה

def read_current(fr):
    rows = fr.evaluate(CURRENT_JS, PROFESSION_NAME)
    if rows is None:
        raise RuntimeError("נראה שה-session פג")
    appts = []
    for s in rows:
        m = re.search(r"בתאריך (\d\d\.\d\d\.\d{4})(?: בשעה (\d\d:\d\d))?", s)
        if not m:
            continue
        doctor = s.split(PROFESSION_NAME)[0].strip()
        clinic = re.search(r"מרפאה: (.*?) כתובת", s)
        appts.append({"date": m.group(1), "time": m.group(2) or "", "doctor": doctor,
                      "clinic": clinic.group(1).strip() if clinic else ""})
    appts.sort(key=lambda a: parse_date(a["date"]))
    return appts[0] if appts else None


def search_all(fr, report=None):
    """מחפש בכל היישובים. report(city, status) - אופציונלי, לדיווח התקדמות."""
    report = report or (lambda city, status: None)
    found = {}
    for city in CITIES:
        try:
            res = fr.evaluate(SEARCH_JS, {"group": SPECIALIZATION_GROUP, "code": SPECIALIZATION_CODE, "city": city})
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
    if not new:
        return

    if send(build_message([v for _, v in new], cur_str)):
        state["notified"].extend(k for k, _ in new)


# ------------------------------------------------------------------ main

def in_active_hours():
    h = datetime.now().hour
    return ACTIVE_HOURS[0] <= h < ACTIVE_HOURS[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="בדיקה אחת ויציאה")
    ap.add_argument("--test-notify", action="store_true", help="שליחת הודעת בדיקה לטלגרם (לא מתחבר לכללית)")
    ap.add_argument("--show", action="store_true",
                    help="להציג את חלון הדפדפן (ברירת המחדל: ברקע, חלון נפתח רק אם צריך CAPTCHA)")
    args = ap.parse_args()

    if args.test_notify:
        env = load_env(required=NOTIFY_KEYS)
        ok = notify(env, "✅ בדיקה ממעקב התורים של כללית - הטלגרם עובד.")
        sys.exit(0 if ok else 1)

    env = load_env()

    state = load_state()
    with sync_playwright() as p:
        browser = Browser(p, show=args.show)
        try:
            next_check = time.time()
            while True:
                if time.time() >= next_check:
                    if in_active_hours() or args.once:
                        try:
                            check_once(browser, env, state)
                            state["fail_count"] = 0
                        except Exception as e:
                            state["fail_count"] = state.get("fail_count", 0) + 1
                            log.exception("הבדיקה נכשלה (%d ברצף)", state["fail_count"])
                            if state["fail_count"] == 3:
                                notify(env, f"⚠️ מעקב התורים נכשל 3 פעמים ברצף. השגיאה האחרונה: {e}")
                        save_state(state)
                        browser.back_to_background()
                    else:
                        log.info("מחוץ לשעות הפעילות")
                    if args.once:
                        break
                    next_check = time.time() + CHECK_EVERY_MIN * 60
                # keep-alive שקט כדי שה-session לא יפוג בין בדיקות
                time.sleep(KEEPALIVE_MIN * 60 if next_check - time.time() > KEEPALIVE_MIN * 60 else max(5, next_check - time.time()))
                try:
                    if "e-services.clalit.co.il" in browser.page.url:
                        browser.page.evaluate(KEEPALIVE_JS)
                except Exception:
                    pass
        finally:
            browser.close()


if __name__ == "__main__":
    main()
