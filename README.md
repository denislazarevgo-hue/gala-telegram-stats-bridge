# Gala Telegram + MAX Stats Bridge

Небольшой сервис для точного сбора просмотров и реакций Telegram-постов через MTProto и MAX-постов через официальный MAX Bot API.

Он нужен, когда Telegram-ссылок много. TGStat Free быстро упирается в лимиты, а этот сервис получает статистику напрямую через Telegram-сессию и поддерживает пачечный endpoint `/stats/batch`.

Для MAX мост нужен из-за HTTPS-особенностей домена `platform-api2.max.ru`: основной сайт может получить `HTTP 526`, а Cloud Run-сервис может ходить в MAX через управляемый HTTP-клиент.

MAX-ссылки вида `https://max.ru/channel/code` содержат публичный код поста, а не внутренний `message_id`. Для таких ссылок мост сначала пытается получить `chat_id` канала через MAX Bot API, затем ищет пост по публичному URL среди последних сообщений канала. Поэтому бот должен быть добавлен в канал и иметь доступ к сообщениям.

## Что потребуется один раз

1. `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` из my.telegram.org.
2. `TELEGRAM_SESSION_STRING`, созданная локально через `make_session.py`.
3. `TELEGRAM_BRIDGE_TOKEN`, любой длинный пароль для связи сайта с мостом.
4. Для MAX отдельный секрет в мосте не нужен: сайт передаёт свой `MAX_BOT_TOKEN` в `Authorization`.

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

Проверка MAX:

```bash
curl -H "Authorization: Bearer MAX_BOT_TOKEN" "http://localhost:8000/max/stats?url=https://max.ru/channel/message"
```

## Подключение к сайту

В переменные сайта нужно добавить:

```env
TELEGRAM_STATS_BATCH_ENDPOINT=https://адрес-моста/stats/batch
TELEGRAM_STATS_ENDPOINT=https://адрес-моста/stats
TELEGRAM_STATS_TOKEN=тот_же_TELEGRAM_BRIDGE_TOKEN
MAX_STATS_BATCH_ENDPOINT=https://адрес-моста/max/stats/batch
MAX_STATS_ENDPOINT=https://адрес-моста/max/stats
```

После этого кнопка обновления на сайте будет собирать Telegram и MAX пачками и возвращать точные целые значения, если платформа отдаёт статистику конкретного поста.

Для MAX можно настроить глубину поиска:

```env
MAX_CHAT_SCAN_PAGE_SIZE=100
MAX_CHAT_SCAN_PAGES=20
```

С такими значениями мост проверяет до 2000 последних сообщений одного MAX-канала за одно обновление. Если пост старше, увеличьте `MAX_CHAT_SCAN_PAGES`, но не ставьте слишком большое значение без необходимости.

## Развёртывание

- Для Cloud Run: смотрите `CLOUD_RUN.md`.
- Для Render: в этой папке уже есть `render.yaml`.

## Важно

- Сервис не открывает видео и не увеличивает просмотры: используется `increment=False`.
- Сессия должна принадлежать аккаунту, которому доступны нужные публичные или приватные каналы.
- Если Telegram попросит код входа, его вводит владелец аккаунта только локально при создании `TELEGRAM_SESSION_STRING`.
- Для MAX точные цифры доступны только по данным, которые отдаёт официальный Bot API. Публичный код из ссылки сам по себе не является `message_id`.
