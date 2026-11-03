# -*- coding: utf-8 -*-
"""
כל מה שמדבר עם האתר של כללית: הדפדפן, ההתחברות, קריאת התור הקיים וחיפוש תורים ביישוב.
הקוד הזה רק קורא נתונים. הוא לא קובע, לא מעדכן ולא מבטל תורים.
"""

import re
import time

from playwright.sync_api import TimeoutError as PWTimeout

from config import PROFESSION_NAME, SPECIALIZATION_CODE, SPECIALIZATION_GROUP
from .common import PROFILE_DIR, log, parse_date

BASE = "https://e-services.clalit.co.il"
LOGIN_URL = f"{BASE}/OnlineWeb/General/Login.aspx"
ZIMUN_URL = f"{BASE}/OnlineWeb/Services/Tamuz/TamuzTransfer.aspx"

SEL_ID = "#ctl00_cphBody__loginView_tbUserId"
SEL_CODE = "#ctl00_cphBody__loginView_tbUserName"
SEL_PASS = "#ctl00_cphBody__loginView_tbPassword"
SEL_CAPTCHA = "#ctl00_cphBody__loginView_tbCaptchaLogin"


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

    def keep_alive(self):
        """רענון שקט של ה-session, אם הדפדפן נמצא באתר של כללית."""
        try:
            if "e-services.clalit.co.il" in self.page.url:
                self.page.evaluate(KEEPALIVE_JS)
        except Exception:
            pass

    def close(self):
        if self.ctx is not None:
            try:
                self.ctx.close()
            except Exception:
                pass
            self.ctx = None


# ----------------------------------------------------------- קריאת נתונים

def read_current(fr):
    """התור העתידי הקרוב שלך בהתמחות PROFESSION_NAME, או None."""
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


def search_city(fr, city):
    """חיפוש ביישוב אחד (כולל סביבה), ממוין מהתור הקרוב.
    מחזיר {"header": ..., "items": [...]}. header ריק = היישוב לא זוהה באתר."""
    return fr.evaluate(SEARCH_JS, {"group": SPECIALIZATION_GROUP, "code": SPECIALIZATION_CODE, "city": city})
