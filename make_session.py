from __future__ import annotations

import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()


def main() -> None:
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise SystemExit("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.")

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        print("\nСессия создана. Скопируйте строку ниже в переменную TELEGRAM_SESSION_STRING на сервере.\n")
        print(client.session.save())


if __name__ == "__main__":
    main()
