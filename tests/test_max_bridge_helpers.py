import pathlib
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import (
    MaxStatsItem,
    _extract_max_chat_id,
    _extract_max_messages_list,
    _is_max_message_match,
    _max_chat_id_from_env,
    _max_chat_ids_from_updates,
    _max_result_from_message,
    _max_result_from_user_message,
    _scan_max_user_chat,
    _max_public_text_from_html,
    _max_user_session_configured,
    _max_user_session_json,
    _max_error_or_hint,
    _max_message_timestamp_ms,
    _parse_max_url,
)


def test_parse_public_max_url():
    message_id, chat_id, channel = _parse_max_url("https://max.ru/NeoficialniyBeZsonoV/AZ3OwQ_cBH4")

    assert message_id == "AZ3OwQ_cBH4"
    assert chat_id is None
    assert channel == "NeoficialniyBeZsonoV"


def test_extract_chat_id_from_nested_payload():
    assert _extract_max_chat_id({"chat": {"chat_id": 12345}}) == "12345"
    assert _extract_max_chat_id({"response": {"chat": {"id": "-777"}}}) == "-777"


def test_extract_messages_list_from_known_shapes():
    assert _extract_max_messages_list({"messages": [{"id": 1}]}) == [{"id": 1}]
    assert _extract_max_messages_list({"data": {"items": [{"id": 2}]}}) == [{"id": 2}]


def test_match_public_code_inside_message_url():
    message = {"url": "https://max.ru/NeoficialniyBeZsonoV/AZ3OwQ_cBH4", "stat": {"views": 10}}

    assert _is_max_message_match(message, "https://max.ru/NeoficialniyBeZsonoV/AZ3OwQ_cBH4", "AZ3OwQ_cBH4")


def test_match_public_max_post_by_text():
    public_text = "#НашиРебята Алексей, позывной «Будулай», доброволец из Оренбургской области. История о простом русском герое."
    message = {
        "body": {
            "text": "#НашиРебята Алексей, позывной Будулай, доброволец из Оренбургской области. История о простом русском герое.",
        },
        "stat": {"views": 123, "reactions": 7},
    }

    assert _is_max_message_match(message, "https://max.ru/NeoficialniyBeZsonoV/AZ3OwQ_cBH4", "AZ3OwQ_cBH4", public_text)


def test_extract_public_text_from_max_html():
    html = '<meta property="og:description" content="#НашиРебята Алексей и Оксана рассказывают о работе в тылу ради победы.">'

    assert _max_public_text_from_html(html) == "#НашиРебята Алексей и Оксана рассказывают о работе в тылу ради победы."


def test_max_chat_id_from_env(monkeypatch):
    monkeypatch.setenv("MAX_CHANNEL_CHAT_IDS", "Other:111, @NeoficialniyBeZsonoV:-222")

    assert _max_chat_id_from_env("neoficialniybezsonov") == "-222"


def test_extract_chat_id_from_update_matching_public_text():
    item = MaxStatsItem(
        id=1,
        url="https://max.ru/NeoficialniyBeZsonoV/AZ3OwQ_cBH4",
        messageId="AZ3OwQ_cBH4",
        channel="NeoficialniyBeZsonoV",
        publicText="Алексей, позывной Будулай, доброволец из Оренбургской области. История о простом русском герое.",
    )
    payload = {
        "updates": [
            {
                "update_type": "message_created",
                "message": {
                    "recipient": {"chat_id": "-333"},
                    "body": {"text": "Алексей, позывной Будулай, доброволец из Оренбургской области. История о простом русском герое."},
                },
            }
        ]
    }

    assert _max_chat_ids_from_updates(payload, [item]) == ["-333"]


def test_invalid_message_id_becomes_public_link_hint():
    item = MaxStatsItem(id=1, url="https://max.ru/NeoficialniyBeZsonoV/AZ3OwQ_cBH4", messageId="AZ3OwQ_cBH4", channel="NeoficialniyBeZsonoV")

    hint = _max_error_or_hint(item, "Invalid message_id: AZ3OwQ_cBH4")

    assert "message_id" in hint
    assert "chat_id" in hint


def test_second_timestamp_normalized_to_ms():
    assert _max_message_timestamp_ms({"timestamp": 1_700_000_000}) == 1_700_000_000_000


def test_max_user_session_b64(monkeypatch):
    monkeypatch.setenv("MAX_USER_SESSION_JSON_B64", "eyJ0b2tlbiI6ICJhYmMifQ==")
    monkeypatch.delenv("MAX_USER_SESSION_JSON", raising=False)

    assert _max_user_session_configured() is True
    assert _max_user_session_json() == '{"token": "abc"}'


def test_max_result_from_pymax_like_message():
    class FakeMessage:
        def model_dump(self, **kwargs):
            return {
                "id": 42,
                "chat_id": -100,
                "stats": {"views": 123456},
                "reaction_info": {"total_count": 789},
            }

    item = MaxStatsItem(
        id=1,
        url="https://max.ru/example/abc",
        messageId="abc",
        channel="example",
    )

    result = _max_result_from_message(item, FakeMessage(), source="max_bridge_user_session")

    assert result["views"] == 123456
    assert result["reactions"] == 789
    assert result["status"] == "ok"
    assert result["source"] == "max_bridge_user_session"


def test_scan_max_user_chat_uses_pymax_history_signature(monkeypatch):
    monkeypatch.setenv("MAX_USER_SCAN_PAGES", "1")
    monkeypatch.setenv("MAX_USER_SCAN_PAGE_SIZE", "10")

    class FakeClient:
        async def fetch_history(
            self,
            chat_id,
            forward=0,
            backward=40,
            backward_time=0,
            forward_time=0,
            from_time=None,
            get_chat=False,
            get_messages=True,
            interactive=False,
        ):
            assert chat_id == -100
            assert backward == 10
            assert from_time is None
            assert get_chat is False
            assert get_messages is True
            assert interactive is False
            return [
                {
                    "id": 42,
                    "chat_id": -100,
                    "time": 1_700_000_000_000,
                    "text": "MAX post body with enough words for matching",
                    "stats": {"views": 321},
                    "reaction_info": {"total_count": 12},
                    "url": "https://max.ru/example/abc123",
                }
            ]

    item = MaxStatsItem(
        id=1,
        url="https://max.ru/example/abc123",
        messageId="abc123",
        channel="example",
    )

    result = asyncio.run(_scan_max_user_chat(FakeClient(), "-100", [item]))

    assert result["ok"] is True
    assert result["checked_count"] == 1
    assert result["messages"][0]["stats"]["views"] == 321


def test_max_user_message_result_fetches_stats_and_reactions():
    class FakeApp:
        async def invoke(self, opcode, payload):
            assert int(opcode) == 74
            assert payload == {"chatId": -100, "messageIds": [42]}
            return SimpleNamespace(payload={"stats": {"42": {"views": 654321}}})

    class FakeClient:
        _app = FakeApp()

        async def get_reactions(self, chat_id, message_ids):
            assert chat_id == -100
            assert message_ids == [42]
            return {"42": {"total_count": 987}}

    item = MaxStatsItem(
        id=1,
        url="https://max.ru/example/abc123",
        messageId="abc123",
        channel="example",
    )
    message = {"id": 42, "chat_id": -100, "text": "MAX post body"}

    result = asyncio.run(_max_result_from_user_message(FakeClient(), item, "-100", message))

    assert result["views"] == 654321
    assert result["reactions"] == 987
    assert result["status"] == "ok"
    assert result["source"] == "max_bridge_user_session"
