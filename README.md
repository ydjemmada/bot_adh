# Adhahi.dz Telegram Monitor

This bot checks Adhahi's public wilaya quotas API for available wilayas and sends
Telegram alerts when availability appears.

## Render Deployment

This project is prepared for Render with:

```text
Dockerfile
render.yaml
RENDER_DEPLOY.md
```

Use Render's Blueprint flow after pushing the project to GitHub. See
`RENDER_DEPLOY.md` for the full GitHub and Render steps.

## Upload Package

For PythonAnywhere, upload `pythonanywhere_upload.zip` and unzip it into one project
folder. The PythonAnywhere package uses the optimized API-first checker and does not
need Chromium/Playwright by default.

## Configure

Create `.env` from `.env.example` and set:

```bash
TELEGRAM_BOT_TOKEN=your_real_botfather_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
BROWSER_FALLBACK_ENABLED=false
ERROR_SCREENSHOT_PATH=
PORT=
```

To find your chat id, start a conversation with your bot on Telegram, send any message,
then run:

```bash
python clear_webhook.py
python get_chat_id.py
```

## Run on PythonAnywhere

```bash
cd /home/YOUR_USERNAME/adhahi_telegram
mkvirtualenv adhahi-env --python=python3.10
pip install -r requirements.txt
python -u monitor.py
```

For continuous running, use a PythonAnywhere Always-on task:

```bash
cd /home/YOUR_USERNAME/adhahi_telegram && source virtualenvwrapper.sh && workon adhahi-env && python -u monitor.py
```

Stop any old deployment before starting this one so Telegram polling only runs in one
place.
