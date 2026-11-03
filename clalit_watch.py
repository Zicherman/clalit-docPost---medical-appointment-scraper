# -*- coding: utf-8 -*-
"""
מעקב תורים מוקדמים בכללית -> התראה בטלגרם

הרצה:
    python clalit_watch.py                 # ריצה רציפה: בדיקה כל CHECK_EVERY_MIN דקות + פקודות בטלגרם
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


STATE_DEFAULTS = {
    "notified": [],            # תורים שכבר דווחו
    "last_current": None,      # התור הנוכחי שלך, מהבדיקה האחרונה
    "fail_count": 0,           # כשלונות ברצף
    "tg_offset": None,         # העדכון הבא לקריאה מטלגרם
    "paused_until": None,      # None = פעיל, "forever" = עד /resume, אחרת זמן ISO
    "last_check_time": None,   # זמן ISO של הבדיקה האחרונה
    "last_check_error": None,  # השגיאה של הבדיקה האחרונה, אם נכשלה
}


def load_state():
    st = {}
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    for k, v in STATE_DEFAULTS.items():
        st.setdefault(k, list(v) if isinstance(v, list) else v)
    return st


def save_state(st):
    st["notified"] = st["notified"][-500:]
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def tg_call(env, method, params, timeout=30):
    """קריאה ל-Telegram Bot API. מחזיר את תשובת ה-JSON. בשגיאה, ההודעה לא כוללת את הטוקן."""
    url = f"https://api.telegram.org/bot{env['TG_TOKEN']}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        try:
            with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:  # טלגרם מחזיר JSON עם הסבר גם בשגיאות
            body = e.read()
        return json.loads(body.decode("utf-8", "ignore"))
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}".replace(env["TG_TOKEN"], "***")) from None


def notify(env, text):
    """שולח הודעה בטלגרם. מחזיר True אם הטלגרם אישר (ok=true)."""
    try:
        resp = tg_call(env, "sendMessage", {"chat_id": env["TG_CHAT_ID"], "text": text.replace("*", "")})
    except Exception as e:
        log.error("Telegram נכשל: %s", e)
        return False
    if resp.get("ok"):
        log.info("Telegram נשלח")
        return True
    log.error("Telegram נכשל: %s", resp.get("description"))
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
    if new and send(build_message([v for _, v in new], cur_str)):
        state["notified"].extend(k for k, _ in new)
    return len(better), len(new)


# ------------------------------------------------------------------ main

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


class Watcher:
    """הלולאה הראשית: מטפל בפקודות טלגרם, ומריץ בדיקות ו-keep-alive בזמן. הכל ב-thread אחד."""

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
        now = datetime.now()
        self.state["last_check_time"] = now.isoformat(timespec="seconds")
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

        elif cmd == "/resume":
            if not self.pause_info()[0]:
                self.reply(f"המעקב כבר פעיל. הבדיקה הבאה: {fmt_time(self.next_check_at())}")
                return
            self.state["paused_until"] = None
            self.save()
            self.reply(f"▶️ המעקב חזר לפעול. הבדיקה הבאה: {fmt_time(self.next_check_at())}")

        elif cmd == "/status":
            self.reply(self.status_text())

        elif cmd == "/check":
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

        else:
            self.reply(HELP_TEXT)

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
            try:
                if "e-services.clalit.co.il" in self.browser.page.url:
                    self.browser.page.evaluate(KEEPALIVE_JS)
            except Exception:
                pass

    def step(self):
        self.poll_commands()
        self.tick()

    def loop(self):
        log.info("המעקב פועל ומאזין לפקודות בטלגרם")
        while True:
            self.step()


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
            watcher = Watcher(browser, env, state)
            if args.once:
                # בדיקה אחת בלי להאזין לפקודות (ובלי להתחשב בהשהיה או בשעות הפעילות)
                try:
                    watcher.run_check()
                except Exception:
                    pass  # כבר נרשם ללוג
            else:
                watcher.loop()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
