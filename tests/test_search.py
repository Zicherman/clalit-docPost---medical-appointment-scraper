# -*- coding: utf-8 -*-
"""
בדיקה חד-פעמית: מתחבר לכללית, קורא את התור הנוכחי, מחפש בכל היישובים,
ומציג את ההודעה שהתוכנית האמיתית הייתה שולחת. הכל מודפס למסך ולקובץ test_results.txt.

לא שולח טלגרם, לא שומר state, ולא קובע/מבטל שום תור.
צריך רק CLALIT_ID / CLALIT_CODE / CLALIT_PASSWORD בקובץ ההגדרות.

הרצה:  tests\test_search.bat   (או: python tests/test_search.py)
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # כדי למצוא את config ואת docspot
import config  # noqa: E402
from docspot.appointments import build_message, find_better, format_current, search_all  # noqa: E402
from docspot.clalit import Browser, read_current  # noqa: E402
from docspot.common import LOGIN_KEYS, load_env  # noqa: E402

OUT_FILE = Path(__file__).resolve().parent / "test_results.txt"
lines_out = []


def out(s=""):
    print(s)
    lines_out.append(s)


def main():
    env = load_env(required=LOGIN_KEYS)
    notify = lambda t: out(f"[הודעה שהייתה נשלחת בטלגרם]\n{t}\n")

    with sync_playwright() as p:
        browser = Browser(p)  # ברקע, כמו בתוכנית האמיתית
        try:
            out("############################################################")
            out("# חלק א' - בדיקות (חלק מהטסט בלבד, לא נשלח בתוכנית האמיתית)")
            out("############################################################")
            out()
            out("התחברות ומעבר לזימון תורים:")
            fr = browser.get_frame(env, notify)
            out(f"  OK  ({fr.url})")

            current = read_current(fr)
            out("התור הנוכחי שלך:")
            out(f"  {format_current(current) or f'לא נמצא תור עתידי ב{config.PROFESSION_NAME}'}")

            out("חיפוש לפי יישוב:")
            found = search_all(fr, report=lambda city, status: out(f"  {city}: {status}"))
            better = [v for _, v in find_better(found, current)]
            out()

            out("############################################################")
            out("# חלק ב' - הפלט הסופי של התוכנית")
            out("############################################################")
            out("זה מה שהתוכנית האמיתית (clalit_watch.py) שולחת לך בטלגרם.")
            out("זה הפלט היחיד שלה - חוץ ממנו נשלחות רק הודעות תקלה")
            out("(CAPTCHA, 3 כשלונות ברצף, או שינוי בתור הקיים שלך).")
            out(f"בדיקה כל {config.CHECK_EVERY_MIN} דקות בין {config.ACTIVE_HOURS[0]}:00 ל-{config.ACTIVE_HOURS[1]}:00,")
            out("וכל תור נשלח רק פעם אחת (בהרצות הבאות יישלחו רק תורים חדשים).")
            out("------------------------------------------------------------")
            if better:
                out(build_message(better, format_current(current)))
            else:
                out("(לא נמצאו תורים מוקדמים יותר - לא תישלח הודעה)")
            out("------------------------------------------------------------")
        except Exception as e:
            out(f"\n!!! הבדיקה נכשלה: {e}")
            out("שלח את test_results.txt ואת watch.log")
        finally:
            browser.close()
            OUT_FILE.write_text("\n".join(lines_out), encoding="utf-8-sig")
            print(f"\nהתוצאות נשמרו גם ב-{OUT_FILE.name} (נוח יותר לקרוא בעברית בפנקס רשימות)")
            input("לחץ Enter כדי לסגור...")


if __name__ == "__main__":
    main()
