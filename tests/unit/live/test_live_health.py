"""Live source health: a platform that refuses this machine is backed off and reported once."""

from datetime import UTC, datetime, timedelta

from sixstars.live import health

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
BOT = "Sign in to confirm you're not a bot"


def blocked(
    state: health.State, now: datetime, access: str | None = "direct"
) -> tuple[health.State, list[health.Notice]]:
    return health.note_blocked(state, "youtube", now, BOT, notify_after=2, backoff_s=1800, access=access)


def test_a_healthy_platform_is_never_held_back() -> None:
    assert health.backing_off({}, "youtube", NOW) is None


def test_the_first_block_backs_off_briefly_and_tells_nobody() -> None:
    state, notices = blocked({}, NOW)
    assert notices == []
    assert state["youtube"]["blocks"] == 1 and state["youtube"]["error"] == BOT
    assert health.backing_off(state, "youtube", NOW + timedelta(minutes=4)) == NOW + timedelta(minutes=5)
    assert health.backing_off(state, "youtube", NOW + timedelta(minutes=5)) is None  # time to probe
    assert health.backing_off(state, "twitch", NOW) is None  # the other platform is not affected


def test_the_second_block_in_a_row_warns_once_and_backs_off_for_the_full_time() -> None:
    state, _ = blocked({}, NOW)
    state, notices = blocked(state, NOW + timedelta(minutes=5))
    assert [(n.severity, n.title, n.event) for n in notices] == [
        ("warn", "Live source youtube is blocked", "live.source_blocked")
    ]
    assert "PROXY_URL" in notices[0].body and BOT in notices[0].body and "2026-11-17 12:00" in notices[0].body
    assert health.backing_off(state, "youtube", NOW + timedelta(minutes=30)) == NOW + timedelta(minutes=35)
    # every later probe that is refused stays silent
    for probe in range(1, 6):
        state, notices = blocked(state, NOW + timedelta(minutes=5 + 30 * probe))
        assert notices == [] and state["youtube"]["notified"] is True
    assert state["youtube"]["blocks"] == 7 and state["youtube"]["since"] == NOW.isoformat()


def test_recovery_is_announced_once_and_only_after_a_warning() -> None:
    quiet, _ = blocked({}, NOW)
    cleared, notices = health.note_ok(quiet, "youtube", NOW + timedelta(minutes=6))
    assert cleared == {} and notices == []  # nobody was told it was down
    state, _ = blocked(quiet, NOW + timedelta(minutes=5))
    cleared, notices = health.note_ok(state, "youtube", NOW + timedelta(hours=2))
    assert cleared == {}
    assert [(n.severity, n.title, n.event) for n in notices] == [
        ("info", "Live source youtube recovered", "live.source_recovered")
    ]
    assert "2 refused starts" in notices[0].body
    assert health.note_ok(cleared, "youtube", NOW) == ({}, [])


def test_a_new_proxy_or_cookies_file_ends_the_back_off_at_once() -> None:
    state, _ = blocked({}, NOW, access="PROXY_URL not set, YOUTUBE_COOKIES_FILE not set")
    soon = NOW + timedelta(minutes=1)
    assert (
        health.backing_off(state, "youtube", soon, "PROXY_URL not set, YOUTUBE_COOKIES_FILE not set")
        is not None
    )
    assert health.backing_off(state, "youtube", soon, "PROXY_URL set, YOUTUBE_COOKIES_FILE not set") is None


def test_the_state_of_one_platform_does_not_touch_the_other() -> None:
    state, _ = blocked({}, NOW)
    state, _ = health.note_blocked(state, "twitch", NOW, "429", notify_after=1, backoff_s=600, access=None)
    assert set(state) == {"youtube", "twitch"}
    cleared, _ = health.note_ok(state, "twitch", NOW)
    assert set(cleared) == {"youtube"}
