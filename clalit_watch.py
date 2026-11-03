# -*- coding: utf-8 -*-
"""
מעקב תורים מוקדמים בכללית -> התראה בטלגרם

הרצה:
    python clalit_watch.py                 # ריצה רציפה: בדיקה כל CHECK_EVERY_MIN דקות + פקודות בטלגרם
    python clalit_watch.py --once          # בדיקה אחת ויציאה (לבדיקה ראשונה)
    python clalit_watch.py --test-notify   # שולח הודעת בדיקה לטלגרם ויוצא (לא מתחבר לכללית)

ההגדרות נמצאות ב-config.py, פרטי ההתחברות ב-env.txt, והקוד עצמו בתיקייה docspot/.
הסקריפט רק קורא נתונים. הוא לא קובע, לא מעדכן ולא מבטל תורים.
"""

import argparse
import sys

from playwright.sync_api import sync_playwright

from docspot.clalit import Browser
from docspot.common import NOTIFY_KEYS, load_env, load_state
from docspot.telegram import notify
from docspot.watcher import Watcher


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
