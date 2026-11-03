"""
docspot - the code behind the Clalit appointment watcher.

The entry point is clalit_watch.py (in the project root). Your settings live in config.py,
and your login details and Telegram token live in env.txt. This folder holds code only;
you don't need to edit anything here.

The code is split into five files by topic. Each file only uses the files listed before it:

    common.py  ->  telegram.py  ->  clalit.py  ->  appointments.py  ->  watcher.py


common.py - shared basics
-------------------------
Things every other file needs, with nothing specific to Clalit or Telegram:
  - Paths: where env.txt, state.json, watch.log and the browser_profile folder live.
  - Logging: every message goes both to the screen and to watch.log.
  - load_env: reads env.txt (ID, user code, password, Telegram token) and stops with a
    clear message if anything is missing.
  - load_state / save_state: read and write state.json, the bot's "memory" between runs:
      notified          - appointments already reported to you, so none is sent twice
      last_current      - your current appointment, as of the last check
      paused_until      - whether watching is paused, and until when
      last_check_time   - when the last check ran
      tg_offset         - which Telegram messages the bot has already read
      fail_count        - how many checks in a row have failed
  - parse_date: turns a date in the site's format (31.12.2026) into a comparable date.


telegram.py - talking to Telegram
---------------------------------
  - tg_call: the basic function that sends a request to the Telegram Bot API and returns
    the response. If something fails, the error message never includes the token, so the
    token can't end up in the log.
  - notify: sends you a message. Strips asterisks (Telegram doesn't need them), and returns
    True only if Telegram confirmed the message was delivered.
Receiving commands (getUpdates) happens in watcher.py, through tg_call.


clalit.py - everything that talks to the Clalit website
-------------------------------------------------------
This is the only file that touches the site. It only reads data: there is no code here
that books, changes or cancels an appointment.
  - Browser: manages the browser (Chromium via Playwright). It runs in the background,
    with no window. The profile is kept in browser_profile, so the site recognizes the
    browser between runs.
    If the site asks for a CAPTCHA, Browser temporarily opens a visible window so you can
    log in by hand, then goes back to the background after the check (back_to_background).
    keep_alive sends a small request to the site between checks so the login doesn't expire.
  - login: logs in with ID, user code and password. If a CAPTCHA appears it does not try to
    solve it: it messages you, fills in your details, and waits up to 30 minutes for you to
    finish.
  - get_frame: opens the appointment booking page, logging in first if needed. Clalit's
    booking system loads inside an iframe, and all searches run from inside it.
  - read_current: reads your "upcoming appointments" list and returns the nearest one in the
    specialty set in config.py.
  - search_city: searches for open appointments in one city (including nearby towns),
    sorted by earliest date. If the site doesn't recognize the city name, it returns an
    empty result.
  - SEARCH_JS / CURRENT_JS / KEEPALIVE_JS: JavaScript that runs inside the site's page.
    It sends the same requests the browser sends when you search by hand, and reads the
    results from the HTML.


appointments.py - the appointment logic
---------------------------------------
This is where it's decided which appointments matter to you and what to send. Nothing here
depends on how the site is built.
  - search_all: goes through every city in config.py, with a short pause between searches
    to avoid loading the site. Filters out excluded places and merges duplicates (the same
    doctor showing up under several cities).
  - excluded_place: checks whether the clinic name or address contains a place from
    EXCLUDED_PLACES.
  - find_better: keeps only appointments earlier than yours and not too soon
    (MIN_DAYS_AHEAD), sorted from the earliest.
  - build_message: writes the alert message (date, doctor, clinic, address, phone,
    booking link).
  - check_once: one full check, start to finish:
      1. open the site (logging in if needed)
      2. read your current appointment; if it changed since the last check, send a message
      3. search every city
      4. filter and pick the earlier appointments
      5. send the ones not reported yet over Telegram, and mark them as reported
    Returns how many earlier appointments were found and how many of them are new.


watcher.py - the main loop (continuous mode)
--------------------------------------------
Runs the whole time the bot is on. Everything happens in a single thread, because
Playwright doesn't support multiple threads.
Each round of the loop (step) does two things:
  1. poll_commands: asks Telegram for new commands, waiting up to 30 seconds for an answer
     (long polling). Only commands from your own chat (TG_CHAT_ID) are carried out;
     everything else is ignored.
     handle_command routes each command to its own function: cmd_pause, cmd_resume,
     cmd_check, status_text.
  2. tick: checks the clock:
       - if a timed pause has ended, watching resumes and you get a message
       - if watching is active, within active hours, and a check is due, it runs check_once
       - otherwise, if enough time has passed, a keep-alive to the site
  - run_check: wraps check_once. Records the last check time, counts failures in a row
    (and messages you after 3), and saves everything to state.json.
  - next_check_at: works out when the next check will actually run, taking the pause and
    active hours into account. Used by /status and the confirmation messages.
"""
