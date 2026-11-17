"""YouTube live chat reader against a scripted youtube.com (no network): the switch to the all-messages
view, polling, de-duplication, reconnects and the end of the stream."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from chat_yt_helpers import FakeYouTube, SleepRecorder, chat_response, load_actions

from sixstars.chat import youtube_innertube as innertube
from sixstars.chat import youtube_live
from sixstars.chat.models import ChatConnectionError, ChatMessage, ChatSourceError

URL = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
TOP = "FAKE_TOP_CHAT_TOKEN"


@pytest.fixture
def actions(fixtures_dir: Path) -> list[dict[str, Any]]:
    return load_actions(fixtures_dir / "youtube_live_sample.jsonl")


@pytest.fixture
def live_page(fixtures_dir: Path) -> str:
    return (fixtures_dir / "youtube_watch_live_var.html").read_text(encoding="utf-8")


def _count(actions: list[dict[str, Any]]) -> int:
    return sum(len(innertube.action_to_messages(action)) for action in actions)


async def _collect(stream: AsyncIterator[ChatMessage]) -> list[ChatMessage]:
    return [message async for message in stream]


def _happy_script(actions: list[dict[str, Any]]) -> dict[str, list[Any]]:
    return {
        TOP: [chat_response(actions[:5], "TOP_NEXT", views=("TOP_AGAIN", "ALL1"))],
        "ALL1": [chat_response(actions[:40], "C2", views=("TOP_AGAIN", "ALL1"), all_chat_selected=True)],
        "C2": [chat_response(actions[30:], "C3", timeout_ms=2000)],
        "C3": [chat_response([])],
    }


async def test_stream_switches_to_all_messages_and_ends_with_the_stream(
    actions: list[dict[str, Any]], live_page: str
) -> None:
    youtube = FakeYouTube([live_page], _happy_script(actions))
    sleep = SleepRecorder()
    messages = await _collect(youtube_live.stream(URL, transport=youtube.transport, sleep=sleep))
    # Batches overlap (actions 30-39 arrive twice); every message is yielded once.
    assert len(messages) == _count(actions) == 78
    assert youtube.calls[0] == ("watch", "AAAAAAAAAAA")
    assert youtube.continuations == [TOP, "ALL1", "C2", "C3"]
    assert {endpoint for endpoint, _ in youtube.calls[1:]} == {"get_live_chat"}
    # YouTube suggests 10 s; we cap the wait at 5 s, and follow a shorter suggestion.
    assert sleep.delays == [5.0, 2.0]
    assert all(message.platform == "youtube" and message.t > 1_700_000_000 for message in messages)


async def test_stream_can_skip_the_backlog(actions: list[dict[str, Any]], live_page: str) -> None:
    youtube = FakeYouTube([live_page], _happy_script(actions))
    stream = youtube_live.stream(
        URL, transport=youtube.transport, sleep=SleepRecorder(), include_backlog=False
    )
    messages = await _collect(stream)
    assert len(messages) == _count(actions[40:])


async def test_stream_uses_the_first_view_when_no_view_list_is_offered(
    actions: list[dict[str, Any]], live_page: str
) -> None:
    youtube = FakeYouTube([live_page], {TOP: [chat_response(actions[:10], "C2")], "C2": [chat_response([])]})
    messages = await _collect(youtube_live.stream(URL, transport=youtube.transport, sleep=SleepRecorder()))
    assert len(messages) == _count(actions[:10])
    assert youtube.continuations == [TOP, "C2"]


async def test_stream_reconnects_after_a_failed_poll_without_repeating_messages(
    actions: list[dict[str, Any]], live_page: str
) -> None:
    script = _happy_script(actions)
    script["C2"] = [503, chat_response(actions[30:], "C3")]
    youtube = FakeYouTube([live_page], script)
    sleep = SleepRecorder()
    messages = await _collect(youtube_live.stream(URL, transport=youtube.transport, sleep=sleep))
    assert len(messages) == 78
    assert [call for call in youtube.calls if call[0] == "watch"] == [("watch", "AAAAAAAAAAA")] * 2
    assert youtube.continuations == [TOP, "ALL1", "C2", TOP, "ALL1", "C2", "C3"]
    # poll wait, first backoff step, poll wait, poll wait
    assert sleep.delays == [5.0, 1.0, 5.0, 5.0]


async def test_stream_survives_a_network_error(actions: list[dict[str, Any]], live_page: str) -> None:
    script = _happy_script(actions)
    script["C2"] = [httpx.ReadTimeout("slow"), chat_response(actions[30:], "C3")]
    youtube = FakeYouTube([live_page], script)
    messages = await _collect(youtube_live.stream(URL, transport=youtube.transport, sleep=SleepRecorder()))
    assert len(messages) == 78


async def test_stream_gives_up_after_max_reconnects(actions: list[dict[str, Any]], live_page: str) -> None:
    script = _happy_script(actions)
    script["C2"] = [500]
    youtube = FakeYouTube([live_page], script)
    sleep = SleepRecorder()
    stream = youtube_live.stream(URL, transport=youtube.transport, sleep=sleep, max_reconnects=2)
    received: list[ChatMessage] = []
    with pytest.raises(ChatConnectionError, match="2 reconnects"):
        async for message in stream:
            received.append(message)
    assert len(received) == _count(actions[:40])
    # Reconnecting only replays known messages, so the failure count keeps growing: 1 s, then 2 s.
    assert sleep.delays == [5.0, 1.0, 5.0, 2.0, 5.0]


async def test_stream_ends_when_the_video_became_a_replay(
    actions: list[dict[str, Any]], live_page: str, fixtures_dir: Path
) -> None:
    replay_page = (fixtures_dir / "youtube_watch_replay.html").read_text(encoding="utf-8")
    script = _happy_script(actions)
    script["C2"] = [500]
    youtube = FakeYouTube([live_page, replay_page], script)
    messages = await _collect(youtube_live.stream(URL, transport=youtube.transport, sleep=SleepRecorder()))
    assert len(messages) == _count(actions[:40])


async def test_stream_ends_when_the_chat_returns_no_data(
    actions: list[dict[str, Any]], live_page: str
) -> None:
    script = _happy_script(actions)
    script["C2"] = [{"responseContext": {}}]
    youtube = FakeYouTube([live_page], script)
    messages = await _collect(youtube_live.stream(URL, transport=youtube.transport, sleep=SleepRecorder()))
    assert len(messages) == _count(actions[:40])


@pytest.mark.parametrize(
    "page_name,expected",
    [("youtube_watch_no_chat.html", "no live chat"), ("youtube_watch_replay.html", "the stream has ended")],
)
async def test_stream_raises_when_there_is_no_live_chat(
    fixtures_dir: Path, page_name: str, expected: str
) -> None:
    page = (fixtures_dir / page_name).read_text(encoding="utf-8")
    youtube = FakeYouTube([page], {})
    with pytest.raises(ChatSourceError, match=expected):
        await _collect(youtube_live.stream(URL, transport=youtube.transport, sleep=SleepRecorder()))
    assert youtube.calls == [("watch", "AAAAAAAAAAA")]


async def test_stream_raises_when_the_chat_cannot_be_opened(live_page: str) -> None:
    youtube = FakeYouTube([429], {})
    sleep = SleepRecorder()
    with pytest.raises(ChatSourceError, match="cannot open live chat.*HTTP 429"):
        await _collect(youtube_live.stream(URL, transport=youtube.transport, sleep=sleep))
    assert len(youtube.calls) == youtube_live.SETUP_ATTEMPTS
    assert sleep.delays == [1.0, 2.0]


async def test_stream_opens_after_a_failed_first_attempt(
    actions: list[dict[str, Any]], live_page: str
) -> None:
    youtube = FakeYouTube([httpx.ConnectError("refused"), live_page], _happy_script(actions))
    sleep = SleepRecorder()
    messages = await _collect(youtube_live.stream(URL, transport=youtube.transport, sleep=sleep))
    assert len(messages) == 78
    assert sleep.delays == [1.0, 5.0, 2.0]


async def test_stream_rejects_a_bad_reference() -> None:
    with pytest.raises(ValueError):
        await _collect(youtube_live.stream("not a video"))


def test_seen_ids_forget_the_oldest() -> None:
    seen = youtube_live._SeenIds(2)
    assert seen.add("a") and seen.add("b")
    assert not seen.add("a")
    assert seen.add("c")  # pushes "a" out
    assert seen.add("a")
    assert not seen.add("c")
