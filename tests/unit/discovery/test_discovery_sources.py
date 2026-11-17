"""The yt-dlp search wrapper, the conversion of YouTube data into candidates, the Apify spend guard, the
permission rules and the candidate filter. No network, no database."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sixstars.ai.schemas import PermissionVerdict
from sixstars.discovery import sources, tiers, ytsearch
from sixstars.discovery.config import DiscoveryConfig, ScoreConfig
from sixstars.discovery.models import Candidate, Creator
from sixstars.discovery.run import filter_candidates
from sixstars.discovery.sources import Stub
from sixstars.discovery.ytsearch import FakeYtDlpSearch, FlatVideo, YtDlpError, YtDlpSearch

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "discovery" / "ytdlp_flat_search.jsonl"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def config(**overrides: Any) -> DiscoveryConfig:
    base = DiscoveryConfig(
        target_game="Grand Theft Auto V",
        search_terms=("GTA 5", "GTA V"),
        interval_s=600,
        vod_lookback_h=48,
        min_vod_duration_s=480,
        live_score_cutoff=55,
        vod_score_cutoff=50,
        max_vod_per_run=6,
        max_vod_per_creator_per_run=1,
        max_vod_backlog=24,
        release_after_below_s=1200,
        title_blocklist=("casino", "sponsored"),
        live_max_slots=3,
        queue_low_watermark=12,
        score=ScoreConfig({}, {}, 0.85, 15, 5, 0.01, 0.02, 1500, 0.005),
        youtube_search_results=40,
        youtube_recent_sorts=("relevance", "views"),
        ytdlp_workers=2,
        ytdlp_timeout_s=25,
        youtube_quota_daily_units=3000,
        rss_max_channels_per_run=25,
        apify_interval_s=21600,
        apify_max_results=5,
        apify_price_per_result_usd=0.003,
        apify_daily_budget_usd=0.4,
        apify_monthly_budget_usd=12,
        twitch_max_streams=300,
        twitch_max_videos=50,
        twitch_follower_lookups_per_run=20,
        growth_window_s=1800,
        min_observation_gap_s=120,
        min_live_viewers=50,
        permission_recheck_days=14,
        require_permission_check=True,
        permission_checks_per_run=12,
        permission_jobs_per_run=25,
        excluded_channels=(),
    )
    return replace(base, **overrides)


# ── yt-dlp search ────────────────────────────────────────────


def test_sp_values_match_the_youtube_filter_menu() -> None:
    assert ytsearch.build_sp(live=True, videos_only=False) == "EgJAAQ=="  # Live
    assert ytsearch.build_sp(date_code=ytsearch.DATE_TODAY, videos_only=False) == "EgIIAg=="  # Today
    assert ytsearch.build_sp(date_code=ytsearch.DATE_WEEK) == "EgQIAxAB"  # This week + Video
    assert ytsearch.build_sp(date_code=ytsearch.DATE_TODAY, sort="views") == "CAMSBAgCEAE="
    assert ytsearch.build_sp(live=True) == "EgQQAUAB"


@pytest.mark.parametrize(("hours", "code"), [(None, None), (24, 2), (48, 3), (168, 3), (169, 4)])
def test_date_filter_covers_the_lookback(hours: int | None, code: int | None) -> None:
    assert ytsearch.date_code_for(hours) == code


def test_search_url_and_command() -> None:
    url = ytsearch.build_search_url("GTA 5 & RP", "EgJAAQ==")
    assert url == "https://www.youtube.com/results?search_query=GTA%205%20%26%20RP&sp=EgJAAQ%3D%3D"
    command = ytsearch.build_command(url, 40, None)
    assert command[1:3] == ["-m", "yt_dlp"]
    assert "--flat-playlist" in command and "--dump-json" in command
    assert command[command.index("--playlist-end") + 1] == "40"
    assert "--proxy" not in command and command[-1] == url
    with_proxy = ytsearch.build_command(url, 40, "http://user:pass@proxy.test:8000")
    assert with_proxy[with_proxy.index("--proxy") + 1] == "http://user:pass@proxy.test:8000"


def test_parse_real_flat_rows() -> None:
    rows = ytsearch.parse_rows(FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 12
    live = [row for row in rows if row.is_live]
    assert len(live) == 6
    assert all(row.viewers is not None and row.views is None and row.duration_s is None for row in live)
    assert all(row.channel_id and row.channel_id.startswith("UC") for row in live)
    finished = [row for row in rows if not row.is_live]
    assert all(row.duration_s for row in finished)
    assert any(row.live_status == "was_live" for row in finished)
    # YouTube sometimes serves a listing without channel and view count: the Data API must fill them
    assert any(row.channel_id is None and row.views is None for row in finished)


def test_parse_skips_noise_duplicates_and_non_videos() -> None:
    text = "\n".join(
        [
            "WARNING: something",
            '{"id": "abc", "title": "One", "ie_key": "Youtube", "view_count": 5, "duration": 600.0}',
            '{"id": "abc", "title": "One again", "ie_key": "Youtube"}',
            '{"id": "UCchannel", "title": "A channel", "ie_key": "YoutubeTab"}',
            "{broken json",
        ]
    )
    assert ytsearch.parse_rows(text) == [FlatVideo("abc", "One", None, "", None, None, 5, 600)]


class Runner:
    def __init__(self, replies: Sequence[tuple[int, str, str]]) -> None:
        self.replies = list(replies)
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        self.commands.append(list(command))
        return self.replies[min(len(self.commands) - 1, len(self.replies) - 1)]


ROW = (
    '{"id": "abc", "title": "One", "ie_key": "Youtube", "live_status": "is_live", "concurrent_view_count": 7}'
)


def test_search_retries_then_succeeds(sleeps: list[float]) -> None:
    runner = Runner([(1, "", "ERROR: HTTP Error 429"), (0, ROW, "")])
    found = YtDlpSearch(runner=runner).search("GTA 5", live=True, limit=10)
    assert [video.video_id for video in found] == ["abc"]
    assert len(runner.commands) == 2 and sleeps == [2.0]
    assert "sp=EgQQAUAB" in runner.commands[0][-1]


def test_search_raises_after_the_last_attempt(sleeps: list[float]) -> None:
    runner = Runner([(1, "", "ERROR: Sign in to confirm you are not a bot")])
    with pytest.raises(YtDlpError, match="not a bot"):
        YtDlpSearch(runner=runner).search("GTA 5")
    assert len(runner.commands) == ytsearch.ATTEMPTS


def test_search_keeps_rows_printed_before_a_timeout() -> None:
    runner = Runner([(-1, ROW, "timed out after 25 s")])
    assert len(YtDlpSearch(runner=runner).search("GTA 5")) == 1
    assert len(runner.commands) == 1


def test_empty_listing_is_not_an_error() -> None:
    assert YtDlpSearch(runner=Runner([(0, "", "")])).search("GTA 5") == []


def test_recent_search_uses_date_filter_and_sort() -> None:
    runner = Runner([(0, "", "")])
    YtDlpSearch(runner=runner).search("GTA 5", within_hours=48, sort="views")
    assert runner.commands[0][-1].endswith("sp=CAMSBAgDEAE%3D")


def test_fake_search_records_calls() -> None:
    fake = FakeYtDlpSearch()
    fake.default_live = [FlatVideo("live1", "t", "UC1", "c", "is_live", 5, None, None)]
    assert [video.video_id for video in fake.search("x", live=True)] == ["live1"]
    assert fake.search("x") == []
    assert [call["live"] for call in fake.calls] == [True, False]


# ── Data API items and stubs ─────────────────────────────────

LIVE_ITEM: dict[str, Any] = {
    "id": "4ZGRGGe2lRo",
    "snippet": {
        "publishedAt": "2026-11-16T23:55:55Z",
        "channelId": "UCbItyvYa6cqvqtwlU2E9-wA",
        "channelTitle": "SteveTheGamer55",
        "title": "GTA 5 MODS LIVE",
        "liveBroadcastContent": "live",
        "defaultAudioLanguage": "en-US",
    },
    "contentDetails": {"duration": "P0D"},
    "statistics": {"viewCount": "4091", "likeCount": "212", "commentCount": "0"},
    "liveStreamingDetails": {"actualStartTime": "2026-11-17T00:38:18Z", "concurrentViewers": "388"},
}
PAST_STREAM_ITEM: dict[str, Any] = {
    "id": "DWqggMxXPcM",
    "snippet": {
        "publishedAt": "2026-11-16T18:00:00Z",
        "channelId": "UC72PuhDwKtZ5MikpGNhPAtA",
        "channelTitle": "TGG",
        "title": "GTA 5: The Final Drive",
        "liveBroadcastContent": "none",
    },
    "contentDetails": {"duration": "PT2H35M9S"},
    "statistics": {"viewCount": "758702", "likeCount": "15763", "commentCount": "664"},
    "liveStreamingDetails": {
        "actualStartTime": "2026-11-16T18:05:00Z",
        "actualEndTime": "2026-11-16T20:40:09Z",
    },
}
UPLOAD_ITEM: dict[str, Any] = {
    "id": "upload00001",
    "snippet": {
        "publishedAt": "2026-11-17T08:00:00Z",
        "channelId": "UCupload",
        "channelTitle": "Uploader",
        "title": "ГТА 5 прохождение без комментариев",
        "liveBroadcastContent": "none",
    },
    "contentDetails": {"duration": "PT24M35S"},
    "statistics": {"viewCount": "5000", "likeCount": "100"},
}


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("PT30S", 30), ("PT2H35M9S", 9309), ("P1DT2H", 93600), ("P0D", 0), ("PT1H", 3600), ("nonsense", None)],
)
def test_parse_iso_duration(value: str, seconds: int | None) -> None:
    assert sources.parse_iso_duration(value) == seconds


def test_live_item_becomes_a_live_candidate() -> None:
    candidate = sources.candidate_from_api(LIVE_ITEM, None, NOW)
    assert candidate is not None
    assert (candidate.source_type, candidate.viewers, candidate.language) == ("live", 388, "en")
    assert candidate.started_at == datetime(2026, 11, 17, 0, 38, 18, tzinfo=UTC)
    assert candidate.url == "https://www.youtube.com/watch?v=4ZGRGGe2lRo"
    assert candidate.channel_id == "UCbItyvYa6cqvqtwlU2E9-wA"


def test_hidden_viewer_count_falls_back_to_the_listing() -> None:
    item = {**LIVE_ITEM, "liveStreamingDetails": {"actualStartTime": "2026-11-17T00:38:18Z"}}
    assert sources.candidate_from_api(item, None, NOW).viewers is None  # type: ignore[union-attr]
    stub = Stub(video_id="4ZGRGGe2lRo", viewers=361, live_status="is_live")
    assert sources.candidate_from_api(item, stub, NOW).viewers == 361  # type: ignore[union-attr]


def test_past_stream_has_chat_and_is_published_when_it_ended() -> None:
    candidate = sources.candidate_from_api(PAST_STREAM_ITEM, None, NOW)
    assert candidate is not None
    assert (candidate.source_type, candidate.has_chat, candidate.duration_s) == ("vod", True, 9309)
    assert (candidate.views, candidate.likes, candidate.comments) == (758702, 15763, 664)
    assert candidate.published_at == datetime(2026, 11, 16, 20, 40, 9, tzinfo=UTC)
    assert candidate.started_at == datetime(2026, 11, 16, 18, 5, tzinfo=UTC)


def test_plain_upload_has_no_chat_and_guesses_language_from_the_title() -> None:
    candidate = sources.candidate_from_api(UPLOAD_ITEM, None, NOW)
    assert candidate is not None
    assert (candidate.has_chat, candidate.comments, candidate.language) == (False, None, "und")


def test_upcoming_broadcast_is_dropped() -> None:
    item = {**LIVE_ITEM, "snippet": {**LIVE_ITEM["snippet"], "liveBroadcastContent": "upcoming"}}
    assert sources.candidate_from_api(item, None, NOW) is None
    assert sources.candidate_from_stub(Stub("x", channel_id="UC1", live_status="is_upcoming")) is None


def test_stub_fallback_without_the_api() -> None:
    live = sources.candidate_from_stub(
        Stub(
            "live1",
            title="GTA live",
            channel_id="UC1",
            channel_name="One",
            live_status="is_live",
            viewers=900,
        )
    )
    assert live is not None and (live.source_type, live.viewers, live.has_chat) == ("live", 900, True)
    past = sources.candidate_from_stub(
        Stub("vod1", title="GTA vod", channel_id="UC1", live_status="was_live", views=5000, duration_s=7200)
    )
    assert past is not None and (past.source_type, past.views, past.has_chat) == ("vod", 5000, True)
    assert sources.candidate_from_stub(Stub("nochannel", title="collab")) is None


def test_enrich_without_key_or_over_budget_uses_the_listing() -> None:
    from sixstars.clients.fakes import FakeYouTubeClient

    stubs = {"live1": Stub("live1", title="GTA", channel_id="UC1", live_status="is_live", viewers=10)}
    found, report, units, _ = sources.enrich_youtube(None, stubs, config(), NOW)
    assert (len(found), report.status, units) == (1, "not_configured", 0)
    client = FakeYouTubeClient()
    found, report, units, _ = sources.enrich_youtube(
        client, stubs, config(youtube_quota_daily_units=10), NOW, units_used_today=lambda: 10.0
    )
    assert (len(found), report.status, units) == (1, "skipped", 0)
    assert client.calls == []
    client.videos["live1"] = {**LIVE_ITEM, "id": "live1"}
    found, report, units, _ = sources.enrich_youtube(
        client, stubs, config(), NOW, units_used_today=lambda: 0.0
    )
    assert (found[0].viewers, report.status, units) == (388, "ok", 1)
    assert client.calls_to("videos_list")[0]["parts"] == list(sources.VIDEO_PARTS)


def test_quota_needed() -> None:
    assert [sources.quota_needed(n) for n in (1, 50, 51, 480)] == [1, 1, 2, 10]


def test_api_results_must_be_about_the_game() -> None:
    def item(
        title: str, category: str = "20", tags: list[str] | None = None, description: str = ""
    ) -> dict[str, Any]:
        snippet = {"title": title, "categoryId": category, "tags": tags or [], "description": description}
        return {"id": "x", "snippet": snippet}

    searched, feed = Stub("x"), Stub("x", needs_title_match=True)
    assert sources.on_topic(item("The LAST Ride in GTA 5"), searched, config())
    assert sources.on_topic(
        item("Welcome to Los Santos", "22", description="Playing Grand Theft Auto V"), searched, config()
    )
    assert sources.on_topic(item("Car meet", "22", tags=["gta v online"]), searched, config())
    # a role-play stream that never names the game is kept when it is at least a Gaming video
    assert sources.on_topic(item("ITS A PRIVILEGE! | Iconic RP"), searched, config())
    # what YouTube pads a thin search with
    assert not sources.on_topic(
        item("Gentle Night RAIN - Rain Sounds For Sleeping", "22"), searched, config()
    )
    assert not sources.on_topic(item("Granny is Live || Granny Horror Gameplay", "24"), searched, config())
    # a known creator's other uploads are not about the game just because they are Gaming videos
    assert not sources.on_topic(item("Minecraft hardcore day 100"), feed, config())
    assert sources.on_topic(item("GTA V heists with friends"), feed, config())


def test_feed_entries_must_name_the_game() -> None:
    off_topic = sources.candidate_from_stub(Stub("a", title="Minecraft day 100", channel_id="UC1"))
    on_topic = sources.candidate_from_stub(Stub("b", title="GTA V heists", channel_id="UC1"))
    assert off_topic is not None and on_topic is not None
    feed = Stub("a", needs_title_match=True)
    assert not sources.keep_youtube(off_topic, feed, config())
    assert sources.keep_youtube(on_topic, feed, config())
    assert sources.keep_youtube(off_topic, Stub("a"), config())  # search results are trusted


# ── Apify guard ──────────────────────────────────────────────


def test_apify_projected_cost() -> None:
    assert sources.apify_projected_cost(config()) == pytest.approx(2 * 5 * 0.003)


def test_apify_starts_when_nothing_ran_and_budget_allows() -> None:
    assert sources.apify_may_start(config(), None, NOW, 0.0, 0.0) == (True, "")


@pytest.mark.parametrize(
    ("overrides", "state_age_s", "day", "month", "reason"),
    [
        ({"apify_interval_s": 0}, None, 0.0, 0.0, "switched off"),
        ({"apify_max_results": 0}, None, 0.0, 0.0, "switched off"),
        ({}, 3600, 0.0, 0.0, "interval not reached"),
        ({}, None, 0.38, 1.0, "daily budget"),
        ({}, None, 0.0, 11.99, "monthly budget"),
    ],
)
def test_apify_guard_says_no(
    overrides: dict[str, Any], state_age_s: int | None, day: float, month: float, reason: str
) -> None:
    state = None
    if state_age_s is not None:
        state = {"run_id": "r1", "started_at": (NOW - timedelta(seconds=state_age_s)).isoformat()}
    allowed, why = sources.apify_may_start(config(**overrides), state, NOW, day, month)
    assert not allowed and why.startswith(reason)


def test_apify_runs_again_after_the_interval() -> None:
    state = {"run_id": "r1", "started_at": (NOW - timedelta(hours=6)).isoformat(), "collected": True}
    assert sources.apify_may_start(config(), state, NOW, 0.03, 0.5)[0]


def test_apify_worst_case_month_stays_under_budget() -> None:
    # the seeded values: 4 production terms x 5 results x $0.003 per run, one run every 6 hours
    seeded = config(search_terms=("GTA 6", "GTA VI", "Grand Theft Auto VI", "GTA6"))
    per_run = sources.apify_projected_cost(seeded)
    runs_per_day = 86400 / seeded.apify_interval_s
    assert per_run * runs_per_day <= seeded.apify_daily_budget_usd
    assert per_run * runs_per_day * 31 <= seeded.apify_monthly_budget_usd


# ── permission rules ─────────────────────────────────────────


def creator(tier: str = "auto", checked_days_ago: float | None = None) -> Creator:
    checked = None if checked_days_ago is None else NOW - timedelta(days=checked_days_ago)
    return Creator("id1", "youtube", "UC1", "@speedy", "Speedy", tier, description_checked_at=checked)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("UC1", True),
        ("uc1", True),
        ("@Speedy", True),
        ("speedy", True),
        ("youtube:UC1", True),
        ("twitch:UC1", False),
        ("twitch:speedy", False),
        ("someoneelse", False),
        ("", False),
    ],
)
def test_exclusion_list(entry: str, expected: bool) -> None:
    assert tiers.is_excluded("youtube", "UC1", "@speedy", [entry]) is expected


def test_needs_check_follows_the_recheck_interval() -> None:
    assert tiers.needs_check(creator(), NOW, 14)
    assert not tiers.needs_check(creator(checked_days_ago=13.9), NOW, 14)
    assert tiers.needs_check(creator(checked_days_ago=14), NOW, 14)
    assert tiers.needs_check(creator("restricted", checked_days_ago=20), NOW, 14)
    assert not tiers.needs_check(creator("blocked"), NOW, 14)


def test_clip_allowed() -> None:
    assert not tiers.clip_allowed(creator("restricted", 1), False)
    assert not tiers.clip_allowed(creator("blocked", 1), False)
    assert not tiers.clip_allowed(creator("auto"), True)
    assert tiers.clip_allowed(creator("auto"), False)
    assert tiers.clip_allowed(creator("auto", 1), True)
    assert tiers.clip_allowed(creator("preferred", 1), True)


def verdict(tier: str, evidence: str | None = None) -> PermissionVerdict:
    return PermissionVerdict(tier=tier, evidence=evidence)  # type: ignore[arg-type]


def test_decide_tier() -> None:
    assert tiers.decide_tier("auto", None, verdict("preferred", "feel free to clip")) == (
        "preferred",
        "feel free to clip",
    )
    assert tiers.decide_tier("preferred", "old quote", verdict("auto")) == ("auto", None)
    assert tiers.decide_tier("auto", None, verdict("restricted", "do not reupload")) == (
        "restricted",
        "do not reupload",
    )
    assert tiers.decide_tier("restricted", "old", verdict("auto")) == ("auto", None)


def test_blocked_never_changes_and_manual_tier_is_kept_unless_the_creator_forbids() -> None:
    assert tiers.decide_tier("blocked", "takedown", verdict("preferred", "clip away")) == (
        "blocked",
        "takedown",
    )
    assert tiers.decide_tier("preferred", "manual: partner", verdict("auto")) == (
        "preferred",
        "manual: partner",
    )
    assert tiers.decide_tier("preferred", "manual: partner", verdict("restricted", "no clips")) == (
        "restricted",
        "no clips",
    )


# ── candidate filter ─────────────────────────────────────────


def candidate(source_id: str, **overrides: Any) -> Candidate:
    values: dict[str, Any] = {
        "platform": "youtube",
        "source_type": "vod",
        "source_id": source_id,
        "url": "u",
        "title": "GTA 5 funny moments",
        "channel_id": "UC1",
        "channel_handle": "one",
        "channel_name": "One",
        "origin": "test",
        "duration_s": 3600,
        "published_at": NOW - timedelta(hours=5),
    }
    values.update(overrides)
    return Candidate(**values)


def test_filter_candidates() -> None:
    kept, skipped = filter_candidates(
        [
            candidate("ok"),
            candidate("ok"),
            candidate("casino", title="GTA Casino stream"),
            candidate("leak", title="gta 6 leaked gameplay"),
            candidate("short", duration_s=479),
            candidate("edge", duration_s=480),
            candidate("old", published_at=NOW - timedelta(hours=49)),
            candidate("unknown-age", published_at=None, duration_s=None),
            candidate("live", source_type="live", duration_s=None, published_at=NOW - timedelta(days=9)),
        ],
        config(),
        NOW,
    )
    assert [item.source_id for item in kept] == ["ok", "edge", "unknown-age", "live"]
    assert skipped == {"title_blocklist": 2, "too_short": 1, "too_old": 1}
