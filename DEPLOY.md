# Deploying on PythonAnywhere

This version is optimized for PythonAnywhere by checking Adhahi's public wilaya
quotas API first. It does not need Chromium or Playwright unless you manually enable
the optional browser fallback.

## Upload Package

Upload `pythonanywhere_upload.zip` to PythonAnywhere and unzip it into one folder:

```bash
/home/YOUR_USERNAME/adhahi_telegram
```

The upload package contains:

```text
monitor.py
get_chat_id.py
clear_webhook.py
requirements.txt
requirements-browser.txt
README.md
DEPLOY.md
.env.example
```

Do not upload your local `.env`, logs, screenshots, `__pycache__`, or old deployment
folders.

## Create `.env`

In PythonAnywhere's file editor, copy `.env.example` to `.env`, then set:

```bash
TELEGRAM_BOT_TOKEN=your_real_botfather_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
```

Recommended PythonAnywhere settings:

```bash
CHECK_INTERVAL_SECONDS=600
CHECK_TIMEOUT_SECONDS=90
API_CONNECT_TIMEOUT_SECONDS=6
API_TIMEOUT_SECONDS=20
API_RETRIES=3
API_RETRY_DELAY_SECONDS=2
API_FORCE_IPV4=true
FIRST_CHECK_DELAY_SECONDS=180
TARGET_URL=https://adhahi.dz/register
WILAYA_QUOTAS_API_URL=https://adhahi.dz/api/v1/public/wilaya-quotas
WILAYA_QUOTAS_API_URLS=https://adhahi.dz/api/v1/public/wilaya-quotas,https://www.adhahi.dz/api/v1/public/wilaya-quotas
BROWSER_FALLBACK_ENABLED=false
CHROMIUM_EXECUTABLE_PATH=
ERROR_SCREENSHOT_PATH=
PORT=
```

`TELEGRAM_CHAT_ID` must be your numeric recipient chat ID, not the bot ID from the
token.

## Install

Open a PythonAnywhere Bash console:

```bash
cd /home/YOUR_USERNAME/adhahi_telegram
mkvirtualenv adhahi-env --python=python3.10
pip install -r requirements.txt
```

Do not install Playwright for the normal API-first version.

## Find Your Chat ID

Start a chat with your bot in Telegram, send it any message, then run:

```bash
cd /home/YOUR_USERNAME/adhahi_telegram
workon adhahi-env
python clear_webhook.py
python get_chat_id.py
```

Copy the printed chat ID into `.env`.

## Test Manually

```bash
cd /home/YOUR_USERNAME/adhahi_telegram
workon adhahi-env
python -u monitor.py
```

You should receive a startup message in Telegram. Then send:

```text
/status
/check
```

The logs should mention:

```text
Checking availability API using 2 endpoint(s)...
Availability API returned ...
```

## Run Continuously

Use a PythonAnywhere Always-on task:

```bash
cd /home/YOUR_USERNAME/adhahi_telegram && source virtualenvwrapper.sh && workon adhahi-env && python -u monitor.py
```

Run only one copy of the bot for a token at a time. Stop Render or any other old
deployment before starting PythonAnywhere, otherwise Telegram may report a polling
conflict.

## Optional Browser Fallback

Only use this if the public API stops working and you specifically want the old
browser-based check:

```bash
pip install -r requirements-browser.txt
```

Then set:

```bash
BROWSER_FALLBACK_ENABLED=true
CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
```

The API-first path is faster and more reliable, so keep browser fallback disabled
unless you need it.
