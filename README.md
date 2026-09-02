# Gala Telegram + MAX Stats Bridge

Небольшой сервис для точного сбора просмотров и реакций Telegram-постов через MTProto и MAX-постов.

Он нужен, когда Telegram-ссылок много. TGStat Free быстро упирается в лимиты, а этот сервис получает статистику напрямую через Telegram-сессию и поддерживает пачечный endpoint `/stats/batch`.

Для MAX мост нужен из-за HTTPS-особенностей домена `platform-api2.max.ru`: основной сайт может получить `HTTP 526`, а Cloud Run-сервис может ходить в MAX через управляемый HTTP-клиент.

MAX-ссылки вида `https://max.ru/channel/code` содержат публичный код поста, а не внутренний `message_id`. Официальный MAX Bot API может читать историю канала только при доступе бота к каналу, поэтому для публичных каналов без бота добавлен второй режим: служебная пользовательская MAX-сессия через `maxapi-python`. В этом режиме мост открывает доступный публичный канал как обычный аккаунт, читает историю без воспроизведения видео и ищет пост по ссылке/тексту.

Важно: `maxapi-python` работает через неофициальный пользовательский клиент MAX. Это самый практичный бесплатный вариант без добавления бота в каждый канал, но он может сломаться при изменениях MAX и его надо использовать только для каналов, к которым у служебного аккаунта есть нормальный доступ.

## Что потребуется один раз

1. `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` из my.telegram.org.
2. `TELEGRAM_SESSION_STRING`, созданная локально через `make_session.py`.
3. `TELEGRAM_BRIDGE_TOKEN`, любой длинный пароль для связи сайта с мостом.
4. Для официального MAX Bot API: сайт передаёт свой `MAX_BOT_TOKEN` в `Authorization`.
5. Для MAX без добавления бота в каждый канал: `MAX_USER_SESSION_JSON_B64`, созданная локально через `make_max_user_session.py`.

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

## Точный MAX без добавления бота в каждый канал

Этот режим нужен для вашей рабочей схемы: много публичных каналов, куда нельзя или неудобно добавлять бота.

1. Создайте или возьмите отдельный служебный MAX-аккаунт.
2. Убедитесь, что этот аккаунт может открыть нужные публичные каналы MAX.
3. Локально запустите:

```bash
python make_max_user_session.py
```

4. Откройте созданный файл `max_login_qr.html` в браузере и отсканируйте QR-код служебным MAX-аккаунтом. Страница сама обновляется, если скрипт создаёт новый QR.
5. Скрипт создаст файлы `max_user_session.json` и `max_user_session_b64.txt`.
6. Значение из `max_user_session_b64.txt` нужно добавить в Cloud Run как переменную `MAX_USER_SESSION_JSON_B64`.
7. После обновления Cloud Run `/health` должен показывать `"max_user_session": true`.

Не отправляйте содержимое `max_user_session.json` и `max_user_session_b64.txt` в чаты. Это полноценная сессия входа.

Дополнительные настройки:

```env
MAX_USER_SESSION_JSON_B64=
MAX_USER_AUTO_JOIN=true
MAX_USER_SCAN_PAGE_SIZE=100
MAX_USER_SCAN_PAGES=20
```

`MAX_USER_SCAN_PAGES=20` и `MAX_USER_SCAN_PAGE_SIZE=100` означают поиск примерно по 2000 последним сообщениям канала. Если посты старше, увеличьте `MAX_USER_SCAN_PAGES`.

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
MAX_CHANNEL_CHAT_IDS=NeoficialniyBeZsonoV:-1000000000000
MAX_PUBLIC_PAGE_TEXT_FALLBACK=true
MAX_UPDATES_LIMIT=1000
```

С такими значениями официальный bot-режим проверяет до 2000 последних сообщений одного MAX-канала за одно обновление. Если публичный код поста из ссылки не совпал с данными API, мост дополнительно берёт текст публичной карточки MAX и сопоставляет его с текстами сообщений из API. `MAX_CHANNEL_CHAT_IDS` нужен, если MAX больше не отдаёт `chat_id` по публичной ссылке канала: укажите пары `channel:chat_id` через запятую. Если `chat_id` не указан, мост также пробует найти его в последних событиях бота через `MAX_UPDATES_LIMIT`. Если пост старше, увеличьте `MAX_CHAT_SCAN_PAGES`, но не ставьте слишком большое значение без необходимости.

## Развёртывание

- Для Cloud Run: смотрите `CLOUD_RUN.md`.
- Для Render: в этой папке уже есть `render.yaml`.

## Важно

- Сервис не открывает видео и не увеличивает просмотры: используется `increment=False`.
- Сессия должна принадлежать аккаунту, которому доступны нужные публичные или приватные каналы.
- Если Telegram попросит код входа, его вводит владелец аккаунта только локально при создании `TELEGRAM_SESSION_STRING`.
- Для MAX точные цифры доступны только по данным, которые отдаёт MAX: через официальный Bot API при доступе бота к каналу или через служебную пользовательскую сессию, если аккаунт может открыть канал.
