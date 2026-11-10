"""Get a Telegram chat ID and test delivery without storing the token on disk."""
import getpass
import requests


def main():
    print("First, open your new Telegram bot and send it /start.")
    token = getpass.getpass("Paste your bot token (hidden): ").strip()
    if not token:
        raise SystemExit("No token supplied.")
    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates", timeout=25
    )
    resp.raise_for_status()
    data = resp.json()
    chats = [
        item["message"]["chat"]["id"]
        for item in data.get("result", [])
        if item.get("message") and item["message"].get("chat")
    ]
    if not chats:
        raise SystemExit("No messages found. Send /start to the bot and run this again.")
    chat_id = chats[-1]
    print(f"Your chat ID is: {chat_id}")
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": "Your job monitor is connected!"},
        timeout=25,
    )
    resp.raise_for_status()
    print("Test message sent. Add the bot token and chat ID as GitHub Actions secrets.")


if __name__ == "__main__":
    main()
