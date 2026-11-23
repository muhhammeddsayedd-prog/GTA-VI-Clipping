"""Campaign looks against the real schema (D118, PR 3): brief_match before drawing (a failing clip becomes a
Normal clip, never dropped), the campaign's render profile applied to the style, logos stored in the
profile row and cached on disk, and the overlay-keep profile refused until Render v2."""

import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from posting_support import seed

from sixstars import config, db
from sixstars.campaigns import look, profiles, service
from sixstars.factory.style import StyleRules


@pytest.fixture(autouse=True)
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db.execute(
        "truncate clip_campaigns, clip_campaign_creators, lane_quotas, posts, clips, spikes, creators, "
        "platform_accounts restart identity cascade"
    )
    db.execute("delete from render_profiles where id like 'test-%%'")
    seed("production")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.env.cache_clear()


def png_bytes() -> bytes:
    raw = b"\x00" + b"\xff\x00\x00\xff" * 4
    header = struct.pack(">IIBBBBB", 4, 1, 8, 6, 0, 0, 0)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def setup_campaign(
    profile: str = "clean-disclose", brief: dict[str, object] | None = None
) -> tuple[str, str]:
    creator = db.fetch_one(
        "insert into creators (platform, channel_id, handle) values ('twitch', 'u1', 'streamer') "
        "returning id::text as id"
    )
    assert creator is not None
    campaign = service.apply_change(
        "look-test",
        {
            "name": "Look test",
            "rate_per_1k": 1.0,
            "budget_left": 100,
            "ends_at": datetime.now(UTC) + timedelta(days=2),
            "platforms": ["instagram"],
            "creator_ids": [creator["id"]],
            "quota": {"instagram": "1-3"},
            "render_profile": profile,
            "brief_rules": brief or {"min_len": 10, "max_len": 40},
        },
        "test",
        create=True,
    )
    service.set_status("look-test", "active", "test")
    clip = db.fetch_one(
        "insert into clips (series_no, creator_id, source_platform, source_id, source_url, source_type, "
        "lane, start_s, end_s, gate_profile, status, campaign_id, render_profile) "
        "values (nextval('clip_series_seq'), %s, 'twitch', 'v1', 'u', 'vod', 'hourly', 0, 20, 'hourly', "
        "'rendering', %s, %s) returning id::text as id",
        (creator["id"], campaign.id, profile),
    )
    assert clip is not None
    return str(clip["id"]), str(campaign.id)


def test_a_fitting_clip_gets_its_campaign_look() -> None:
    clip_id, campaign_id = setup_campaign()
    rules = look.rules_for(clip_id, StyleRules(), has_facecam=True, duration_s=20.0)
    assert (rules.watermark, rules.disclosure_text, rules.disclosure_s) == (False, "Paid partnership", 1.5)
    row = db.fetch_one(
        "select campaign_id::text as campaign_id, brief_result from clips where id = %s", (clip_id,)
    )
    assert row is not None and row["campaign_id"] == campaign_id and row["brief_result"]["passed"] is True


@pytest.mark.parametrize(("facecam", "duration"), [(False, 20.0), (True, 55.0), (True, 8.0)])
def test_a_clip_that_misses_the_brief_becomes_normal_not_dropped(facecam: bool, duration: float) -> None:
    clip_id, _ = setup_campaign()
    rules = look.rules_for(clip_id, StyleRules(), has_facecam=facecam, duration_s=duration)
    assert rules == StyleRules()  # the brand look
    row = db.fetch_one(
        "select status, campaign_id, render_profile, brief_result from clips where id = %s", (clip_id,)
    )
    assert row is not None
    assert (row["status"], row["campaign_id"], row["render_profile"]) == ("rendering", None, "brand")
    assert row["brief_result"]["passed"] is False and row["brief_result"]["campaign"] == "look-test"
    event = db.fetch_one("select message from events where type = 'campaign.brief_failed' order by id desc")
    assert event is not None and "made as a Normal clip" in event["message"]


def test_a_normal_clip_is_untouched() -> None:
    clip_id, _ = setup_campaign()
    db.execute("update clips set campaign_id = null where id = %s", (clip_id,))
    rules = StyleRules(follow_overlay_s=1.5, follow_overlay_text="Follow")
    assert look.rules_for(clip_id, rules, has_facecam=False, duration_s=99) is rules


def test_logo_profiles_store_the_png_and_cache_it(tmp_path: Path) -> None:
    db.execute(
        "insert into render_profiles (id, watermark, logo_pos, disclosure_text, disclosure_s) "
        "values ('test-logo', false, 'bottom_right', 'Paid partnership', 1.5)"
    )
    with pytest.raises(ValueError):
        profiles.save_logo("test-logo", b"not a png")
    digest = profiles.save_logo("test-logo", png_bytes())
    profile = profiles.load("test-logo")
    assert profile.logo_sha == digest
    path = profiles.logo_file(profile)
    assert path is not None and path.read_bytes() == png_bytes() and path.parent == tmp_path / "logos"
    clip_id, _ = setup_campaign(profile="test-logo")
    rules = look.rules_for(clip_id, StyleRules(), has_facecam=True, duration_s=20.0)
    assert rules.campaign_logo == str(path) and rules.campaign_logo_pos == "bottom_right"


def test_a_logo_profile_without_its_logo_and_overlay_keep_are_refused() -> None:
    with pytest.raises(service.CampaignError) as missing:
        setup_campaign(profile="partner-logo")
    assert "has none uploaded" in str(missing.value)
    db.execute("truncate clip_campaigns, creators restart identity cascade")
    with pytest.raises(service.CampaignError) as overlay:
        setup_campaign(profile="overlay-keep")
    assert "Render v2" in str(overlay.value)
