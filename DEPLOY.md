# Deploying on PythonAnywhere

Upload the prepared `pythonanywhere_upload.zip` file to PythonAnywhere, then unzip it
inside one folder such as:

```bash
/home/YOUR_USERNAME/adhahi_telegram
```

Do not upload local logs, `__pycache__`, screenshots, test files, or an old `.env`
containing secrets you no longer want to use.

## Files in the upload package

```text
monitor.py
get_chat_id.py
requirements.txt
README.md
DEPLOY.md
.env.example
```

## Create `.env`

In PythonAnywhere's file editor, copy `.env.example` to `.env`, then fill in your real
Telegram values:

```bash
TELEGRAM_BOT_TOKEN=your_real_botfather_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
```

`TELEGRAM_BOT_TOKEN` must be the full token from BotFather, not just the bot id.

`TELEGRAM_CHAT_ID` must be the numeric id of the recipient chat. It must not be the
bot id from the token. For a private chat, open Telegram, start the bot, send it any
message, then run `python get_chat_id.py` after setting `TELEGRAM_BOT_TOKEN`.

## PythonAnywhere settings

Use PythonAnywhere's installed Chromium:

```bash
CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
ERROR_SCREENSHOT_PATH=
PORT=
```

`PORT` should stay empty on PythonAnywhere because this bot uses Telegram polling and
does not need to run as a web app.

## Install

Open a PythonAnywhere Bash console and run:

```bash
cd /home/YOUR_USERNAME/adhahi_telegram
mkvirtualenv adhahi-env --python=python3.10
pip install -r requirements.txt
```

PythonAnywhere already provides Chromium, so do not run `playwright install chromium`.

## Test manually

```bash
cd /home/YOUR_USERNAME/adhahi_telegram
workon adhahi-env
python clear_webhook.py
python get_chat_id.py
python -u monitor.py
```

If `get_chat_id.py` says no messages were found, open Telegram, start the bot, send
it any message, then run `python get_chat_id.py` again. If `monitor.py` starts
correctly, you should receive a Telegram startup message.

## Run continuously

Use a PythonAnywhere Always-on task if your account supports it:

```bash
cd /home/YOUR_USERNAME/adhahi_telegram && source virtualenvwrapper.sh && workon adhahi-env && python -u monitor.py
```

Run only one copy of the bot for a token at a time. Stop the old KataBump deployment
before starting PythonAnywhere, otherwise Telegram may report a `getUpdates` conflict.
