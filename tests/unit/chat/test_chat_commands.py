"""The `sixstars chat` commands, the package entry points and the proxy decision, with the adapters
replaced by fakes (no network, no database)."""

import asyncio
from collections import Counter
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import structlog
from typer.testing import CliRunner

from sixstars import chat, config, settings
from sixstars.chat import (
    commands,
    net,
    twitch_irc,
    twitch_replay,
    youtube_innertube,
    youtube_live,
    youtube_replay,
)
from sixstars.chat.models import ChatMessage, ChatSourceError, ReplayChat, read_jsonl
from sixstars.cli import app

runner = CliRunner()

YOUTUBE_URL = "https://www.youtube.com/watch?v=AAAAAAAAAAA"


def _msg(t: float, user: str = "viewer", text: str = "LMAO", **extra: Any) -> ChatMessage:
    return ChatMessage(platform="twitch", t=t, user_id=user, username=user, text=text, kind="chat", **extra)


def _fake_stream(messages: list[ChatMessage], seen: dict[str, Any]) -> Any:
    async def stream(source: str, **kwargs: Any) -> AsyncGenerator[ChatMessage, None]:
        seen.update(kwargs, source=source)
        for message in messages:
            yield message

    return stream


# --- formatting -------------------------------------------------------------------------------------


def test_format_message() -> None:
    plain = _msg(1_700_000_000.0, "Viewer", "hello chat")
    assert commands.format_message(plain) == "22:13:20 [chat] Viewer: hello chat"
    flagged = ChatMessage(
        "youtube", 3661.0, "UC1", "@Mod", "$5.00", "donation", is_mod=True, is_broadcaster=True
    )
    assert commands.format_message(flagged) == "01:01:01 [donation/BM] @Mod: $5.00"


def test_summary_line() -> None:
    kinds = Counter({"chat": 30, "sub": 2})
    assert commands.summary_line(kinds, 12, 60.0) == (
        "32 messages in 60 s (chat=30, sub=2); 12 users; 5.0 chat messages per 10 s"
    )
    assert (
        commands.summary_line(Counter(), 0, 0.0)
        == "0 messages in 0 s (none); 0 users; 0.0 chat messages per 10 s"
    )


# --- chat dump --------------------------------------------------------------------------------------


def test_chat_help_lists_both_commands() -> None:
    result = runner.invoke(app, ["chat", "--help"])
    assert result.exit_code == 0
    assert "dump" in result.output and "replay" in result.output


def test_dump_prints_twitch_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    messages = [_msg(1_700_000_000.0, "a", "first"), _msg(1_700_000_001.0, "b", "second", is_mod=True)]
    monkeypatch.setattr(twitch_irc, "stream", _fake_stream(messages, seen))
    result = runner.invoke(app, ["chat", "dump", "SomeChannel", "--seconds", "5"])
    assert result.exit_code == 0
    assert "22:13:20 [chat] a: first" in result.output
    assert "22:13:21 [chat/M] b: second" in result.output
    assert "2 messages" in result.output and "2 users" in result.output
    assert seen == {"source": "SomeChannel", "proxy": None, "max_reconnects": 3}


def test_dump_marks_youtube_backlog_and_counts_only_new_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    old = _msg(1_000.0, "@old", "from before")
    new = _msg(4_000_000_000.0, "@new", "just now")
    monkeypatch.setattr(youtube_live, "stream", _fake_stream([old, new], seen))
    result = runner.invoke(app, ["chat", "dump", YOUTUBE_URL])
    assert result.exit_code == 0
    assert "(earlier) 00:16:40 [chat] @old: from before" in result.output
    assert "1 messages" in result.output
    assert seen["source"] == YOUTUBE_URL and seen["cookies_file"] is None


def test_dump_reports_chat_errors_and_bad_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(source: str, **kwargs: Any) -> AsyncGenerator[ChatMessage, None]:
        raise ChatSourceError("no live chat for YouTube video AAAAAAAAAAA")
        yield  # pragma: no cover

    monkeypatch.setattr(youtube_live, "stream", failing)
    result = runner.invoke(app, ["chat", "dump", YOUTUBE_URL])
    assert result.exit_code == commands.EXIT_ERROR
    assert "chat failed: no live chat" in result.output
    bad = runner.invoke(app, ["chat", "dump", "not a login"])
    assert bad.exit_code == commands.EXIT_USAGE
    assert "not a Twitch login" in bad.output


# --- chat replay ------------------------------------------------------------------------------------


def test_replay_writes_jsonl_for_a_twitch_vod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    messages = [_msg(10.0, "a"), _msg(20.0, "b", "😂")]

    def fake_download(vod: str, **kwargs: Any) -> ReplayChat:
        seen.update(kwargs, vod=vod)
        return ReplayChat(platform="twitch", source_id="123", messages=messages, duration_s=3600.0)

    monkeypatch.setattr(twitch_replay, "download", fake_download)
    out = tmp_path / "sub" / "chat.jsonl"
    result = runner.invoke(app, ["chat", "replay", "v123", "-o", str(out)])
    assert result.exit_code == 0
    assert "twitch 123: 2 messages (chat=2), video length 3600 s" in result.output
    assert read_jsonl(out) == messages
    assert seen == {"vod": "v123", "proxy": None}


@pytest.mark.parametrize("source", [YOUTUBE_URL, "AAAAAAAAAAA", "https://youtu.be/AAAAAAAAAAA"])
def test_replay_routes_youtube_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> None:
    def fake_download(video: str, **kwargs: Any) -> ReplayChat:
        return ReplayChat(platform="youtube", source_id="AAAAAAAAAAA", messages=[_msg(1.0)])

    monkeypatch.setattr(youtube_replay, "download", fake_download)
    result = runner.invoke(app, ["chat", "replay", source, "--output", str(tmp_path / "chat.jsonl")])
    assert result.exit_code == 0
    assert "youtube AAAAAAAAAAA: 1 messages (chat=1), video length unknown" in result.output


def test_replay_routes_twitch_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        twitch_replay, "download", lambda vod, **kwargs: ReplayChat("twitch", "9", [], duration_s=1.0)
    )
    args = ["chat", "replay", "https://www.twitch.tv/videos/9", "-o", str(tmp_path / "chat.jsonl")]
    assert "twitch 9: 0 messages (none)" in runner.invoke(app, args).output


def test_replay_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = str(tmp_path / "chat.jsonl")
    monkeypatch.setattr(youtube_replay, "download", lambda video, **kwargs: None)
    no_chat = runner.invoke(app, ["chat", "replay", YOUTUBE_URL, "-o", out])
    assert no_chat.exit_code == commands.EXIT_NO_CHAT
    assert "no chat replay" in no_chat.output
    assert not Path(out).exists()

    def failing(video: str, **kwargs: Any) -> ReplayChat:
        raise ChatSourceError("YouTube watch page returned HTTP 429")

    monkeypatch.setattr(youtube_replay, "download", failing)
    failed = runner.invoke(app, ["chat", "replay", YOUTUBE_URL, "-o", out])
    assert failed.exit_code == commands.EXIT_ERROR
    assert "HTTP 429" in failed.output
    monkeypatch.undo()
    bad = runner.invoke(app, ["chat", "replay", "not-a-video-or-vod", "-o", out])
    assert bad.exit_code == commands.EXIT_USAGE
    assert "not a YouTube video id or URL" in bad.output


# --- proxy and package entry points -----------------------------------------------------------------


def test_no_proxy_means_no_database_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_settings(key: str) -> list[Any]:
        raise AssertionError("settings must not be read when PROXY_URL is unset")

    monkeypatch.setattr(settings, "get_list", no_settings)
    assert net.proxy_for("youtube") is None
    assert net.proxy_for("twitch") is None


def test_proxy_applies_only_to_the_configured_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    from sixstars import config

    monkeypatch.setenv("PROXY_URL", "http://user:pass@proxy:8080")
    config.env.cache_clear()
    lists = {"downloads.proxy_platforms": ["youtube"]}
    monkeypatch.setattr(settings, "get_list", lambda key, default=None: lists.get(key, default))
    assert net.proxy_for("youtube") == "http://user:pass@proxy:8080"
    assert net.proxy_for("twitch") is None


def test_proxy_is_for_video_downloads_only_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paid per GB: live recording, discovery and the chat CLI stay direct unless `proxy.lanes` says so."""
    from sixstars import config

    monkeypatch.setenv("PROXY_URL", "http://user:pass@proxy:8080")
    config.env.cache_clear()
    lists: dict[str, list[Any]] = {"downloads.proxy_platforms": ["youtube"]}
    monkeypatch.setattr(settings, "get_list", lambda key, default=None: lists.get(key, default))
    assert net.proxy_for("youtube", "vod") == "http://user:pass@proxy:8080"
    for lane in ("live", "discovery", "chat"):
        assert net.proxy_for("youtube", lane) is None  # type: ignore[arg-type]
    lists["proxy.lanes"] = ["vod", "live"]
    assert net.proxy_for("youtube", "live") == "http://user:pass@proxy:8080"


def test_backoff_delay_schedule() -> None:
    assert [net.backoff_delay(i) for i in range(-1, 8)] == [1.0, 1.0, 2.0, 5.0, 10.0, 30.0, 30.0, 30.0, 30.0]


def test_open_live_routes_by_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(twitch_irc, "stream", lambda login, **kw: seen.update(kw, twitch=login))
    monkeypatch.setattr(youtube_live, "stream", lambda video, **kw: seen.update(kw, youtube=video))
    chat.open_live("twitch", "somechannel", max_reconnects=5)
    assert seen == {"twitch": "somechannel", "proxy": None, "max_reconnects": 5}
    seen.clear()
    chat.open_live("youtube", "AAAAAAAAAAA")
    assert seen == {"youtube": "AAAAAAAAAAA", "proxy": None, "cookies_file": None, "max_reconnects": None}


def test_download_replay_routes_by_platform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    twitch_chat = ReplayChat("twitch", "1", [])
    monkeypatch.setattr(
        twitch_replay, "download", lambda vod, raw, **kw: seen.update(kw, vod=vod, raw=raw) or twitch_chat
    )
    monkeypatch.setattr(youtube_replay, "download", lambda video, **kw: seen.update(kw, video=video))
    raw = tmp_path / "raw.json"
    assert chat.download_replay("twitch", "1", raw_path=raw) is twitch_chat
    assert seen == {"vod": "1", "raw": raw, "proxy": None}
    seen.clear()
    assert chat.download_replay("youtube", "AAAAAAAAAAA") is None
    assert seen == {"video": "AAAAAAAAAAA", "proxy": None, "cookies_file": None}


# ── a cookies path without a file means "no cookies" (the default .env names an absent file) ──


def test_a_missing_cookies_file_means_no_cookies_and_is_logged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "secrets" / "youtube_cookies.txt"
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(missing))
    config.env.cache_clear()
    net._missing_cookies_logged.clear()
    with structlog.testing.capture_logs() as logs:
        assert net.cookies_file() is None
        assert net.cookies_file() is None
        assert net.usable_cookies(missing) is None
    assert [entry["event"] for entry in logs] == ["youtube_cookies_file_missing"]
    assert logs[0]["path"] == str(missing)


def test_an_existing_cookies_file_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(cookies))
    config.env.cache_clear()
    assert net.cookies_file() == cookies
    assert net.usable_cookies(None) is None


def test_download_replay_and_open_live_do_not_pass_a_missing_cookies_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(tmp_path / "missing.txt"))
    config.env.cache_clear()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(youtube_replay, "download", lambda video, **kw: seen.update(kw, replay=video))
    monkeypatch.setattr(youtube_live, "stream", lambda video, **kw: seen.update(kw, live=video))
    chat.download_replay("youtube", "AAAAAAAAAAA")
    assert seen["replay"] == "AAAAAAAAAAA" and seen["cookies_file"] is None
    seen.clear()
    chat.open_live("youtube", "AAAAAAAAAAA")
    assert seen["live"] == "AAAAAAAAAAA" and seen["cookies_file"] is None


def test_the_adapters_themselves_survive_a_missing_cookies_file(tmp_path: Path) -> None:
    """The crash M08 reported: `MozillaCookieJar.load` raised FileNotFoundError inside `new_client`."""
    missing = tmp_path / "missing.txt"
    client = youtube_innertube.new_client(cookies_file=missing)
    assert client.cookies.get("SOCS", domain=".youtube.com") is not None
    asyncio.run(client.aclose())
    command = youtube_replay.build_ytdlp_command("AAAAAAAAAAA", tmp_path, None, missing)
    assert "--cookies" not in command
