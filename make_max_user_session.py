from __future__ import annotations

import asyncio
import base64
import html
import os
import time
from pathlib import Path

import qrcode
from qrcode.image.svg import SvgPathImage
from pymax import ExtraConfig, WebClient


OUTPUT_JSON = Path("max_user_session.json")
OUTPUT_B64 = Path("max_user_session_b64.txt")
OUTPUT_QR = Path("max_login_qr.svg")
OUTPUT_QR_HTML = Path("max_login_qr.html")


class SvgQrHandler:
    async def show_qr(self, qr_url: str) -> None:
        qr = qrcode.QRCode(border=4)
        qr.add_data(qr_url)
        qr.make(fit=True)
        image = qr.make_image(image_factory=SvgPathImage)
        image.save(OUTPUT_QR)
        _write_qr_html()
        print(f"QR сохранён в {OUTPUT_QR.resolve()}")
        print(f"Удобнее открыть {OUTPUT_QR_HTML.resolve()}: страница сама обновит QR при повторной попытке.")
        print("Скрипт ждёт подтверждения входа, не закрывайте это окно.")


def _write_qr_html() -> None:
    timestamp = int(time.time())
    OUTPUT_QR_HTML.write_text(
        f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <title>MAX QR Login</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #101010;
      color: #f5f1e8;
      font-family: Arial, sans-serif;
    }}
    main {{
      text-align: center;
      max-width: 520px;
      padding: 32px;
    }}
    img {{
      background: white;
      width: min(80vw, 420px);
      height: min(80vw, 420px);
      padding: 20px;
      border-radius: 18px;
    }}
    p {{
      color: #cbb36c;
      font-size: 18px;
      line-height: 1.4;
    }}
  </style>
</head>
<body>
  <main>
    <img src="{html.escape(OUTPUT_QR.name)}?v={timestamp}" alt="MAX login QR">
    <p>Сканируйте QR служебным MAX-аккаунтом и подтвердите вход. Эта страница обновляется сама.</p>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )


def _build_client() -> WebClient:
    return WebClient(
        work_dir="max_user_session_cache",
        session_name="max_user_session.db",
        extra_config=ExtraConfig(
            reconnect=False,
            telemetry=False,
            log_level="WARNING",
        ),
        qr_provider=SvgQrHandler(),
    )


def _looks_like_expired_qr(exc: Exception) -> bool:
    text = str(exc).lower()
    return "track.not.found" in text or "qr authentication expired" in text or "invalid trackid" in text


async def _close_client(client: WebClient | None) -> None:
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        pass


async def main() -> None:
    print("Сейчас будет создан QR для входа в MAX.")
    client: WebClient | None = None
    attempts = max(1, int(os.getenv("MAX_QR_ATTEMPTS", "3")))
    for attempt in range(1, attempts + 1):
        client = _build_client()
        try:
            await client.connect()
            break
        except Exception as exc:
            await _close_client(client)
            client = None
            if attempt >= attempts or not _looks_like_expired_qr(exc):
                raise
            print(f"QR не подтвердили вовремя, создаю новый. Попытка {attempt + 1} из {attempts}.")

    if client is None:
        raise RuntimeError("MAX-сессия не создана.")

    session = getattr(getattr(client, "_app", None), "session", None)
    if session is None:
        raise RuntimeError("MAX-сессия не создана.")

    payload = session.model_dump_json(by_alias=True, exclude_none=True)
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    OUTPUT_JSON.write_text(payload, encoding="utf-8")
    OUTPUT_B64.write_text(encoded, encoding="utf-8")

    print()
    print("Готово.")
    print(f"Сессия сохранена в {OUTPUT_JSON}")
    print(f"Для Cloud Run используйте значение из {OUTPUT_B64}")
    print("Никому не отправляйте содержимое этих файлов в чат.")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
