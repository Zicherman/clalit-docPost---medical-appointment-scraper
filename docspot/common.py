# -*- coding: utf-8 -*-
"""נתיבים, לוג, קובץ ההגדרות (env.txt) וקובץ המצב (state.json)."""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "browser_profile"
STATE_FILE = ROOT / "state.json"
LOG_FILE = ROOT / "watch.log"
ENV_FILES = [ROOT / ".env", ROOT / "env.txt"]  # הראשון שקיים

LOGIN_KEYS = ("CLALIT_ID", "CLALIT_CODE", "CLALIT_PASSWORD")
NOTIFY_KEYS = ("TG_TOKEN", "TG_CHAT_ID")
ALL_KEYS = LOGIN_KEYS + NOTIFY_KEYS

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


def parse_date(s):
    """תאריך בפורמט של האתר (31.12.2026)."""
    return datetime.strptime(s, "%d.%m.%Y").date()


# ------------------------------------------------------------ env.txt

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


# --------------------------------------------------------- state.json

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
