from __future__ import annotations

import asyncio
import os
import re
from collections import defaultdict
from typing import Any

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
        return
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
