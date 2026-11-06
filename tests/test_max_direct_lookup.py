import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import app as bridge


def post(code="AaGrJwbKSO4", channel="example", row_id=1):
    return bridge.MaxStatsItem(id=row_id, url=f"https://max.ru/{channel}/{code}", channel=channel, messageId=code)


def test_public_message_code_is_an_exact_64_bit_id():
    assert bridge._max_public_message_id("AaGrJwbKSO4") == 117563249394338030
    assert bridge._max_public_message_id("AZ3OwQ_cBH4") == 116476493971588222
    assert bridge._max_public_message_id("117563249394338030") == 117563249394338030


@pytest.mark.parametrize("code", [None, "", "abc", "AaGrJwbKSO!", "AaGrJwbKSO5", "AAAAAAAAAAA", "__________8", "0", "-1", str(2**63)])
def test_malformed_public_codes_are_not_guessed(code):
    assert bridge._max_public_message_id(code) is None


def fake_client(views=35683, reactions=211):
    async def messages(chat_id, message_ids):
        return [{"id": mid, "chat_id": chat_id, "text": "Название поста\nПолный текст"} for mid in message_ids]

    async def stats(opcode, payload):
        assert int(opcode) == 74
        return SimpleNamespace(payload={"stats": {str(mid): {"views": views} for mid in payload["messageIds"]}})

    async def reaction_counts(chat_id, message_ids):
        return {str(mid): SimpleNamespace(model_dump=lambda **kwargs: {"total_count": reactions}) for mid in message_ids}

    return SimpleNamespace(get_messages=AsyncMock(side_effect=messages), get_reactions=AsyncMock(side_effect=reaction_counts), _app=SimpleNamespace(invoke=AsyncMock(side_effect=stats)))


def test_public_links_skip_html_and_history_and_keep_titles(monkeypatch):
    client = fake_client()
    monkeypatch.setattr(bridge, "_get_max_user_client", AsyncMock(return_value=client))
    monkeypatch.setattr(bridge, "_resolve_max_user_chat_id", AsyncMock(return_value="-100"))
    hydrate = AsyncMock(side_effect=AssertionError("Public HTML must not be needed"))
    scan = AsyncMock(side_effect=AssertionError("History must not be needed"))
    monkeypatch.setattr(bridge, "_hydrate_max_public_texts", hydrate)
    monkeypatch.setattr(bridge, "_scan_max_user_chat", scan)
    rows = asyncio.run(bridge._collect_max_user_session_batch([post(), post("AZ3OwQ_cBH4", row_id=2)]))
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "ok"
        assert row["views"] == 35683
        assert row["reactions"] == 211
        assert row["precision"] == "exact"
        assert row["title"] == "Название поста"
    assert client.get_messages.await_count == 1
    assert client.get_reactions.await_count == 1
    assert client._app.invoke.await_count == 1
    hydrate.assert_not_awaited()
    scan.assert_not_awaited()


def test_large_channel_uses_chunks_not_requests_per_post():
    client = fake_client()
    items = [post(base64.urlsafe_b64encode(mid.to_bytes(8, "big")).decode().rstrip("="), row_id=mid) for mid in range(100, 220)]
    rows = asyncio.run(bridge._collect_max_user_direct(client, "-100", items))
    assert len(rows) == 120
    assert all(row["status"] == "ok" for row in rows)
    assert client.get_messages.await_count == 3
    assert client.get_reactions.await_count == 3
    assert client._app.invoke.await_count == 3


@pytest.mark.parametrize("message", [{"id": 42, "chat_id": -100}, {"id": 117563249394338030, "chat_id": -999}])
def test_wrong_post_or_channel_is_rejected_not_reported_as_zero(message):
    client = fake_client()
    client.get_messages = AsyncMock(return_value=[message])
    row = asyncio.run(bridge._collect_max_user_direct(client, "-100", [post()]))[0]
    assert row["status"] == "not_found"
    assert row["views"] is None
    assert row["reactions"] is None
    client._app.invoke.assert_not_awaited()


def test_failure_of_one_channel_does_not_break_other_channels(monkeypatch):
    client = fake_client()
    monkeypatch.setattr(bridge, "_get_max_user_client", AsyncMock(return_value=client))

    async def resolve(client, item):
        if item.channel == "broken":
            raise TimeoutError()
        return "-100"

    monkeypatch.setattr(bridge, "_resolve_max_user_chat_id", resolve)
    rows = asyncio.run(bridge._collect_max_user_session_batch([post(channel="broken"), post(channel="working", row_id=2)]))
    assert rows[0]["views"] is None
    assert rows[0]["status"] == "manual_required"
    assert rows[1]["status"] == "ok"


def test_user_session_error_does_not_start_unavailable_bot_fallback(monkeypatch):
    monkeypatch.setattr(bridge, "_max_user_session_configured", lambda: True)
    monkeypatch.setattr(bridge, "_collect_max_user_session_batch", AsyncMock(return_value=[bridge._max_result(post(), None, None, "not_found", "Пост не найден")]))
    bot = AsyncMock(side_effect=AssertionError("No bot fallback for public channel links"))
    monkeypatch.setattr(bridge, "_collect_max_direct_batch", bot)
    row = asyncio.run(bridge._collect_max_batch([post()], "test-token"))["items"][0]
    assert row["status"] == "not_found"
    assert row["views"] is None
    bot.assert_not_awaited()


def test_missing_counters_stay_blank_but_real_zero_is_preserved():
    client = fake_client(views=0, reactions=0)
    row = asyncio.run(bridge._collect_max_user_direct(client, "-100", [post()]))[0]
    assert row["status"] == "ok"
    assert row["views"] == 0
    client._app.invoke = AsyncMock(side_effect=TimeoutError())
    client.get_reactions = AsyncMock(side_effect=TimeoutError())
    row = asyncio.run(bridge._collect_max_user_direct(client, "-100", [post()]))[0]
    assert row["status"] == "manual_required"
    assert row["views"] is None
    assert row["reactions"] is None
