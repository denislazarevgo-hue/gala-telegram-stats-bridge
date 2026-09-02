from __future__ import annotations

import os
import getpass
from pathlib import Path

from dotenv import load_dotenv
from telethon.sessions import StringSession
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv()


def main() -> None:
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise SystemExit("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.")

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        if not client.is_user_authorized():
            phone = input("Введите номер телефона Telegram в международном формате: ").strip()
            client.send_code_request(phone)
            code = input("Введите новый код из Telegram только здесь, не в чат Codex: ").strip()

            try:
                client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                password = getpass.getpass(
                    "Введите пароль 2FA Telegram. Символы не будут отображаться: "
                )
                client.sign_in(password=password)

        session_string = client.session.save()

    output_path = Path("telegram_session_string.txt")
    output_path.write_text(session_string, encoding="utf-8")
    print(f"Сессия создана и сохранена в {output_path}. Не публикуйте этот файл.")


if __name__ == "__main__":
    main()
