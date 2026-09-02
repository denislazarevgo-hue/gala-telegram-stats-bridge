from __future__ import annotations

import asyncio
import os
import re
from collections import defaultdict
from urllib.parse import unquote, urlparse
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetMessagesViewsRequest

load_dotenv()

app = FastAPI(title="Gala Telegram Stats Bridge")

_client: TelegramClient | None = None
_client_lock = asyncio.Lock()


class StatsItem(BaseModel):
    id: int | None = None
    url: str
    channel: str | None = None
    message_id: int | None = Field(default=None, alias="messageId")

    class Config:
        populate_by_name = True


class BatchRequest(BaseModel):
    items: list[StatsItem]


class MaxStatsItem(BaseModel):
    id: int | None = None
    url: str
    message_id: str | None = Field(default=None, alias="messageId")
    chat_id: str | None = Field(default=None, alias="chatId")
    channel: str | None = None

    class Config:
        populate_by_name = True


class MaxBatchRequest(BaseModel):
    items: list[MaxStatsItem]


def _result(
    item: StatsItem,
    views: int | None,
    reactions: int | None,
    status: str,
    error: str | None = None,
    raw_data: Any = None,
) -> dict[str, Any]:
    if status != "ok":
        views = None
        reactions = None
    return {
        "id": item.id,
        "url": item.url,
        "platform": "telegram",
        "views": views,
        "reactions": reactions,
        "status": status,
        "error": error,
        "source": "telegram_mtproto_bridge",
        "precision": "exact" if status == "ok" else "unknown",
        "raw_data": raw_data,
    }


def _require_auth(request: Request) -> None:
    expected = os.getenv("TELEGRAM_BRIDGE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="TELEGRAM_BRIDGE_TOKEN не заполнен.")
    header = request.headers.get("authorization", "")
    if header != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Неверный Telegram bridge token.")


def _parse_url(url: str) -> tuple[str | None, int | None]:
    match = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]+)/(\d+)", url)
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


async def _get_client() -> TelegramClient:
    global _client

    async with _client_lock:
        if _client and _client.is_connected() and await _client.is_user_authorized():
            return _client

        api_id = os.getenv("TELEGRAM_API_ID", "").strip()
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        if not api_id or not api_hash:
            raise RuntimeError("TELEGRAM_API_ID и TELEGRAM_API_HASH не заполнены.")

        session_string = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
        session_name = os.getenv("TELEGRAM_SESSION_NAME", "gala_telegram_stats").strip()
        session = StringSession(session_string) if session_string else session_name
        timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "25"))

        _client = TelegramClient(
            session,
            int(api_id),
            api_hash,
            receive_updates=False,
            request_retries=1,
            connection_retries=1,
            timeout=timeout,
        )
        await _client.connect()
        if not await _client.is_user_authorized():
            raise RuntimeError("Telegram-сессия не авторизована.")
        return _client


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "telegram_api_id": bool(os.getenv("TELEGRAM_API_ID", "").strip()),
        "telegram_api_hash": bool(os.getenv("TELEGRAM_API_HASH", "").strip()),
        "telegram_session": bool(os.getenv("TELEGRAM_SESSION_STRING", "").strip()),
        "max_bridge": True,
        "max_verify_tls": _max_verify_tls(),
    }


@app.get("/stats")
async def stats(request: Request, url: str, channel: str | None = None, message_id: int | None = None) -> dict[str, Any]:
    _require_auth(request)
    parsed_channel, parsed_message_id = _parse_url(url)
    item = StatsItem(
        id=0,
        url=url,
        channel=channel or parsed_channel,
        messageId=message_id or parsed_message_id,
    )
    return (await _collect_batch([item]))["items"][0]


@app.post("/stats/batch")
async def stats_batch(request: Request, body: BatchRequest) -> dict[str, Any]:
    _require_auth(request)
    return await _collect_batch(body.items)


@app.get("/max/stats")
async def max_stats(
    request: Request,
    url: str,
    id: str | None = None,
    message_id: str | None = None,
    chat_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    token = _max_token_from_request(request)
    item = MaxStatsItem(
        id=0,
        url=url,
        messageId=message_id or id,
        chatId=chat_id,
        channel=channel,
    )
    return (await _collect_max_batch([item], token))["items"][0]


@app.post("/max/stats/batch")
async def max_stats_batch(request: Request, body: MaxBatchRequest) -> dict[str, Any]:
    token = _max_token_from_request(request)
    return await _collect_max_batch(body.items, token)


async def _collect_batch(items: list[StatsItem]) -> dict[str, Any]:
    normalized: list[StatsItem] = []
    for item in items:
        parsed_channel, parsed_message_id = _parse_url(item.url)
        normalized.append(
            StatsItem(
                id=item.id,
                url=item.url,
                channel=item.channel or parsed_channel,
                messageId=item.message_id or parsed_message_id,
            )
        )

    invalid = [
        _result(item, None, None, "unsupported_url", "Telegram-ссылка не распознана.")
        for item in normalized
        if not item.channel or item.message_id is None
    ]
    valid = [item for item in normalized if item.channel and item.message_id is not None]
    if not valid:
        return {"items": invalid}

    try:
        client = await _get_client()
    except RuntimeError as exc:
        return {"items": invalid + [_result(item, None, None, "auth_required", str(exc)) for item in valid]}

    grouped: dict[str, list[StatsItem]] = defaultdict(list)
    for item in valid:
        grouped[item.channel or ""].append(item)

    results = invalid[:]
    for channel, channel_items in grouped.items():
        results.extend(await _collect_channel(client, channel, channel_items))

    return {"items": sorted(results, key=lambda row: row.get("id") or 0)}


async def _collect_channel(client: TelegramClient, channel: str, items: list[StatsItem]) -> list[dict[str, Any]]:
    try:
        entity = await client.get_entity(channel)
    except (errors.ChannelPrivateError, errors.ChatAdminRequiredError):
        return [_result(item, None, None, "auth_required", "Канал приватный или недоступен для Telegram-сессии.") for item in items]
    except (errors.UsernameInvalidError, errors.UsernameNotOccupiedError, ValueError):
        return [_result(item, None, None, "not_found", "Telegram-канал не найден.") for item in items]

    output: list[dict[str, Any]] = []
    chunk_size = max(1, int(os.getenv("TELEGRAM_BATCH_CHUNK_SIZE", "100")))
    for start in range(0, len(items), chunk_size):
        chunk = items[start : start + chunk_size]
        ids = [int(item.message_id or 0) for item in chunk]
        try:
            views_response = await client(GetMessagesViewsRequest(peer=entity, id=ids, increment=False))
            messages = await client.get_messages(entity, ids=ids)
        except errors.FloodWaitError as exc:
            output.extend([
                _result(item, None, None, "rate_limited", f"Telegram ограничил запросы. Подождите {exc.seconds} секунд.")
                for item in chunk
            ])
            continue
        except (errors.ChannelPrivateError, errors.ChatAdminRequiredError):
            output.extend([
                _result(item, None, None, "auth_required", "Канал приватный или недоступен для Telegram-сессии.")
                for item in chunk
            ])
            continue
        except Exception as exc:
            output.extend([_result(item, None, None, "error", f"Ошибка Telegram-сбора: {exc}") for item in chunk])
            continue

        view_items = getattr(views_response, "views", views_response)
        if not isinstance(messages, list):
            messages = [messages]
        messages_by_id = {getattr(message, "id", None): message for message in messages if message is not None}

        for index, item in enumerate(chunk):
            message = messages_by_id.get(item.message_id)
            views = _extract_views(view_items, index)
            if views is None and message is not None:
                views = _optional_int(getattr(message, "views", None))
            reactions = _sum_reactions(message)
            if views is None and reactions is None:
                output.append(_result(item, None, None, "not_found", "Telegram не вернул сообщение или статистику."))
            else:
                output.append(_result(item, views, reactions, "ok", raw_data={"channel": channel, "message_id": item.message_id}))

    return output


async def _collect_max_batch(items: list[MaxStatsItem], token: str) -> dict[str, Any]:
    normalized: list[MaxStatsItem] = []
    for item in items:
        parsed_id, parsed_chat_id, parsed_channel = _parse_max_url(item.url)
        normalized.append(
            MaxStatsItem(
                id=item.id,
                url=item.url,
                messageId=item.message_id or parsed_id,
                chatId=item.chat_id or parsed_chat_id,
                channel=item.channel or parsed_channel,
            )
        )

    invalid = [
        _max_result(item, None, None, "manual_required", "Из ссылки MAX не удалось достоверно извлечь ID поста.")
        for item in normalized
        if not item.message_id
    ]
    valid = [item for item in normalized if item.message_id]
    if not valid:
        return {"items": invalid}

    output = invalid[:]
    chunk_size = max(1, int(os.getenv("MAX_BATCH_CHUNK_SIZE", "100")))
    for start in range(0, len(valid), chunk_size):
        chunk = valid[start : start + chunk_size]
        data = await _fetch_max_messages([str(item.message_id) for item in chunk], token)
        if data["http_status"] == 429:
            output.extend([_max_result(item, None, None, "rate_limited", "MAX временно ограничил запросы.") for item in chunk])
            continue
        if not data["ok"]:
            status = _status_from_http(data["http_status"])
            output.extend([_max_result(item, None, None, status, data["error"]) for item in chunk])
            continue

        for item in chunk:
            message = _extract_max_message(data["json"], item.url, str(item.message_id), require_matching_message=True)
            if not message:
                single = await _fetch_max_message(str(item.message_id), token)
                if single["ok"]:
                    message = _extract_max_message(single["json"], item.url, str(item.message_id), require_matching_message=False)
                elif single["http_status"] in (401, 403, 404, 429):
                    output.append(_max_result(item, None, None, _status_from_http(single["http_status"]), single["error"]))
                    continue

            if not message:
                output.append(_max_result(item, None, None, "not_found", "MAX API не вернул сообщение для этой ссылки."))
                continue
            output.append(_max_result_from_message(item, message))

    return {"items": sorted(output, key=lambda row: row.get("id") or 0)}


async def _fetch_max_messages(message_ids: list[str], token: str) -> dict[str, Any]:
    endpoint = "https://platform-api2.max.ru/messages"
    params = {"message_ids": ",".join(_unique(message_ids))}
    return await _max_get_json(endpoint, token, params=params)


async def _fetch_max_message(message_id: str, token: str) -> dict[str, Any]:
    endpoint = f"https://platform-api2.max.ru/messages/{message_id}"
    return await _max_get_json(endpoint, token)


async def _max_get_json(url: str, token: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    raw = await _max_get_json_once(url, token, params=params, bearer=False)
    if raw["http_status"] in (401, 403):
        bearer = await _max_get_json_once(url, token, params=params, bearer=True)
        if bearer["ok"] or bearer["http_status"] != raw["http_status"]:
            return bearer
    return raw


async def _max_get_json_once(
    url: str,
    token: str,
    params: dict[str, str] | None = None,
    bearer: bool = False,
) -> dict[str, Any]:
    timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "25"))
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {token}" if bearer else token,
        "user-agent": "Gala View Report Collector MAX Bridge/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=_max_verify_tls()) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException:
        return {"ok": False, "http_status": 0, "json": None, "error": "MAX не ответил за отведённое время."}
    except httpx.HTTPError as exc:
        return {"ok": False, "http_status": 0, "json": None, "error": f"Не удалось получить ответ от MAX: {exc}"}

    json_data = None
    try:
        json_data = response.json()
    except ValueError:
        json_data = None

    if not response.is_success:
        return {
            "ok": False,
            "http_status": response.status_code,
            "json": json_data,
            "error": _response_error(json_data, f"HTTP {response.status_code}: {response.reason_phrase or '<none>'}"),
        }

    return {"ok": True, "http_status": response.status_code, "json": json_data, "error": None}


def _max_result(
    item: MaxStatsItem,
    views: int | None,
    reactions: int | None,
    status: str,
    error: str | None = None,
    raw_data: Any = None,
) -> dict[str, Any]:
    if status != "ok":
        views = None
        reactions = None
    return {
        "id": item.id,
        "url": item.url,
        "platform": "max",
        "views": views,
        "reactions": reactions,
        "status": status,
        "error": error,
        "source": "max_bridge_platform_api",
        "precision": "exact" if status == "ok" else "unknown",
        "raw_data": raw_data,
    }


def _max_result_from_message(item: MaxStatsItem, message: Any) -> dict[str, Any]:
    views = _first_int(message, ["stat.views", "stats.views", "statistics.views", "views", "view_count"])
    reactions = _first_int(
        message,
        [
            "stat.reactions",
            "stats.reactions",
            "statistics.reactions",
            "reactions",
            "reaction_count",
            "likes",
            "likes_count",
        ],
    )
    if reactions is None:
        reactions = _sum_reaction_objects(_first_value(message, ["reactions", "stat.reactions", "stats.reactions", "statistics.reactions"]))

    if views is None and reactions is None:
        return _max_result(
            item,
            None,
            None,
            "manual_required",
            "MAX API нашёл сообщение, но не вернул просмотры или реакции. Обычно это значит, что боту не хватает доступа к статистике канала.",
        )

    return _max_result(
        item,
        views,
        reactions,
        "ok",
        raw_data={"message_id": item.message_id, "chat_id": item.chat_id, "channel": item.channel},
    )


def _parse_max_url(url: str) -> tuple[str | None, str | None, str | None]:
    value = url.strip()
    if not re.match(r"^https?://", value, re.I):
        value = f"https://{value}"

    message_id: str | None = None
    chat_id: str | None = None
    channel: str | None = None

    try:
        parsed = urlparse(value)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
    except ValueError:
        parts = []

    if len(parts) >= 3 and parts[0].lower() == "c":
        chat_id = parts[1]
        message_id = parts[2]
    elif len(parts) >= 2 and parts[0].lower() not in {"post", "message", "msg", "video", "share"}:
        channel = parts[0].lstrip("@")
        message_id = parts[1]

    message_id = (
        message_id
        or _regex_group(value, r"(?:post|message|msg|video|share)/([A-Za-z0-9_-]+)")
        or _regex_group(value, r"[?&](?:post_id|message_id|msg_id|id)=([A-Za-z0-9_-]+)")
        or _regex_group(value, r"/([A-Za-z0-9_-]{8,})(?:[/?#]|$)")
    )
    return message_id, chat_id, channel


def _extract_max_message(payload: Any, url: str, message_id: str, require_matching_message: bool) -> Any | None:
    root = payload if isinstance(payload, dict) else {}
    messages = (
        root.get("messages")
        or _dict_get(root, "data", "messages")
        or _dict_get(root, "response", "messages")
    )
    if isinstance(messages, list):
        if not require_matching_message and len(messages) == 1:
            return messages[0]
        for message in messages:
            if _is_max_message_match(message, url, message_id):
                return message
        return None

    direct = root.get("message") or _dict_get(root, "data", "message") or _dict_get(root, "response", "message") or payload
    if not require_matching_message or _is_max_message_match(direct, url, message_id):
        return direct
    return None


def _is_max_message_match(message: Any, url: str, message_id: str) -> bool:
    if not isinstance(message, dict):
        return False

    id_fields = [
        message.get("id"),
        message.get("message_id"),
        message.get("messageId"),
        message.get("mid"),
        _dict_get(message, "body", "mid"),
        _dict_get(message, "body", "message_id"),
        _dict_get(message, "body", "messageId"),
    ]
    if any(str(value or "") == message_id for value in id_fields):
        return True

    normalized = _normalize_url_for_compare(url)
    url_fields = [
        message.get("url"),
        message.get("link"),
        message.get("permalink"),
        message.get("message_link"),
        message.get("messageLink"),
        _dict_get(message, "body", "url"),
        _dict_get(message, "body", "link"),
    ]
    return any(
        isinstance(value, str)
        and value.strip()
        and (_normalize_url_for_compare(value) == normalized or message_id in value)
        for value in url_fields
    )


def _max_token_from_request(request: Request) -> str:
    header = request.headers.get("authorization", "").strip()
    if header.lower().startswith("bearer "):
        header = header[7:].strip()
    if not header:
        raise HTTPException(status_code=401, detail="Для MAX bridge нужен Authorization token.")
    return header


def _max_verify_tls() -> bool:
    return os.getenv("MAX_VERIFY_TLS", "false").strip().lower() in {"1", "true", "yes", "on"}


def _first_int(source: Any, paths: list[str]) -> int | None:
    for path in paths:
        value = _value_at_path(source, path)
        number = _optional_int(value)
        if number is not None:
            return number
    return None


def _first_value(source: Any, paths: list[str]) -> Any:
    for path in paths:
        value = _value_at_path(source, path)
        if value is not None:
            return value
    return None


def _value_at_path(source: Any, path: str) -> Any:
    value = source
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _sum_reaction_objects(value: Any) -> int | None:
    if not isinstance(value, list):
        return None
    total = 0
    found = False
    for item in value:
        if not isinstance(item, dict):
            continue
        count = _optional_int(item.get("count") or item.get("counter"))
        if count is not None:
            total += count
            found = True
    return total if found else None


def _dict_get(source: dict[str, Any], key: str, child_key: str) -> Any:
    child = source.get(key)
    return child.get(child_key) if isinstance(child, dict) else None


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _regex_group(value: str, pattern: str) -> str | None:
    match = re.search(pattern, value, re.I)
    return match.group(1) if match else None


def _normalize_url_for_compare(url: str) -> str:
    value = url.strip().lower().rstrip("/")
    if not re.match(r"^https?://", value, re.I):
        value = f"https://{value}"
    return value


def _status_from_http(status: int) -> str:
    if status in (401, 403):
        return "auth_required"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    return "manual_required"


def _response_error(json_data: Any, fallback: str) -> str:
    if isinstance(json_data, dict):
        error = json_data.get("error")
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            return str(error.get("message") or error)
        message = json_data.get("message") or json_data.get("detail")
        if isinstance(message, str):
            return message
    return fallback


def _extract_views(view_items: Any, index: int) -> int | None:
    if not isinstance(view_items, list) or index >= len(view_items):
        return None
    item = view_items[index]
    if isinstance(item, int):
        return item
    return _optional_int(getattr(item, "views", None) or getattr(item, "count", None))


def _sum_reactions(message: Any) -> int | None:
    reactions = getattr(message, "reactions", None)
    results = getattr(reactions, "results", None)
    if not results:
        return None
    counts = [_optional_int(getattr(item, "count", None)) for item in results]
    usable = [count for count in counts if count is not None]
    return sum(usable) if usable else None


def _optional_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
