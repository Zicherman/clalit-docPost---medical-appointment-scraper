# Clalit DocSpot

Automatic monitor for open specialist appointments at Clalit Health Services (Israel). When an appointment opens up earlier than the one you already have, you get a Telegram message.

> **The script is read-only.** It never books, changes or cancels appointments. You book the appointment yourself, on the website or in the app.

## How it works

1. Logs in to the Clalit website with your credentials, using a Playwright browser that runs in the background.
2. Reads your upcoming appointment in the configured specialty (default: neurology).
3. Searches for open appointments in each of your "anchor cities", including nearby towns, sorted by earliest date.
4. Filters out places that aren't relevant to you, and appointments that are too soon (today).
5. If it finds an appointment earlier than yours, it sends you a Telegram message. Each appointment is reported only once.

Example message (messages are in Hebrew, like the site):

```
🩺 נמצא תור מוקדם יותר לנוירולוגיה!
התור שלך: 27.01.2027 12:30 – נוירולוגיה – ד"ר ..., מרכז ...

📅 30.12.2026 – נוירולוגיה – ד"ר ...
   Clinic name, address
   ☎️ 03-0000000

להזמנה: https://e-services.clalit.co.il/...
```

## Requirements

- Windows (the script itself is cross-platform, but the `.bat` files and the scheduling instructions are for Windows)
- Python 3.10+
- A Clalit online account that logs in with ID number, user code and password
- A Telegram account

## Installation

```
git clone <repo-url>
cd Clalit_DocSpot
pip install -r requirements.txt
python -m playwright install chromium
```

Then copy `env.example.txt` to a file named `env.txt` and fill in your details.

### Telegram bot

1. In Telegram, open a chat with `@BotFather`, send `/newbot` and pick a name. You'll get a **token**. Put it in `TG_TOKEN`.
2. Open a chat with your new bot and send it any message. The bot can't message you until you do.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and look for `"chat":{"id":`. That number goes in `TG_CHAT_ID`.
4. Test it (does not connect to Clalit):
   ```
   python clalit_watch.py --test-notify
   ```

## Configuration

Settings live at the top of `clalit_watch.py`:

| Setting | What it does | Default |
|---|---|---|
| `CITIES` | Anchor cities to search. Each one also covers its surroundings. Names must match the site exactly, **without hyphens**: `תל אביב יפו`, not `תל אביב - יפו` | Central Israel |
| `EXCLUDED_PLACES` | Appointments whose clinic name or address contains any of these are ignored | - |
| `SPECIALIZATION_GROUP` / `SPECIALIZATION_CODE` / `PROFESSION_NAME` | The specialty. Neurology is 32/32 | Neurology |
| `CHECK_EVERY_MIN` | Check interval in continuous mode | 60 |
| `ACTIVE_HOURS` | Active hours in continuous mode | 9:00–18:00 |
| `MIN_DAYS_AHEAD` | Ignore appointments that are too soon (1 = tomorrow onward) | 1 |
| `ALERT_IF_NO_CURRENT_APPOINTMENT` | If you have no appointment yet, report the earliest one found | `True` |

## Usage

| Command | What it does |
|---|---|
| `tests\test_search.bat` | Full dry run that **sends nothing**: logs in, reads your appointment, searches, and prints the message that would be sent. Output is saved to `tests/test_results.txt` |
| `python clalit_watch.py --test-notify` | Sends a test message to Telegram |
| `python clalit_watch.py --once` | One real check (sends to Telegram if there's a new appointment), then exits |
| `python clalit_watch.py` | Continuous mode: checks every `CHECK_EVERY_MIN` minutes during active hours |
| `--show` | Shows the browser window (for debugging) |

The log is written to `watch.log`.

## Running automatically (Windows Task Scheduler)

The recommended setup is a task that runs `--once` every hour on the hour. If the computer was asleep, the check runs as soon as it wakes up. Run this in PowerShell, after updating the path:

```powershell
$dir = "C:\path\to\Clalit_DocSpot"
$py  = (Get-Command pythonw).Source
$a = New-ScheduledTaskAction -Execute $py -Argument "clalit_watch.py --once" -WorkingDirectory $dir
$t = New-ScheduledTaskTrigger -Daily -At 9:00
$t.Repetition = (New-ScheduledTaskTrigger -Once -At 9:00 -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 9)).Repetition
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 45) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Clalit DocSpot" -Action $a -Trigger $t -Settings $s
```

- Checks every hour on the hour from 9:00 to 17:00. `pythonw` runs the bot without opening a window.
- Run now: `Start-ScheduledTask -TaskName "Clalit DocSpot"`
- Next run time: `Get-ScheduledTaskInfo -TaskName "Clalit DocSpot"`
- Remove: `Unregister-ScheduledTask -TaskName "Clalit DocSpot" -Confirm:$false`

The computer must be on and logged in to your user. While it's asleep, no checks run.

## CAPTCHA

Sometimes the Clalit site asks for a CAPTCHA. **The script does not solve it.** Instead, it sends you a Telegram message and opens a browser window with your details already filled in. Once you complete the CAPTCHA and log in, the check continues on its own and goes back to running in the background. The script waits up to 30 minutes for the manual login.

## Project structure

```
clalit_watch.py        Main script (settings at the top)
run_watch.bat          Quick launcher for Windows, passes arguments through (e.g. --once)
env.example.txt        Template for the env.txt settings file
requirements.txt
tests/
  test_search.py       Full dry run, sends nothing
  test_search.bat
```

Files created at runtime and kept out of git: `env.txt`, `browser_profile/`, `state.json`, `watch.log`, `tests/test_results.txt`.

## Troubleshooting

- **"היישוב לא זוהה באתר" (city not recognized)**: the name in `CITIES` must match the site exactly, without hyphens.
- **"ההתחברות נכשלה" (login failed)**: check your details in `env.txt`. You can run with `--show` to watch what happens.
- **No messages arrive**: run `--test-notify`. If that works, there's probably just no earlier appointment yet. `watch.log` has a line for every check.
- **test_search fails while the bot is running**: both use the same browser profile. Stop the bot first (`Stop-ScheduledTask`).

## Security and privacy

- `env.txt` holds your password and bot token, and `browser_profile/` holds a logged-in session. Both are in `.gitignore`. **Never share or upload them.**
- `state.json`, `watch.log` and `test_results.txt` contain medical details (your appointment). They are kept out of git too.
- The Telegram token is never written to the log.

## Disclaimer

Personal, unofficial project, not affiliated with Clalit Health Services. Use at your own risk. The script only accesses your own account, and makes a small number of requests (one check per hour, with a pause between searches) to avoid loading the site.
