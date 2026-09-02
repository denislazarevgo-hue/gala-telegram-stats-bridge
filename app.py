from __future__ import annotations

import asyncio
import html as html_lib
import os
import re
from collections import defaultdict
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

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
    public_text: str | None = Field(default=None, alias="publicText")

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
                publicText=item.public_text,
            )
        )

    invalid = [
        _max_result(item, None, None, "manual_required", "Из ссылки MAX не удалось достоверно извлечь ID поста.")
        for item in normalized
        if not item.message_id and not item.chat_id and not item.channel
    ]
    candidates = [item for item in normalized if item.message_id or item.chat_id or item.channel]
    if not candidates:
        return {"items": invalid}

    output = invalid[:]
    direct_candidates: list[MaxStatsItem] = []
    grouped: dict[str, list[MaxStatsItem]] = defaultdict(list)
    for item in candidates:
        if item.chat_id:
            grouped[f"chat:{item.chat_id}"].append(item)
        elif item.channel:
            grouped[f"channel:{item.channel.lower()}"].append(item)
        else:
            direct_candidates.append(item)

    for group in grouped.values():
        chat_id = group[0].chat_id
        if not chat_id and group[0].channel:
            chat = await _fetch_max_chat_by_link(group[0].channel, token)
            if chat["http_status"] == 429:
                output.extend([_max_result(item, None, None, "rate_limited", "MAX временно ограничил запросы.") for item in group])
                continue
            if chat["ok"]:
                chat_id = _extract_max_chat_id(chat["json"])
            elif chat["http_status"] in (401, 403):
                output.extend([
                    _max_result(item, None, None, "auth_required", "MAX не дал боту доступ к этому каналу. Проверьте, что бот добавлен в канал.")
                    for item in group
                ])
                continue

        if chat_id:
            with_chat_id = [_max_item_with_chat_id(item, chat_id) for item in group]
            with_chat_id = await _hydrate_max_public_texts(with_chat_id)
            scan = await _scan_max_chat(chat_id, with_chat_id, token)
            if scan["http_status"] == 429:
                output.extend([_max_result(item, None, None, "rate_limited", "MAX временно ограничил запросы.") for item in with_chat_id])
                continue
            if scan["ok"]:
                messages = scan["messages"]
                for item in with_chat_id:
                    message = _find_max_message(messages, item.url, str(item.message_id or ""), item.public_text)
                    if message:
                        output.append(_max_result_from_message(item, message))
                    elif item.message_id and not _is_max_public_channel_item(item):
                        direct_candidates.append(item)
                    else:
                        output.append(
                            _max_result(
                                item,
                                None,
                                None,
                                "manual_required",
                                _max_public_link_hint(item, checked_count=scan.get("checked_count"), used_public_text=bool(item.public_text)),
                            )
                        )
                continue

            if scan["http_status"] in (401, 403):
                output.extend([
                    _max_result(item, None, None, "auth_required", "MAX не дал боту доступ к сообщениям этого канала.")
                    for item in with_chat_id
                ])
                continue

            direct_candidates.extend(with_chat_id)
        else:
            direct_candidates.extend(group)

    output.extend(await _collect_max_direct_batch(direct_candidates, token))
    return {"items": sorted(output, key=lambda row: row.get("id") or 0)}


async def _collect_max_direct_batch(items: list[MaxStatsItem], token: str) -> list[dict[str, Any]]:
    valid = [item for item in items if item.message_id]
    if not valid:
        return [
            _max_result(item, None, None, "manual_required", _max_public_link_hint(item, used_public_text=bool(item.public_text)))
            for item in items
        ]

    output: list[dict[str, Any]] = []
    chunk_size = max(1, int(os.getenv("MAX_BATCH_CHUNK_SIZE", "100")))
    for start in range(0, len(valid), chunk_size):
        chunk = valid[start : start + chunk_size]
        data = await _fetch_max_messages([str(item.message_id) for item in chunk], token)
        if data["http_status"] == 429:
            output.extend([_max_result(item, None, None, "rate_limited", "MAX временно ограничил запросы.") for item in chunk])
            continue
        if not data["ok"]:
            status = _status_from_http(data["http_status"])
            output.extend([_max_result(item, None, None, status, _max_error_or_hint(item, data["error"])) for item in chunk])
            continue

        for item in chunk:
            message = _extract_max_message(data["json"], item.url, str(item.message_id), require_matching_message=True, public_text=item.public_text)
            if not message:
                single = await _fetch_max_message(str(item.message_id), token)
                if single["ok"]:
                    message = _extract_max_message(single["json"], item.url, str(item.message_id), require_matching_message=False, public_text=item.public_text)
                elif single["http_status"] in (401, 403, 404, 429):
                    output.append(_max_result(item, None, None, _status_from_http(single["http_status"]), _max_error_or_hint(item, single["error"])))
                    continue

            if not message:
                output.append(_max_result(item, None, None, "not_found", "MAX API не вернул сообщение для этой ссылки."))
                continue
            output.append(_max_result_from_message(item, message))

    return output


async def _fetch_max_chat_by_link(chat_link: str, token: str) -> dict[str, Any]:
    endpoint = f"https://platform-api2.max.ru/chats/{quote(chat_link.lstrip('@'), safe='')}"
    return await _max_get_json(endpoint, token)


async def _fetch_max_chat_messages(
    chat_id: str,
    token: str,
    cursor_ms: int | None = None,
    cursor_param: str = "from",
) -> dict[str, Any]:
    endpoint = "https://platform-api2.max.ru/messages"
    count = max(1, min(100, int(os.getenv("MAX_CHAT_SCAN_PAGE_SIZE", "100"))))
    params = {"chat_id": str(chat_id), "count": str(count)}
    if cursor_ms is not None:
        params[cursor_param] = str(cursor_ms)
    return await _max_get_json(endpoint, token, params=params)


async def _scan_max_chat(chat_id: str, items: list[MaxStatsItem], token: str) -> dict[str, Any]:
    primary = await _scan_max_chat_cursor(chat_id, items, token, "from")
    if not primary["ok"] or _all_max_items_found(primary["messages"], items):
        return primary

    secondary = await _scan_max_chat_cursor(chat_id, items, token, "to")
    if not secondary["ok"]:
        return primary

    messages = _merge_max_messages(primary["messages"] + secondary["messages"])
    return {
        "ok": True,
        "http_status": 200,
        "messages": messages,
        "checked_count": len(messages),
        "error": None,
    }


async def _scan_max_chat_cursor(chat_id: str, items: list[MaxStatsItem], token: str, cursor_param: str) -> dict[str, Any]:
    pages = max(1, int(os.getenv("MAX_CHAT_SCAN_PAGES", "20")))
    cursor_ms: int | None = None
    seen_pages: set[str] = set()
    messages: list[Any] = []

    for _ in range(pages):
        data = await _fetch_max_chat_messages(chat_id, token, cursor_ms, cursor_param)
        if not data["ok"]:
            return {
                "ok": False,
                "http_status": data["http_status"],
                "messages": _merge_max_messages(messages),
                "checked_count": len(_merge_max_messages(messages)),
                "error": data["error"],
            }

        page = _extract_max_messages_list(data["json"])
        if not page:
            break

        signature = "|".join(_max_message_identity(message) for message in page[:10])
        if signature in seen_pages:
            break
        seen_pages.add(signature)
        messages.extend(page)
        unique_messages = _merge_max_messages(messages)

        if _all_max_items_found(unique_messages, items):
            break

        oldest = _oldest_max_message_timestamp_ms(page)
        if oldest is None:
            break
        next_cursor = oldest - 1
        if cursor_ms is not None and next_cursor >= cursor_ms:
            break
        cursor_ms = next_cursor

    unique_messages = _merge_max_messages(messages)
    return {"ok": True, "http_status": 200, "messages": unique_messages, "checked_count": len(unique_messages), "error": None}


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


def _max_item_with_chat_id(item: MaxStatsItem, chat_id: str) -> MaxStatsItem:
    return MaxStatsItem(
        id=item.id,
        url=item.url,
        messageId=item.message_id,
        chatId=chat_id,
        channel=item.channel,
        publicText=item.public_text,
    )


async def _hydrate_max_public_texts(items: list[MaxStatsItem]) -> list[MaxStatsItem]:
    if not _max_public_page_fallback_enabled():
        return items

    candidates = [item for item in items if _is_max_public_channel_item(item) and not item.public_text]
    if not candidates:
        return items

    timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "25"))
    headers = {
        "accept": "text/html,application/xhtml+xml",
        "user-agent": "Gala View Report Collector MAX Bridge/1.0",
    }
    texts: dict[str, str | None] = {}
    async with httpx.AsyncClient(timeout=timeout, verify=_max_verify_tls(), follow_redirects=True, headers=headers) as client:
        for item in candidates:
            if item.url not in texts:
                texts[item.url] = await _fetch_max_public_text(client, item.url)

    hydrated: list[MaxStatsItem] = []
    for item in items:
        hydrated.append(
            MaxStatsItem(
                id=item.id,
                url=item.url,
                messageId=item.message_id,
                chatId=item.chat_id,
                channel=item.channel,
                publicText=item.public_text or texts.get(item.url),
            )
        )
    return hydrated


async def _fetch_max_public_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        response = await client.get(_max_public_http_url(url))
    except httpx.HTTPError:
        return None
    if not response.is_success:
        return None
    return _max_public_text_from_html(response.text)


def _max_public_http_url(url: str) -> str:
    value = url.strip()
    if not re.match(r"^https?://", value, re.I):
        value = f"https://{value}"
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
    if parsed.netloc.lower() == "web.max.ru":
        parsed = parsed._replace(netloc="max.ru")
    return urlunparse(parsed)


def _max_public_text_from_html(page: str) -> str | None:
    candidates: list[str] = []
    patterns = [
        r"<meta[^>]+(?:property|name)=[\"'](?:og:description|description|twitter:description)[\"'][^>]+content=[\"']([^\"']+)[\"']",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"'](?:og:description|description|twitter:description)[\"']",
        r"<span[^>]*>(.*?)</span>",
        r"<title[^>]*>(.*?)</title>",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, page, re.I | re.S):
            text = _clean_max_html_text(match.group(1))
            if len(text) >= 40:
                candidates.append(text)

    if not candidates:
        return None
    return max(candidates, key=len)[:4000]


def _clean_max_html_text(value: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_max_chat_id(payload: Any) -> str | None:
    root = payload if isinstance(payload, dict) else {}
    candidates = [
        root,
        root.get("chat") if isinstance(root, dict) else None,
        _dict_get(root, "data", "chat"),
        _dict_get(root, "response", "chat"),
        root.get("data") if isinstance(root, dict) else None,
        root.get("response") if isinstance(root, dict) else None,
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("chat_id") or candidate.get("chatId") or candidate.get("id")
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _extract_max_messages_list(payload: Any) -> list[Any]:
    root = payload if isinstance(payload, dict) else {}
    candidates = [
        root.get("messages") if isinstance(root, dict) else None,
        root.get("items") if isinstance(root, dict) else None,
        _dict_get(root, "data", "messages"),
        _dict_get(root, "data", "items"),
        _dict_get(root, "response", "messages"),
        _dict_get(root, "response", "items"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
    return []


def _merge_max_messages(messages: list[Any]) -> list[Any]:
    unique: dict[str, Any] = {}
    for message in messages:
        identity = _max_message_identity(message)
        if identity not in unique:
            unique[identity] = message
    return list(unique.values())


def _all_max_items_found(messages: list[Any], items: list[MaxStatsItem]) -> bool:
    return all(_find_max_message(messages, item.url, str(item.message_id or ""), item.public_text) for item in items)


def _find_max_message(messages: list[Any], url: str, message_id: str, public_text: str | None = None) -> Any | None:
    for message in messages:
        if _is_max_message_match(message, url, message_id, public_text):
            return message
    return None


def _oldest_max_message_timestamp_ms(messages: list[Any]) -> int | None:
    timestamps = [_max_message_timestamp_ms(message) for message in messages]
    usable = [timestamp for timestamp in timestamps if timestamp is not None]
    return min(usable) if usable else None


def _max_message_timestamp_ms(message: Any) -> int | None:
    timestamp = _first_int(
        message,
        [
            "timestamp",
            "time",
            "date",
            "created_at",
            "createdAt",
            "body.timestamp",
            "body.time",
            "body.date",
            "body.created_at",
            "body.createdAt",
        ],
    )
    if timestamp is None:
        return None
    return timestamp * 1000 if timestamp < 10**12 else timestamp


def _max_message_identity(message: Any) -> str:
    if not isinstance(message, dict):
        return str(hash(str(message)))
    fields = [
        message.get("id"),
        message.get("message_id"),
        message.get("messageId"),
        message.get("mid"),
        message.get("url"),
        message.get("link"),
        _dict_get(message, "body", "mid"),
        _dict_get(message, "body", "message_id"),
    ]
    return "|".join(str(value) for value in fields if value is not None) or str(_max_message_timestamp_ms(message) or "")


def _extract_max_message(
    payload: Any,
    url: str,
    message_id: str,
    require_matching_message: bool,
    public_text: str | None = None,
) -> Any | None:
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
            if _is_max_message_match(message, url, message_id, public_text):
                return message
        return None

    direct = root.get("message") or _dict_get(root, "data", "message") or _dict_get(root, "response", "message") or payload
    if not require_matching_message or _is_max_message_match(direct, url, message_id, public_text):
        return direct
    return None


def _is_max_message_match(message: Any, url: str, message_id: str, public_text: str | None = None) -> bool:
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
        and (_normalize_url_for_compare(value) == normalized or (bool(message_id) and message_id in value))
        for value in url_fields
    ) or (bool(message_id) and _payload_contains_string(message, message_id)) or _max_public_text_matches_message(message, public_text)


def _max_public_text_matches_message(message: Any, public_text: str | None) -> bool:
    if not public_text:
        return False

    public_normalized = _normalize_max_text(public_text)
    if len(public_normalized) < 40:
        return False

    payload_text = _normalize_max_text(" ".join(_max_text_strings(message)))
    if len(payload_text) < 40:
        return False

    for size in (240, 180, 120, 80):
        if len(public_normalized) >= size and public_normalized[:size] in payload_text:
            return True

    public_words = _significant_max_words(public_normalized)
    if len(public_words) < 10:
        return False
    for start in range(0, min(8, len(public_words) - 9)):
        needle = " ".join(public_words[start : start + 10])
        if needle and needle in payload_text:
            return True
    return False


def _max_text_strings(value: Any, depth: int = 0) -> list[str]:
    if depth > 5:
        return []
    if isinstance(value, str):
        cleaned = _clean_max_html_text(value)
        return [cleaned] if len(cleaned) >= 20 else []
    if isinstance(value, dict):
        output: list[str] = []
        for child in value.values():
            output.extend(_max_text_strings(child, depth + 1))
        return output[:200]
    if isinstance(value, list):
        output = []
        for child in value:
            output.extend(_max_text_strings(child, depth + 1))
        return output[:200]
    return []


def _normalize_max_text(value: str) -> str:
    text = html_lib.unescape(value).lower()
    text = text.replace("…", " ")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^0-9a-zа-яё]+", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _significant_max_words(value: str) -> list[str]:
    return [word for word in value.split() if len(word) >= 3]


def _payload_contains_string(value: Any, needle: str, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_payload_contains_string(child, needle, depth + 1) for child in value.values())
    if isinstance(value, list):
        return any(_payload_contains_string(child, needle, depth + 1) for child in value)
    return False


def _max_error_or_hint(item: MaxStatsItem, error: str | None) -> str:
    if error and "invalid message_id" not in error.lower():
        return error
    if item.channel or item.chat_id:
        return _max_public_link_hint(item, used_public_text=bool(item.public_text))
    return error or "MAX API не принял message_id из ссылки."


def _max_public_link_hint(
    item: MaxStatsItem,
    checked_count: int | None = None,
    used_public_text: bool = False,
) -> str:
    scan_pages = max(1, int(os.getenv("MAX_CHAT_SCAN_PAGES", "20")))
    page_size = max(1, min(100, int(os.getenv("MAX_CHAT_SCAN_PAGE_SIZE", "100"))))
    planned = scan_pages * page_size
    checked = f"{checked_count} реально полученных сообщений" if checked_count is not None else f"до {planned} последних сообщений"
    text_part = (
        " Также я сравнил текст публичной страницы с текстами сообщений из API."
        if used_public_text
        else ""
    )
    return (
        "MAX-ссылка содержит публичный код поста, а не внутренний message_id. "
        f"Я попробовал найти пост среди {checked} канала через chat_id, но MAX API не вернул совпадение.{text_part} "
        "Для точных цифр добавьте бота в канал с доступом к сообщениям или используйте MAX-ссылку формата /c/<chat_id>/<message_id>, если она доступна."
    )


def _is_max_public_channel_item(item: MaxStatsItem) -> bool:
    if not item.channel:
        return False
    try:
        parsed = urlparse(_max_public_http_url(item.url))
        parts = [part for part in parsed.path.split("/") if part]
    except ValueError:
        return False
    return len(parts) >= 2 and parts[0].lower() != "c"


def _max_public_page_fallback_enabled() -> bool:
    return os.getenv("MAX_PUBLIC_PAGE_TEXT_FALLBACK", "true").strip().lower() in {"1", "true", "yes", "on"}


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
