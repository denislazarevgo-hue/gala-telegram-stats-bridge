import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import (
    MaxStatsItem,
    _extract_max_chat_id,
    _extract_max_messages_list,
    _is_max_message_match,
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


def test_invalid_message_id_becomes_public_link_hint():
    item = MaxStatsItem(id=1, url="https://max.ru/NeoficialniyBeZsonoV/AZ3OwQ_cBH4", messageId="AZ3OwQ_cBH4", channel="NeoficialniyBeZsonoV")

    hint = _max_error_or_hint(item, "Invalid message_id: AZ3OwQ_cBH4")

    assert "message_id" in hint
    assert "chat_id" in hint


def test_second_timestamp_normalized_to_ms():
    assert _max_message_timestamp_ms({"timestamp": 1_700_000_000}) == 1_700_000_000_000
