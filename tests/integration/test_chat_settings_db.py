"""Integration tests for the parts of chat ingestion that read the `settings` table. They only read the
seeded rows (the test database is shared with other test runs)."""

from pathlib import Path

import pytest

from sixstars import config
from sixstars.chat import net, twitch_replay
from sixstars.chat.filters import FilterContext, is_counted, normalise_name
from sixstars.chat.models import ChatMessage

TWITCH_SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "chat" / "twitch_replay_sample.json"


def _msg(username: str, t: float = 1000.0) -> ChatMessage:
    return ChatMessage(platform="twitch", t=t, user_id=username, username=username, text="LMAO", kind="chat")


def test_filter_context_reads_the_seeded_settings() -> None:
    ctx = FilterContext.from_settings()
    assert ctx.warmup_s == 900.0
    assert {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot"} <= ctx.ignore_users
    assert (ctx.stream_start, ctx.bot_names) == (0.0, frozenset())


def test_filter_context_from_settings_filters_messages() -> None:
    ctx = FilterContext.from_settings(stream_start=1_700_000_000.0, bot_names=["@SpamAccount"])
    assert ctx.bot_names == {"spamaccount"}
    assert not is_counted(_msg("Nightbot", 1_700_001_000.0), ctx)
    assert not is_counted(_msg("spamaccount", 1_700_001_000.0), ctx)
    assert not is_counted(_msg("viewer", 1_700_000_899.0), ctx)
    assert is_counted(_msg("viewer", 1_700_000_900.0), ctx)


def test_seeded_ignore_list_drops_the_bot_in_the_real_twitch_sample() -> None:
    ctx = FilterContext.from_settings()
    messages = twitch_replay.parse_file(TWITCH_SAMPLE).messages
    counted = [message for message in messages if is_counted(message, ctx)]
    assert 190 <= len(counted) < len(messages)
    assert all(normalise_name(message.username) != "streamelements" for message in counted)


def test_proxy_follows_the_seeded_platform_list(monkeypatch: pytest.MonkeyPatch) -> None:
    assert net.proxy_for("youtube") is None  # no PROXY_URL: direct connections everywhere
    monkeypatch.setenv("PROXY_URL", "http://user:pass@proxy:8080")
    config.env.cache_clear()
    assert net.proxy_for("youtube") == "http://user:pass@proxy:8080"
    assert net.proxy_for("twitch") is None  # the seed lists only "youtube" in downloads.proxy_platforms
