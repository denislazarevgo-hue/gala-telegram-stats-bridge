# Gala Telegram Stats Bridge

Небольшой сервис для точного сбора просмотров и реакций Telegram-постов через MTProto.

Он нужен, когда Telegram-ссылок много. TGStat Free быстро упирается в лимиты, а этот сервис получает статистику напрямую через Telegram-сессию и поддерживает пачечный endpoint `/stats/batch`.

## Что потребуется один раз

1. `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` из my.telegram.org.
2. `TELEGRAM_SESSION_STRING`, созданная локально через `make_session.py`.
3. `TELEGRAM_BRIDGE_TOKEN`, любой длинный пароль для связи сайта с мостом.

## Локальная проверка

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python make_session.py
uvicorn app:app --reload --port 8000
```

Проверка:

```bash
curl "http://localhost:8000/health"
```

## Подключение к сайту

В переменные сайта нужно добавить:

```env
TELEGRAM_STATS_BATCH_ENDPOINT=https://адрес-моста/stats/batch
TELEGRAM_STATS_ENDPOINT=https://адрес-моста/stats
TELEGRAM_STATS_TOKEN=тот_же_TELEGRAM_BRIDGE_TOKEN
```

После этого кнопка обновления на сайте будет собирать Telegram по каналам пачками и возвращать точные целые значения.

## Развёртывание

- Для Cloud Run: смотрите `CLOUD_RUN.md`.
- Для Render: в этой папке уже есть `render.yaml`.

## Важно

- Сервис не открывает видео и не увеличивает просмотры: используется `increment=False`.
- Сессия должна принадлежать аккаунту, которому доступны нужные публичные или приватные каналы.
- Если Telegram попросит код входа, его вводит владелец аккаунта только локально при создании `TELEGRAM_SESSION_STRING`.
