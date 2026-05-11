# Deploying on Render

This project is prepared for Render as a Docker-based Background Worker.

A Background Worker is the right Render service type for this bot because the bot runs continuously and uses Telegram polling. It does not need to receive public web traffic.

## Files Added for Render

```text
Dockerfile
render.yaml
.dockerignore
.gitignore
RENDER_DEPLOY.md
```

Do not commit `.env`, `monitor.log`, screenshots, `__pycache__`, or the old PythonAnywhere upload zip. `.gitignore` already excludes them.

## 1. Test Locally

Make sure your local `.env` has real values:

```bash
TELEGRAM_BOT_TOKEN=your_real_botfather_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
CHECK_INTERVAL_SECONDS=600
CHECK_TIMEOUT_SECONDS=35
TARGET_URL=https://adhahi.dz/register
ERROR_SCREENSHOT_PATH=
```

If you still need your chat ID:

```bash
python clear_webhook.py
python get_chat_id.py
```

Then test the bot:

```bash
python -u monitor.py
```

Stop the local bot before deploying to Render. Only one polling deployment should run for the same Telegram bot token.

## 2. Create the GitHub Repository

This folder has been initialized as a Git repository on the `main` branch. From this
project folder, create the first commit:

```bash
git add .
git status
git commit -m "Prepare Render deployment"
```

Create a new empty repository on GitHub. Do not add a README, `.gitignore`, or license on GitHub because the project already has files locally.

Connect your local repo to GitHub:

```bash
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME.git
git push -u origin main
```

Before pushing, `git status` should not show `.env`. If it does, stop and remove it from Git tracking before pushing.

## 3. Deploy on Render with Blueprint

1. Sign in to Render.
2. Click **New**.
3. Choose **Blueprint**.
4. Connect your GitHub account if Render asks.
5. Select the GitHub repository you pushed.
6. Render will detect `render.yaml`.
7. Confirm the service named `adhahi-telegram-monitor`.
8. Enter the secret environment variables when prompted:

```bash
TELEGRAM_BOT_TOKEN=your_real_botfather_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
```

9. Create/apply the Blueprint.
10. Open the service logs and wait for the startup message:

```text
Bot is running. Auto-check every 10 minutes.
```

You should also receive a Telegram startup message.

## 4. Important Render Notes

Render Background Workers do not use the free instance type, so this deployment uses `plan: starter` in `render.yaml`.

The bot uses Docker because Playwright needs Chromium and system browser dependencies. The `Dockerfile` installs Python requirements and runs:

```bash
playwright install --with-deps chromium
```

Every push to the `main` branch will trigger a new Render deploy because `render.yaml` sets:

```yaml
autoDeployTrigger: commit
```

## 5. Updating Later

After editing code locally:

```bash
git add .
git commit -m "Describe your change"
git push
```

Render will redeploy automatically.

## Troubleshooting

If Telegram reports a polling conflict, stop every old deployment that uses the same bot token, then redeploy Render.

If Render says the bot token or chat ID is invalid, update the service environment variables in the Render dashboard and redeploy.

If the website check fails because Chromium cannot start, check the Render build logs and confirm the service is using the Docker runtime from `render.yaml`.
