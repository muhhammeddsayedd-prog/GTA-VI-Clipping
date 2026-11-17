"""`ChatMessage`, `ReplayChat` and the JSON Lines helpers."""

from pathlib import Path

import pytest

from sixstars.chat.models import ChatMessage, ReplayChat, iter_jsonl, read_jsonl, sort_messages, write_jsonl


def _msg(t: float, user: str = "u1", text: str = "hi") -> ChatMessage:
    return ChatMessage(platform="twitch", t=t, user_id=user, username=user, text=text, kind="chat")


def test_defaults_and_immutability() -> None:
    message = _msg(1.0)
    assert message.is_mod is False
    assert message.is_broadcaster is False
    with pytest.raises(AttributeError):
        message.text = "changed"  # type: ignore[misc]


def test_dict_round_trip() -> None:
    message = ChatMessage("youtube", 12.5, "UC1", "@Name", "LOL 💀", "donation", is_mod=True)
    assert ChatMessage.from_dict(message.to_dict()) == message


def test_from_dict_fills_optional_flags() -> None:
    data = {"platform": "twitch", "t": 3, "user_id": 7, "username": "a", "text": "x", "kind": "chat"}
    message = ChatMessage.from_dict(data)
    assert message == ChatMessage("twitch", 3.0, "7", "a", "x", "chat")


@pytest.mark.parametrize("field,value", [("platform", "kick"), ("kind", "whisper")])
def test_from_dict_rejects_unknown_values(field: str, value: str) -> None:
    data = _msg(1.0).to_dict() | {field: value}
    with pytest.raises(ValueError, match=value):
        ChatMessage.from_dict(data)


def test_sort_is_stable() -> None:
    messages = [_msg(5.0, "a"), _msg(1.0, "b"), _msg(5.0, "c"), _msg(1.0, "d")]
    assert [m.user_id for m in sort_messages(messages)] == ["b", "d", "a", "c"]


def test_jsonl_round_trip(tmp_path: Path) -> None:
    messages = [_msg(1.0, "a", "KEKW"), _msg(2.0, "b", "ça va 😂")]
    path = tmp_path / "chat.jsonl"
    assert write_jsonl(path, messages) == 2
    assert read_jsonl(path) == messages
    assert path.read_bytes().count(b"\n") == 2
    assert b"\r" not in path.read_bytes()


def test_iter_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "chat.jsonl"
    write_jsonl(path, [_msg(1.0)])
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert len(list(iter_jsonl(path))) == 1


def test_replay_chat_message_count() -> None:
    chat = ReplayChat(platform="twitch", source_id="1", messages=[_msg(1.0), _msg(2.0)], duration_s=10.0)
    assert chat.message_count == 2
    assert chat.title is None
