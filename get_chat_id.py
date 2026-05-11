import requests
import os
from pathlib import Path
from dotenv import load_dotenv

def get_chat_id():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not bot_token or bot_token == "your_bot_token_here":
        print("Error: Please set your TELEGRAM_BOT_TOKEN in the .env file.")
        print("If you haven't created a .env file, copy .env.example to .env and edit it.")
        return

    print("Checking for messages sent to your bot...")
    print("If this hangs or returns nothing, make sure you have sent a message (e.g., 'hello') to your bot on Telegram!")
    
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        if data.get("ok"):
            results = data.get("result", [])
            if not results:
                print("\nNo messages found. Please go to Telegram, start a chat with your bot, send a message like 'hello', and run this script again.")
                return
                
            # Get the latest message
            latest_message = results[-1]
            if "message" in latest_message:
                chat = latest_message["message"]["chat"]
                chat_id = chat["id"]
                username = chat.get("username", "Unknown")
                first_name = chat.get("first_name", "Unknown")
                
                print(f"\nSuccess! Found chat ID.")
                print(f"User: {first_name} (@{username})")
                print(f"Chat ID: {chat_id}")
                print(f"\nPlease copy this Chat ID into your .env file as TELEGRAM_CHAT_ID={chat_id}")
            else:
                print("Could not find a valid message in the updates.")
        else:
            description = data.get("description", "Unknown error")
            print(f"Error from Telegram API: {description}")
            if "webhook" in description.lower():
                print("\nThis bot currently has a webhook enabled.")
                print("For this PythonAnywhere polling deployment, clear it with:")
                print("python clear_webhook.py")
            
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to Telegram: {e}")

if __name__ == "__main__":
    get_chat_id()
