"""Integration tests for the spike engine against Postgres: settings are read from the `settings` table,
spikes are stored once however often detection runs, and each new spike records an event."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sixstars import db, settings
from sixstars.chat import synthetic
from sixstars.cli import app
from sixstars.spikes import (
    DetectedSpike,
    LiveSpikeDetector,
    SpikeConfig,
    from_settings,
    persist_spike,
    scan_replay,
)

TWITCH_SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "chat" / "twitch_replay_sample.json"
UNIX = 1_800_000_000.0


@pytest.fixture
def source() -> Iterator[dict[str, str]]:
    """A creator with one video and one live session; everything the tests write is removed afterwards."""
    creator = db.fetch_one(
        "insert into creators (platform, channel_id, handle) values ('twitch', 'm04-test', 'm04test') "
        "returning id::text as id"
    )
    assert creator is not None
    video = db.fetch_one(
        "insert into videos (creator_id, platform, source_id, url) "
        "values (%s, 'twitch', 'm04-vod', 'https://example.com/v') returning id::text as id",
        (creator["id"],),
    )
    session = db.fetch_one(
        "insert into live_sessions (creator_id, platform, source_id, channel_login, url) "
        "values (%s, 'twitch', 'm04-live', 'm04test', 'https://example.com/l') returning id::text as id",
        (creator["id"],),
    )
    assert video is not None and session is not None
    yield {"creator_id": creator["id"], "video_id": video["id"], "live_session_id": session["id"]}
    db.execute("update spikes set merged_into = null where creator_id = %s", (creator["id"],))
    db.execute("delete from spikes where creator_id = %s", (creator["id"],))
    db.execute("delete from videos where creator_id = %s", (creator["id"],))
    db.execute("delete from live_sessions where creator_id = %s", (creator["id"],))
    db.execute("delete from creators where id = %s", (creator["id"],))


def replay_spikes() -> list[DetectedSpike]:
    chat = synthetic.merge(
        synthetic.flat_chat(1800, 5),
        synthetic.burst(1200.0, 120, 80),
        synthetic.burst(1240.0, 60, 40, seed=5, user_prefix="again"),
        synthetic.burst(1500.0, 90, 60, seed=6, user_prefix="later"),
    )
    return scan_replay(chat)  # no config passed: the thresholds come from the settings table


def test_config_comes_from_the_settings_table_and_follows_changes() -> None:
    cfg = from_settings()
    assert (cfg.bucket_s, cfg.lead_s, cfg.z_b, cfg.min_messages) == (10.0, 20.0, 2.5, 15.0)
    assert cfg.weights["msgs"] == 0.4 and "streamelements" in cfg.ignore_users
    settings.set("spikes.lead_s", 25)
    settings.set("spikes.z_b", 9.9)
    changed = SpikeConfig.from_settings()
    assert (changed.lead_s, changed.z_b) == (25.0, 9.9)
    assert replay_spikes() == []  # nothing reaches the raised threshold
    assert SpikeConfig.from_settings({"spikes.lead_s": 15}).lead_s == 15.0


def test_replay_spikes_are_stored_once_with_their_merged_spikes(source: dict[str, str]) -> None:
    spikes = replay_spikes()
    assert [spike.peak_t for spike in spikes] == [1200.0, 1500.0]
    ids = [persist_spike(s, creator_id=source["creator_id"], video_id=source["video_id"]) for s in spikes]
    again = [
        persist_spike(s, creator_id=source["creator_id"], video_id=source["video_id"])
        for s in replay_spikes()
    ]
    assert again == ids and len(set(ids)) == 2

    rows = db.fetch_all(
        "select id::text as id, mode, status, tier, spike_offset_s, spike_wall, score_z, parts, "
        "msgs_in_bucket, unique_chatters, reaction_count, clip_requests, chat_sample, "
        "window_start_s, window_end_s, window_start_wall, merged_into::text as merged_into, "
        "live_session_id from spikes where video_id = %s order by spike_offset_s",
        (source["video_id"],),
    )
    assert [(row["status"], float(row["spike_offset_s"])) for row in rows] == [
        ("detected", 1200.0),
        ("merged", 1240.0),
        ("detected", 1500.0),
    ]
    first, merged, _ = rows
    assert (first["id"], merged["merged_into"]) == (ids[0], ids[0])
    assert (first["mode"], first["tier"], first["spike_wall"], first["live_session_id"]) == (
        "replay",
        "S",
        None,
        None,
    )
    assert (float(first["window_start_s"]), float(first["window_end_s"])) == (1180.0, 1215.0)
    assert first["window_start_wall"] is None
    assert (first["msgs_in_bucket"], first["unique_chatters"]) == (125, 85)
    assert first["reaction_count"] > 100 and first["clip_requests"] > 0
    assert float(first["score_z"]) == pytest.approx(9.5)
    assert first["parts"]["msgs_z"] == 10.0 and first["parts"]["spike_floor"] == 1200.0
    assert (first["parts"]["first_t"], first["parts"]["pad_start"], first["parts"]["pad_end"]) == (
        1200.0,
        1165.0,
        1230.0,
    )
    assert len(first["chat_sample"]) == 40 and set(first["chat_sample"][0]) == {"t_rel", "user", "text"}

    events = db.fetch_all(
        "select entity_type, entity_id, data, service from events where type = 'spike.detected'"
    )
    assert sorted(event["entity_id"] for event in events) == sorted(
        ids
    )  # one event per new spike, none on re-run
    by_id = {event["entity_id"]: event for event in events}
    assert by_id[ids[0]]["data"]["merged"] == 1 and by_id[ids[0]]["data"]["tier"] == "S"
    assert by_id[ids[0]]["entity_type"] == "spike" and by_id[ids[0]]["service"] == "spikes"


def test_live_spikes_are_stored_with_wall_clock_times(source: dict[str, str]) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5, start=UNIX), synthetic.burst(UNIX + 1200.0, 120, 80))
    detector = LiveSpikeDetector("twitch", UNIX, listen_start=UNIX)  # settings are read from the table
    found: list[DetectedSpike] = []
    index, now = 0, UNIX
    while now <= UNIX + 1300.0:
        while index < len(chat) and chat[index].t < now:
            detector.feed(chat[index])
            index += 1
        found += detector.tick(now)
        now += 5.0
    assert len(found) == 1
    spike = found[0]
    spike_id = persist_spike(
        spike, creator_id=source["creator_id"], live_session_id=source["live_session_id"]
    )
    assert (
        persist_spike(spike, creator_id=source["creator_id"], live_session_id=source["live_session_id"])
        == spike_id
    )
    rows = db.fetch_all("select * from spikes where live_session_id = %s", (source["live_session_id"],))
    assert len(rows) == 1
    row = rows[0]
    assert (row["mode"], row["status"], row["tier"], row["spike_offset_s"], row["window_start_s"]) == (
        "live",
        "detected",
        "S",
        None,
        None,
    )
    assert row["spike_wall"] == datetime.fromtimestamp(spike.peak_t, UTC)
    assert row["window_start_wall"] == datetime.fromtimestamp(spike.window_start, UTC)
    assert row["window_end_wall"] == datetime.fromtimestamp(spike.window_end, UTC)
    assert row["parts"]["spike_floor"] == spike.spike_floor
    assert db.fetch_one("select count(*) as n from events where type = 'spike.detected'") == {"n": 1}


def test_persist_needs_a_source(source: dict[str, str]) -> None:
    spike = replay_spikes()[0]
    with pytest.raises(ValueError, match="live_session_id or video_id"):
        persist_spike(spike, creator_id=source["creator_id"])


def test_scan_command_reads_settings_from_the_database() -> None:
    result = CliRunner().invoke(app, ["spikes", "scan", str(TWITCH_SAMPLE), "--top", "3"])
    assert result.exit_code == 0, result.output
    assert "1 spikes (S=1, A=0, B=0)" in result.output
    assert result.output.splitlines()[2].split()[:3] == ["1", "09:55:00", "S"]
