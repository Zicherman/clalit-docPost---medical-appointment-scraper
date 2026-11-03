# -*- coding: utf-8 -*-
"""שליחת הודעות וקריאת עדכונים מ-Telegram Bot API."""

import json
import urllib.error
import urllib.parse
import urllib.request

from .common import log


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
