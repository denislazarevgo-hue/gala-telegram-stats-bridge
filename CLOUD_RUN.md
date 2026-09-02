# Cloud Run: Telegram MTProto + MAX bridge

Эта папка уже готова как маленький Docker-сервис для Cloud Run. Он нужен сайту Gala View Report Collector, чтобы получать точные просмотры и реакции Telegram через MTProto, а также ходить в MAX API из среды, где можно управлять HTTPS-проверкой.

## Что должно быть готово

1. В проекте Google Cloud должен быть включен billing.
2. Должны быть `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` из `my.telegram.org`.
3. Должна быть создана `TELEGRAM_SESSION_STRING` через `make_session.py`.
4. Нужно придумать длинный `TELEGRAM_BRIDGE_TOKEN`; он будет общим секретом между сайтом и мостом.
5. Для MAX отдельный токен в Cloud Run не нужен: сайт передаёт `MAX_BOT_TOKEN` мосту как `Authorization`-заголовок.

## Настройки Cloud Run

Рекомендуемые параметры:

- Service name: `gala-telegram-stats-bridge`
- Region: `europe-west1`
- Runtime: Dockerfile из этой папки
- CPU: минимально доступный вариант
- Memory: 512 MiB
- Min instances: 0
- Max instances: 1 или 2
- Authentication: публичный HTTP endpoint, но защищённый `TELEGRAM_BRIDGE_TOKEN`

## Environment variables

В Cloud Run нужно добавить:

```env
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION_STRING=...
TELEGRAM_BRIDGE_TOKEN=...
REQUEST_TIMEOUT_SECONDS=25
TELEGRAM_BATCH_CHUNK_SIZE=100
MAX_BATCH_CHUNK_SIZE=100
MAX_CHAT_SCAN_PAGE_SIZE=100
MAX_CHAT_SCAN_PAGES=20
MAX_VERIFY_TLS=false
```

Не кладите эти значения в GitHub, Excel, README или логи.

## Проверка

После деплоя:

```bash
curl https://адрес-cloud-run/health
```

Должно быть:

```json
{"ok":true,"telegram_api_id":true,"telegram_api_hash":true,"telegram_session":true,"max_bridge":true,"max_verify_tls":false}
```

Проверка одной ссылки:

```bash
curl -H "Authorization: Bearer TELEGRAM_BRIDGE_TOKEN" "https://адрес-cloud-run/stats?url=https://t.me/channel/123"
```

Проверка MAX-ссылки:

```bash
curl -H "Authorization: Bearer MAX_BOT_TOKEN" "https://адрес-cloud-run/max/stats?url=https://max.ru/channel/message"
```

## Подключение к сайту

В переменные сайта Gala View Report Collector добавить:

```env
TELEGRAM_STATS_BATCH_ENDPOINT=https://адрес-cloud-run/stats/batch
TELEGRAM_STATS_ENDPOINT=https://адрес-cloud-run/stats
TELEGRAM_STATS_TOKEN=тот_же_TELEGRAM_BRIDGE_TOKEN
MAX_STATS_BATCH_ENDPOINT=https://адрес-cloud-run/max/stats/batch
MAX_STATS_ENDPOINT=https://адрес-cloud-run/max/stats
MAX_STATS_TOKEN=
```

После этого кнопка обновления статистики будет использовать мост и возвращать точные целые числа для Telegram и MAX, если MAX Bot API отдаёт статистику конкретного сообщения.
