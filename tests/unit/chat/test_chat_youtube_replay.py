"""YouTube chat replay against a scripted youtube.com (no network), plus the yt-dlp fallback with a fake
yt-dlp process."""

import shutil
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from chat_yt_helpers import FakeYouTube, SleepRecorder, chat_response, load_actions

from sixstars.chat import youtube_replay
from sixstars.chat.models import ChatSourceError, ReplayChat

VIDEO_ID = "AAAAAAAAAAA"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
TOP = "FAKE_TOP_CHAT_TOKEN"
REPLAY_KIND = "liveChatReplayContinuationData"


@pytest.fixture
def actions(fixtures_dir: Path) -> list[dict[str, Any]]:
    return load_actions(fixtures_dir / "youtube_replay_sample.jsonl")


@pytest.fixture
def replay_page(fixtures_dir: Path) -> str:
    return (fixtures_dir / "youtube_watch_replay.html").read_text(encoding="utf-8")


def _script(actions: list[dict[str, Any]]) -> dict[str, list[Any]]:
    def page(part: list[dict[str, Any]], token: str | None) -> dict[str, Any]:
        return chat_response(part, token, timeout_ms=None, kind=REPLAY_KIND)

    first = chat_response(
        actions[:20], "TOP_NEXT", timeout_ms=None, kind=REPLAY_KIND, views=("TOP_AGAIN", "ALL1")
    )
    return {
        TOP: [first],
        "ALL1": [page(actions[:80], "C2")],
        "C2": [page(actions[70:150], "C3")],
        "C3": [page(actions[150:], None)],
    }


# --- primary path -----------------------------------------------------------------------------------


async def test_fetch_pages_through_the_whole_replay(actions: list[dict[str, Any]], replay_page: str) -> None:
    youtube = FakeYouTube([replay_page], _script(actions))
    chat = await youtube_replay.fetch(URL, transport=youtube.transport, sleep=SleepRecorder())
    assert chat is not None
    assert (chat.platform, chat.source_id, chat.duration_s) == ("youtube", VIDEO_ID, 19676.0)
    assert chat.title == "Example finished stream"
    # 194 actions: 9 are ticker items, and the overlapping page (70-79) is not counted twice.
    assert chat.message_count == 185
    assert Counter(message.kind for message in chat.messages) == {
        "chat": 171,
        "sub": 9,
        "donation": 2,
        "gift": 2,
        "system": 1,
    }
    times = [message.t for message in chat.messages]
    assert times == sorted(times) and times[-1] == 19406.187
    assert youtube.continuations == [TOP, "ALL1", "C2", "C3"]
    assert {endpoint for endpoint, _ in youtube.calls[1:]} == {"get_live_chat_replay"}


async def test_fetch_returns_none_for_a_video_without_chat(fixtures_dir: Path) -> None:
    page = (fixtures_dir / "youtube_watch_no_chat.html").read_text(encoding="utf-8")
    youtube = FakeYouTube([page], {})
    assert await youtube_replay.fetch(VIDEO_ID, transport=youtube.transport) is None
    assert youtube.calls == [("watch", VIDEO_ID)]


async def test_fetch_refuses_a_stream_that_is_still_live(fixtures_dir: Path) -> None:
    page = (fixtures_dir / "youtube_watch_live_var.html").read_text(encoding="utf-8")
    youtube = FakeYouTube([page], {})
    with pytest.raises(youtube_replay.VideoUnavailableError, match="still live"):
        await youtube_replay.fetch(VIDEO_ID, transport=youtube.transport)


async def test_fetch_reports_an_unplayable_video() -> None:
    page = (
        '<script>ytcfg.set({"INNERTUBE_CLIENT_VERSION":"2.1"});'
        'var ytInitialPlayerResponse = {"playabilityStatus": '
        '{"status": "ERROR", "reason": "Video unavailable"}};'
        'var ytInitialData = {"contents": {}};</script>'
    )
    youtube = FakeYouTube([page], {})
    with pytest.raises(youtube_replay.VideoUnavailableError, match="Video unavailable"):
        await youtube_replay.fetch(VIDEO_ID, transport=youtube.transport)


async def test_fetch_retries_failed_requests(actions: list[dict[str, Any]], replay_page: str) -> None:
    script = _script(actions)
    script["C2"] = [500, httpx.ReadTimeout("slow"), script["C2"][0]]
    youtube = FakeYouTube([503, replay_page], script)
    sleep = SleepRecorder()
    chat = await youtube_replay.fetch(URL, transport=youtube.transport, sleep=sleep)
    assert chat is not None and chat.message_count == 185
    assert sleep.delays == [1.0, 1.0, 3.0]
    assert youtube.continuations == [TOP, "ALL1", "C2", "C2", "C2", "C3"]


async def test_fetch_raises_when_a_request_keeps_failing(
    actions: list[dict[str, Any]], replay_page: str
) -> None:
    script = _script(actions)
    script["C2"] = [429]
    youtube = FakeYouTube([replay_page], script)
    with pytest.raises(ChatSourceError, match="HTTP 429"):
        await youtube_replay.fetch(URL, transport=youtube.transport, sleep=SleepRecorder())
    assert youtube.continuations.count("C2") == youtube_replay.REQUEST_ATTEMPTS


async def test_fetch_stops_at_the_overall_timeout(actions: list[dict[str, Any]], replay_page: str) -> None:
    youtube = FakeYouTube([replay_page], _script(actions))
    with pytest.raises(ChatSourceError, match="timed out"):
        await youtube_replay.fetch(URL, transport=youtube.transport, timeout_s=-1.0)


async def test_fetch_treats_a_response_without_chat_as_the_end(
    actions: list[dict[str, Any]], replay_page: str
) -> None:
    script = _script(actions)
    script["C2"] = [{"responseContext": {}}]
    youtube = FakeYouTube([replay_page], script)
    chat = await youtube_replay.fetch(URL, transport=youtube.transport)
    assert chat is not None and chat.message_count == len(youtube_replay.messages_from_actions(actions[:80]))


def test_parse_actions_file_matches_the_api_path(fixtures_dir: Path, tmp_path: Path) -> None:
    messages = youtube_replay.parse_actions_file(fixtures_dir / "youtube_replay_sample.jsonl")
    assert len(messages) == 185
    bad = tmp_path / "bad.json"
    bad.write_text('{"ok": 1}\n\nnot json\n', encoding="utf-8")
    with pytest.raises(ChatSourceError, match="not JSON"):
        youtube_replay.parse_actions_file(bad)


# --- yt-dlp fallback --------------------------------------------------------------------------------


class FakeYtDlp:
    """Stands in for the yt-dlp process: optionally writes the chat file where yt-dlp would."""

    def __init__(self, sample: Path | None, code: int = 0, stdout: str = "19675\n", stderr: str = "") -> None:
        self.sample = sample
        self.result = (code, stdout, stderr)
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        self.calls.append((list(command), timeout_s))
        if self.sample is not None:
            template = command[command.index("--output") + 1]
            shutil.copyfile(self.sample, template.replace("%(id)s.%(ext)s", f"{VIDEO_ID}.live_chat.json"))
        return self.result


def test_ytdlp_fallback_parses_the_live_chat_file(fixtures_dir: Path) -> None:
    ytdlp = FakeYtDlp(fixtures_dir / "youtube_replay_sample.jsonl")
    chat = youtube_replay.download_with_ytdlp(URL, runner=ytdlp, timeout_s=40.0)
    assert chat is not None
    assert (chat.source_id, chat.message_count, chat.duration_s, chat.title) == (VIDEO_ID, 185, 19675.0, None)
    command, timeout_s = ytdlp.calls[0]
    assert timeout_s == 40.0
    assert command[1:3] == ["-m", "yt_dlp"]
    assert command[command.index("--sub-langs") + 1] == "live_chat"
    assert "--skip-download" in command and "--write-subs" in command and "--no-progress" in command
    assert command[-1] == URL
    assert "--proxy" not in command and "--cookies" not in command


def test_ytdlp_command_takes_proxy_and_cookies(tmp_path: Path) -> None:
    (tmp_path / "c.txt").write_text("# Netscape HTTP Cookie File", encoding="utf-8")
    command = youtube_replay.build_ytdlp_command(VIDEO_ID, tmp_path, "http://u:p@proxy:1", tmp_path / "c.txt")
    assert command[command.index("--proxy") + 1] == "http://u:p@proxy:1"
    assert command[command.index("--cookies") + 1] == str(tmp_path / "c.txt")


def test_ytdlp_fallback_returns_none_when_no_chat_file_is_written() -> None:
    assert youtube_replay.download_with_ytdlp(URL, runner=FakeYtDlp(None, stdout="213\n")) is None


def test_ytdlp_fallback_raises_on_failure() -> None:
    ytdlp = FakeYtDlp(None, code=1, stderr="ERROR: [youtube] AAAAAAAAAAA: Video unavailable")
    with pytest.raises(ChatSourceError, match="Video unavailable"):
        youtube_replay.download_with_ytdlp(URL, runner=ytdlp)


def test_ytdlp_fallback_tolerates_an_unknown_duration(fixtures_dir: Path) -> None:
    ytdlp = FakeYtDlp(fixtures_dir / "youtube_replay_sample.jsonl", stdout="NA\n")
    chat = youtube_replay.download_with_ytdlp(URL, runner=ytdlp)
    assert chat is not None and chat.duration_s is None


# --- blocking wrapper -------------------------------------------------------------------------------


def _replay() -> ReplayChat:
    return ReplayChat(platform="youtube", source_id=VIDEO_ID, messages=[])


def test_download_returns_the_primary_result(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _replay()
    seen: dict[str, Any] = {}

    async def fake_fetch(video: str, **kwargs: Any) -> ReplayChat | None:
        seen.update(kwargs, video=video)
        return expected

    monkeypatch.setattr(youtube_replay, "fetch", fake_fetch)
    assert youtube_replay.download(URL, proxy="http://proxy:1", timeout_s=5.0) is expected
    assert seen == {"video": URL, "proxy": "http://proxy:1", "cookies_file": None, "timeout_s": 5.0}


def test_download_falls_back_to_ytdlp_on_a_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _replay()

    async def failing_fetch(video: str, **kwargs: Any) -> ReplayChat | None:
        raise ChatSourceError("YouTube did not return a usable watch page")

    monkeypatch.setattr(youtube_replay, "fetch", failing_fetch)
    monkeypatch.setattr(youtube_replay, "download_with_ytdlp", lambda video, **kwargs: expected)
    assert youtube_replay.download(URL) is expected
    with pytest.raises(ChatSourceError, match="usable watch page"):
        youtube_replay.download(URL, use_fallback=False)


def test_download_does_not_fall_back_for_an_unavailable_video(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_fetch(video: str, **kwargs: Any) -> ReplayChat | None:
        raise youtube_replay.VideoUnavailableError("still live")

    def no_fallback(video: str, **kwargs: Any) -> ReplayChat | None:
        raise AssertionError("yt-dlp must not be tried")

    monkeypatch.setattr(youtube_replay, "fetch", failing_fetch)
    monkeypatch.setattr(youtube_replay, "download_with_ytdlp", no_fallback)
    with pytest.raises(youtube_replay.VideoUnavailableError):
        youtube_replay.download(URL)
