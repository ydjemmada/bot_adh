import os
from pathlib import Path

import requests
from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not bot_token or bot_token == "your_bot_token_here":
        print("Error: Please set TELEGRAM_BOT_TOKEN in .env first.")
        return

    response = requests.get(
        f"https://api.telegram.org/bot{bot_token}/deleteWebhook",
        params={"drop_pending_updates": "false"},
        timeout=20,
    )
    data = response.json()

    if data.get("ok"):
        print("Webhook cleared. You can now use Telegram polling.")
    else:
        print(f"Telegram API error: {data.get('description', 'Unknown error')}")


if __name__ == "__main__":
    main()
