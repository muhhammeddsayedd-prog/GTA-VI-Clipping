"""Source hardening, pure parts: the Data API search budget and rotation, the source health (one warning,
one recovery, yt-dlp back-off), the language signals and the yield ranking. No network, no database."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sixstars.clients._http import ApiError
from sixstars.clients.fakes import FakeYouTubeClient, fake_search_item
from sixstars.discovery import health, scoring, sources, ytsearch
from sixstars.discovery.config import SOURCES_DEFAULTS, DiscoveryConfig, ScoreConfig
from sixstars.discovery.sources import Stub
from sixstars.discovery.ytsearch import FakeYtDlpSearch, FlatVideo, YtDlpError, YtDlpSearch

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
# The exact error the staging server (Hetzner, data-centre address) returned on every pass.
SERVER_ERROR = (
    "yt-dlp search failed for 'GTA 5': ERROR: query \"GTA 5\" page 1: Unable to download API page: "
    "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
)
SERVER_STDERR = (
    'ERROR: query "GTA 5" page 1: Unable to download API page: '
    "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
)
TERMS = tuple(str(term) for term in SOURCES_DEFAULTS["discovery.non_english_terms"])

SCORE = ScoreConfig(
    weights_live={"viewers": 0.35, "growth": 0.25, "chat_rate": 0.2, "global_reach": 0.1, "history": 0.1},
    weights_vod={
        "views_per_hour": 0.45,
        "engagement": 0.15,
        "size_ratio": 0.15,
        "global_reach": 0.1,
        "history": 0.15,
    },
    non_english_penalty=0.85,
    preferred_bonus=15,
    chat_replay_bonus=5,
    chat_mpm_per_viewer=0.01,
    chat_msgs_per_view=0.02,
    chat_min_messages=1500,
    passive_views_engagement=0.005,
    edited_upload_bonus=6,
    edited_max_duration_s=3600,
    edited_engagement_full=0.04,
    long_stream_min_s=3600,
    thin_chat_factor=0.75,
)


def config(**overrides: Any) -> DiscoveryConfig:
    base = DiscoveryConfig(
        target_game="Grand Theft Auto V",
        search_terms=("GTA 5", "GTA V", "Grand Theft Auto V", "GTA RP"),
        interval_s=600,
        vod_lookback_h=48,
        min_vod_duration_s=480,
        live_score_cutoff=55,
        vod_score_cutoff=50,
        max_vod_per_run=6,
        max_vod_per_creator_per_run=1,
        max_vod_backlog=24,
        release_after_below_s=1200,
        title_blocklist=(),
        live_max_slots=3,
        queue_low_watermark=12,
        score=SCORE,
        youtube_search_results=40,
        youtube_recent_sorts=("relevance", "views"),
        ytdlp_workers=2,
        ytdlp_timeout_s=25,
        youtube_quota_daily_units=3000,
        rss_max_channels_per_run=50,
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
        search_api_mode="fallback",
        search_api_calls_per_pass=1,
        search_api_interval_s=1800,
        search_api_daily_calls=60,
        search_api_orders={"live": "viewCount", "recent": "viewCount"},
        search_api_durations={"recent": "long"},
        non_english_terms=TERMS,
    )
    return replace(base, **overrides)


# ── search.list: rotation and budget ─────────────────────────


OR_QUERY = '"GTA 5"|"GTA V"|"Grand Theft Auto V"|"GTA RP"'


def test_the_calls_alternate_live_and_recent_and_live_walks_through_the_terms() -> None:
    """Recent videos: one OR query over all terms. Live: one term per call (the OR query finds few live
    streams, measured on the real API)."""
    cfg = config()
    assert sources.search_api_queries(cfg, "recent") == [OR_QUERY]
    assert sources.search_api_queries(cfg, "live") == ["GTA 5", "GTA V", "Grand Theft Auto V", "GTA RP"]
    assert sources.search_api_plan(cfg, 9) == [
        ("live", "GTA 5"),
        ("recent", OR_QUERY),
        ("live", "GTA V"),
        ("recent", OR_QUERY),
        ("live", "Grand Theft Auto V"),
        ("recent", OR_QUERY),
        ("live", "GTA RP"),
        ("recent", OR_QUERY),
        ("live", "GTA 5"),
    ]


def test_the_rotation_follows_the_settings() -> None:
    everything = config(search_api_combined_modes=("live", "recent"))
    assert sources.search_api_plan(everything, 3) == [
        ("live", OR_QUERY),
        ("recent", OR_QUERY),
        ("live", OR_QUERY),
    ]
    separate = config(search_api_combined_modes=(), search_terms=("GTA 5", "GTA V"))
    assert sources.search_api_plan(separate, 4) == [
        ("live", "GTA 5"),
        ("recent", "GTA 5"),
        ("live", "GTA V"),
        ("recent", "GTA V"),
    ]
    only_live = config(search_api_modes=("live", "nonsense"), search_terms=("GTA 6",))
    assert sources.search_api_plan(only_live, 2) == [("live", "GTA 6"), ("live", "GTA 6")]
    assert sources.search_api_queries(config(search_terms=("GTA 6",)), "recent") == [
        "GTA 6"
    ]  # nothing to join
    assert sources.search_api_pick(config(search_terms=()), 0) is None
    assert sources.search_api_pick(config(search_api_modes=()), 0) is None


@pytest.mark.parametrize(
    ("overrides", "state", "calls_today", "primary_working", "allowed", "reason"),
    [
        ({}, {}, 0, False, True, ""),
        ({"search_api_mode": "off"}, {}, 0, False, False, "switched off"),
        ({"search_api_calls_per_pass": 0}, {}, 0, False, False, "switched off"),
        ({}, {}, 0, True, False, "not needed: the yt-dlp search works"),
        ({"search_api_mode": "always"}, {}, 0, True, True, ""),
        ({}, {}, 60, False, False, "daily budget spent (60 of 60 calls)"),
        ({}, {"last_call_at": "2026-11-17T11:40:00+00:00"}, 3, False, False, "interval not reached"),
        # a pass that arrives a few seconds early still counts
        ({}, {"last_call_at": "2026-11-17T11:30:20+00:00"}, 3, False, True, ""),
        ({}, {"last_call_at": "2026-11-17T11:00:00+00:00"}, 3, False, True, ""),
        ({}, {"quota_until": "2026-11-18T08:10:00+00:00"}, 3, False, False, "Google reported the quota"),
        ({}, {"quota_until": "2026-11-17T08:10:00+00:00"}, 3, False, True, ""),
        ({"search_terms": ()}, {}, 0, False, False, "no search terms or modes"),
    ],
)
def test_search_api_guard(
    overrides: dict[str, Any],
    state: dict[str, Any],
    calls_today: int,
    primary_working: bool,
    allowed: bool,
    reason: str,
) -> None:
    result = sources.search_api_may_call(config(**overrides), state, NOW, calls_today, primary_working)
    assert result[0] is allowed
    assert result[1].startswith(reason)


def test_default_cadence_never_exhausts_the_day() -> None:
    """The arithmetic of the notes: at the 10-minute cadence the defaults make 48 calls a day, under the
    cap of 60, under Google's 100, and each hour holds one live and one recent search."""
    cfg = config()
    state: dict[str, Any] = {}
    calls: list[tuple[datetime, str, str]] = []
    for number in range(144):  # one day of passes
        at = NOW.replace(hour=0) + timedelta(seconds=600 * number)
        allowed, _ = sources.search_api_may_call(cfg, state, at, len(calls), False)
        if allowed:
            pick = sources.search_api_pick(cfg, len(calls))
            assert pick is not None
            calls.append((at, *pick))
            state = {"index": len(calls), "last_call_at": at.isoformat()}
    assert len(calls) == 48 <= cfg.search_api_daily_calls < 100
    for hour in range(24):
        in_hour = [(mode, query) for at, mode, query in calls if at.hour == hour]
        # every hour: one live search, and one recent search that covers every term
        assert [mode for mode, _ in in_hour] == ["live", "recent"], hour
        assert in_hour[1][1] == OR_QUERY
    # every term gets its live search six times a day (every four hours)
    for term in cfg.search_terms:
        assert sum(1 for _, mode, query in calls if mode == "live" and query == term) == 6
    # Even if Google still charged the old 100 units per call, 60 calls leave 4,000 of 10,000 units.
    assert 10_000 - cfg.search_api_daily_calls * 100 >= 3_000


def test_next_quota_reset_is_after_midnight_pacific() -> None:
    assert sources.next_quota_reset(NOW) == datetime(2026, 11, 18, 8, 10, tzinfo=UTC)
    early = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
    assert sources.next_quota_reset(early) == datetime(2026, 11, 17, 8, 10, tzinfo=UTC)


def test_quota_errors_are_recognised() -> None:
    quota = ApiError(
        "youtube",
        "GET /youtube/v3/search",
        403,
        "The request cannot be completed because you have exceeded your quota.",
    )
    assert sources.is_quota_error(quota)
    assert not sources.is_quota_error(
        ApiError("youtube", "GET /youtube/v3/search", 400, "invalidSearchFilter")
    )
    assert not sources.is_quota_error(ApiError("youtube", "GET /youtube/v3/search", 403, "API key not valid"))
    assert not sources.is_quota_error(RuntimeError("quota"))


def test_a_search_result_becomes_a_stub_with_unescaped_text() -> None:
    item = fake_search_item("live0000001", "UCa", title="GTA 5 &amp; chill | Tom&#39;s stream", live=True)
    item["snippet"]["description"] = "#gta5 &quot;live&quot;"
    stub = sources._stub_from_search(item)
    assert stub is not None
    assert (stub.video_id, stub.channel_id, stub.live_status, stub.origin) == (
        "live0000001",
        "UCa",
        "is_live",
        "ytapi",
    )
    assert stub.title == "GTA 5 & chill | Tom's stream" and stub.description == '#gta5 "live"'
    assert stub.published_at == datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    upcoming = fake_search_item("soon0000001")
    upcoming["snippet"]["liveBroadcastContent"] = "upcoming"
    stub = sources._stub_from_search(upcoming)
    assert stub is not None and stub.live_status == "is_upcoming"
    assert sources.candidate_from_stub(stub) is None
    assert sources._stub_from_search({"id": {"kind": "youtube#channel", "channelId": "UCa"}}) is None


def test_search_api_step_reports_off_and_not_configured_without_touching_anything() -> None:
    assert sources.search_api_step(FakeYouTubeClient(), config(search_api_mode="off"), NOW, False) == (
        [],
        None,
        0,
        None,
    )
    stubs, report, calls, note = sources.search_api_step(None, config(), NOW, False)
    assert report is not None and (report.name, report.status) == ("youtube_search_api", "not_configured")
    assert (stubs, calls, note) == ([], 0, None)


# ── yt-dlp search: probe and back-off ────────────────────────


def failing_search() -> FakeYtDlpSearch:
    search = FakeYtDlpSearch()
    search.error = YtDlpError(SERVER_ERROR)
    return search


def test_the_server_error_text_comes_out_of_the_wrapper_unchanged(sleeps: list[float]) -> None:
    runs: list[int] = []

    def runner(command: Any, timeout_s: float) -> tuple[int, str, str]:
        runs.append(1)
        return 1, "", "WARNING: something\n" + SERVER_STDERR + "\n"

    with pytest.raises(YtDlpError) as caught:
        YtDlpSearch(runner=runner).search("GTA 5")
    assert str(caught.value) == SERVER_ERROR
    assert len(runs) == ytsearch.ATTEMPTS


def test_a_healthy_search_runs_every_task_and_a_failing_one_is_reported_whole() -> None:
    cfg = config()
    with ThreadPoolExecutor(max_workers=2) as pool:
        search = failing_search()
        stubs, report, searches = sources.search_youtube(search, cfg, pool, health.Gate())
    assert (stubs, report.status, searches) == ([], "error", 12)
    assert report.detail == SERVER_ERROR  # not cut: the notification must show all of it
    assert len(search.calls) == 12


def test_a_source_known_to_fail_probes_with_one_search() -> None:
    cfg = config()
    with ThreadPoolExecutor(max_workers=2) as pool:
        search = failing_search()
        stubs, report, searches = sources.search_youtube(search, cfg, pool, health.Gate(probe=True))
        assert (stubs, report.status, searches, len(search.calls)) == ([], "error", 1, 1)
        assert report.detail.startswith(SERVER_ERROR) and "probe" in report.detail

        working = FakeYtDlpSearch()
        working.default_live = [FlatVideo("live0000001", "GTA 5", "UCa", "A", "is_live", 900, None, None)]
        stubs, report, searches = sources.search_youtube(working, cfg, pool, health.Gate(probe=True))
        assert (report.status, searches, len(working.calls)) == ("ok", 12, 12)
        assert [stub.video_id for stub in stubs] == ["live0000001"] * 4  # the probe's rows are kept


def test_a_source_that_backs_off_runs_nothing() -> None:
    cfg = config()
    gate = health.Gate(skip=True, detail="backing off until 13:00 UTC after 2 failed passes: boom")
    with ThreadPoolExecutor(max_workers=2) as pool:
        search = failing_search()
        stubs, report, searches = sources.search_youtube(search, cfg, pool, gate)
    assert (stubs, report.status, report.detail, searches, search.calls) == (
        [],
        "skipped",
        gate.detail,
        0,
        [],
    )


def test_search_command_takes_cookies_only_when_given(tmp_path: Path) -> None:
    plain = ytsearch.build_command("https://x", 5, None)
    assert "--cookies" not in plain and "--proxy" not in plain
    cookies = tmp_path / "cookies.txt"
    full = ytsearch.build_command("https://x", 5, "http://u:p@proxy:1", cookies)
    assert full[full.index("--cookies") + 1] == str(cookies)
    assert full[full.index("--proxy") + 1] == "http://u:p@proxy:1" and full[-1] == "https://x"


def test_the_proxy_url_never_reaches_an_error_message(sleeps: list[float]) -> None:
    def runner(command: Any, timeout_s: float) -> tuple[int, str, str]:
        return 1, "", "ERROR: Unable to connect to proxy http://user:secret@proxy:8000"

    with pytest.raises(YtDlpError) as caught:
        YtDlpSearch(proxy="http://user:secret@proxy:8000", runner=runner).search("GTA 5")
    assert "secret" not in str(caught.value) and "<proxy>" in str(caught.value)


# ── source health ────────────────────────────────────────────

BACKOFF = {health.YTDLP_SOURCE: (2, 3600)}


def run_passes(
    statuses: list[str], notify_after: int = 3
) -> tuple[dict[str, dict[str, Any]], list[list[health.Notice]]]:
    state: dict[str, dict[str, Any]] = {}
    sent: list[list[health.Notice]] = []
    for number, status in enumerate(statuses):
        at = NOW + timedelta(minutes=10 * number)
        detail = SERVER_ERROR if status == "error" else ""
        state, notices = health.step(
            state, [("youtube_search", status, detail)], at, notify_after=notify_after, backoff=BACKOFF
        )
        sent.append(notices)
    return state, sent


def test_a_source_that_keeps_failing_is_reported_once_with_error_and_fix() -> None:
    state, sent = run_passes(["error"] * 6)
    assert [len(notices) for notices in sent] == [0, 0, 1, 0, 0, 0]
    assert state["youtube_search"]["passes"] == 6
    notice = sent[2][0]
    assert (notice.severity, notice.source, notice.event) == (
        "warn",
        "youtube_search",
        "discovery.source_failing",
    )
    assert notice.title == "Discovery source youtube_search keeps failing"
    assert "3 passes in a row since 2026-11-17 14:00 Cairo (2026-11-17 12:00 UTC)" in notice.body
    assert "RemoteDisconnected('Remote end closed connection without response')" in notice.body
    assert "PROXY_URL" in notice.body
    assert state["youtube_search"]["failures"] == 6 and state["youtube_search"]["notified"] is True


def test_recovery_is_announced_once_and_only_after_a_warning() -> None:
    state, sent = run_passes(["error", "error", "error", "skipped", "ok", "ok", "error", "empty"])
    assert [[n.severity for n in notices] for notices in sent] == [[], [], ["warn"], [], ["info"], [], [], []]
    recovered = sent[4][0]
    assert recovered.title == "Discovery source youtube_search recovered"
    assert "after 4 passes" in recovered.body and recovered.event == "discovery.source_recovered"
    assert state == {}  # a single later failure that went away was never worth a message


def test_skipped_and_not_configured_change_nothing() -> None:
    state, _ = run_passes(["error"])  # failing, but not backing off yet
    same, notices = health.step(
        state,
        [("youtube_search", "skipped", "no search terms"), ("twitch", "not_configured", "no Twitch keys")],
        NOW,
        notify_after=3,
    )
    assert same == state and notices == []


def test_the_ytdlp_search_backs_off_after_repeated_failures_then_probes() -> None:
    state, _ = run_passes(["error"])
    assert health.gate(state, "youtube_search", NOW + timedelta(minutes=10)) == health.Gate(probe=True)

    state, _ = run_passes(["error", "error"])
    until = NOW + timedelta(minutes=10) + timedelta(seconds=3600)
    assert state["youtube_search"]["skip_until"] == until.isoformat()
    during = health.gate(state, "youtube_search", NOW + timedelta(minutes=20))
    assert during.skip and not during.probe
    assert during.detail.startswith("backing off until 13:10 UTC after 2 failed attempts: yt-dlp search fail")
    after = health.gate(state, "youtube_search", until + timedelta(seconds=1))
    assert after == health.Gate(probe=True)
    assert health.gate({}, "youtube_search", NOW) == health.Gate()
    assert health.gate(state, "youtube_rss", NOW) == health.Gate()


def test_a_pass_sat_out_while_backing_off_counts_towards_the_warning_but_not_the_back_off() -> None:
    """Two failures start the back-off; the third pass is skipped, and that is when the owner hears of it
    (20 minutes after the first failure, not an hour later at the next probe)."""
    state, sent = run_passes(["error", "error"])
    until = state["youtube_search"]["skip_until"]
    at = NOW + timedelta(minutes=20)
    assert health.gate(state, "youtube_search", at).skip
    state, notices = health.step(
        state, [("youtube_search", "skipped", "backing off")], at, notify_after=3, backoff=BACKOFF
    )
    assert [notice.severity for notice in notices] == ["warn"]
    assert "3 passes in a row" in notices[0].body and "RemoteDisconnected" in notices[0].body
    entry = state["youtube_search"]
    assert (entry["failures"], entry["passes"], entry["skip_until"]) == (2, 3, until)
    state, notices = health.step(
        state,
        [("youtube_search", "skipped", "backing off")],
        at + timedelta(minutes=10),
        notify_after=3,
        backoff=BACKOFF,
    )
    assert notices == [] and state["youtube_search"]["skip_until"] == until


def test_a_proxy_or_cookies_added_by_the_owner_end_the_back_off_at_once(tmp_path: Path) -> None:
    assert YtDlpSearch().access == "direct" == FakeYtDlpSearch().access
    assert YtDlpSearch(proxy="http://u:p@proxy:1").access == "proxy"
    assert YtDlpSearch(cookies_file=tmp_path / "c.txt").access == "cookies"
    assert YtDlpSearch(proxy="http://u:p@proxy:1", cookies_file=tmp_path / "c.txt").access == "proxy+cookies"

    state: dict[str, dict[str, Any]] = {}
    for number in range(2):
        state, _ = health.step(
            state,
            [("youtube_search", "error", SERVER_ERROR)],
            NOW + timedelta(minutes=10 * number),
            notify_after=3,
            backoff=BACKOFF,
            access={"youtube_search": "direct"},
        )
    assert state["youtube_search"]["access"] == "direct"
    at = NOW + timedelta(minutes=20)
    assert health.gate(state, "youtube_search", at, "direct").skip  # nothing changed: keep waiting
    assert health.gate(state, "youtube_search", at, "proxy") == health.Gate(probe=True)
    assert health.gate(state, "youtube_search", at, "cookies") == health.Gate(probe=True)
    assert health.gate(state, "youtube_search", at).skip  # a caller that does not say: keep waiting


def test_one_day_on_the_server_costs_a_handful_of_doomed_searches_not_1728() -> None:
    """Without the back-off: 144 passes x 12 searches. With it: the first pass, then one probe an hour."""
    state: dict[str, dict[str, Any]] = {}
    searches = 0
    warnings = 0
    for number in range(144):
        at = NOW + timedelta(minutes=10 * number)
        gate = health.gate(state, "youtube_search", at)
        if gate.skip:
            continue
        searches += 1 if gate.probe else 12
        state, notices = health.step(
            state, [("youtube_search", "error", SERVER_ERROR)], at, notify_after=3, backoff=BACKOFF
        )
        warnings += len(notices)
    assert searches <= 12 + 24 and warnings == 1


def test_other_sources_never_back_off_and_unknown_sources_get_the_doctor_hint() -> None:
    state: dict[str, dict[str, Any]] = {}
    for number in range(3):
        state, notices = health.step(
            state,
            [("youtube_api", "error", "HTTP 403"), ("mystery", "error", "boom")],
            NOW + timedelta(minutes=10 * number),
            notify_after=3,
            backoff=BACKOFF,
        )
    assert state["youtube_api"]["skip_until"] is None
    assert {notice.source for notice in notices} == {"youtube_api", "mystery"}
    assert "sixstars discovery doctor" in next(n.body for n in notices if n.source == "mystery")


# ── language ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "channel", "description", "tags", "signal"),
    [
        ("GTA 5 LIVE | Train vs 100 Luxury Cars", "Sunil Gaming", "", (), None),  # nothing says so: honest
        (
            "GTA 5 LIVE | Train vs 100 Luxury Cars",
            "Sunil Gaming",
            "Fun stream #gta5hindi #live",
            (),
            "term:hindi",
        ),
        (
            "GTA 5 LIVE | Train vs 100 Luxury Cars",
            "Sunil Gaming",
            "",
            ("gta 5 hindi gameplay",),
            "term:hindi",
        ),
        ("GTA 5 LIVE in Hindi", "Raajoo Gaming", "", (), "term:hindi"),
        ("GTA 5 लाइव स्ट्रीम", "Raajoo Gaming", "", (), "script:title"),
        ("GTA 5 LIVE", "राजू गेमिंग", "", (), "script:channel"),
        ("GTA 5 LIVE", "Raajoo Gaming", "आज हम खेलेंगे जीटीए और बहुत मज़ा करेंगे", (), "script:description"),
        ("FALTAM 2 DIAS PRO GTA 6!! AO VIVO", "Megadragom GamePlay", "", (), "term:ao vivo"),
        ("GTA RP Brasil | Cidade Alta", "Canal", "", (), "term:brasil"),
        (
            "GTA 5 RP | Iconic RP",
            "Grizzley Gang Gaming",
            "English RP. Subtitles also in Hindi and Tamil.",
            (),
            None,
        ),
        ("ГТА 5 РП стрим", "Канал", "", (), "script:title"),
        ("", "", "", (), None),
    ],
)
def test_language_signal(
    title: str, channel: str, description: str, tags: tuple[str, ...], signal: str | None
) -> None:
    found = scoring.language_signal(
        title=title, channel_name=channel, description=description, tags=tags, terms=TERMS
    )
    assert found == signal


def test_a_signal_overrules_a_reported_english_but_never_a_reported_other_language() -> None:
    assert scoring.detect_language("en", "term:hindi") == "und"
    assert scoring.detect_language(None, "script:title") == "und"
    assert scoring.detect_language("en", None) == "en"
    assert scoring.detect_language(None, None) is None
    assert scoring.detect_language("pt", None) == "pt"
    assert scoring.detect_language("hi", "term:hindi") == "hi"
    assert not scoring.is_english("und")


def test_non_latin_share_and_its_threshold() -> None:
    assert scoring.non_latin_share("GTA 5") == 0.0
    assert scoring.non_latin_share("12345 !!!") is None
    share = scoring.non_latin_share("GTA लाइव")
    assert share == 0.5  # combining vowel signs are not letters
    assert scoring.language_signal(title="GTA लाइव", share_threshold=0.9) is None
    assert scoring.title_language("GTA लाइव") == "und" and scoring.title_language("GTA live") is None


def hindi_item(audio_language: str | None = "en") -> dict[str, Any]:
    snippet: dict[str, Any] = {
        "publishedAt": "2026-11-17T09:00:00Z",
        "channelId": "UChindi",
        "channelTitle": "Sunil Gaming",
        "title": "GTA 5 LIVE | Train vs 100 Luxury Cars",
        "description": "Daily live stream.\n#gta5 #gta5hindi #shortslive",
        "tags": ["gta 5", "gta v"],
        "liveBroadcastContent": "live",
    }
    if audio_language:
        snippet["defaultAudioLanguage"] = audio_language
    return {
        "id": "hindi000001",
        "snippet": snippet,
        "liveStreamingDetails": {"actualStartTime": "2026-11-17T09:00:00Z", "concurrentViewers": "8477"},
    }


def test_a_hindi_stream_marked_english_by_youtube_is_not_taken_as_english() -> None:
    """The M09 finding: `defaultAudioLanguage: en` on Hindi streams put them in two of three live slots."""
    candidate = sources.candidate_from_api(hindi_item(), None, NOW, config())
    assert candidate is not None
    assert (candidate.language, candidate.language_signal) == ("und", "term:hindi")
    english = hindi_item()
    english["snippet"]["description"] = "Daily live stream. #gta5"
    candidate = sources.candidate_from_api(english, None, NOW, config())
    assert candidate is not None and (candidate.language, candidate.language_signal) == ("en", None)
    reported = sources.candidate_from_api(hindi_item("hi"), None, NOW, config())
    assert reported is not None and (reported.language, reported.language_signal) == ("hi", None)
    # without the term list (a hand-built config) the reported language stands
    plain = sources.candidate_from_api(hindi_item(), None, NOW)
    assert plain is not None and plain.language == "en"


def test_the_listing_alone_also_gets_the_language_check() -> None:
    stub = Stub("v0000000001", title="GTA 5 लाइव", channel_id="UCa", channel_name="A", live_status="is_live")
    candidate = sources.candidate_from_stub(stub, config())
    assert candidate is not None and (candidate.language, candidate.language_signal) == (
        "und",
        "script:title",
    )
    tagged = Stub("v0000000002", title="GTA 5", channel_id="UCa", channel_name="A", description="#tamil")
    candidate = sources.candidate_from_stub(tagged, config())
    assert candidate is not None and candidate.language == "und"


def test_a_non_english_stream_ranks_below_the_same_stream_in_english() -> None:
    def live(language: str | None) -> float:
        score, _ = scoring.score_live(
            viewers=8477,
            viewers_then=None,
            measured_chat_mpm=None,
            language=language,
            audience_spread=None,
            history=0.5,
            preferred=False,
            config=SCORE,
        )
        return score

    assert live("und") < live("en") == live(None)
    assert live("und") == pytest.approx((live("en") - 4.0) * 0.85, abs=0.02)  # reach 0.6 and the penalty


# ── yield ────────────────────────────────────────────────────


def vod(**overrides: Any) -> tuple[float, dict[str, Any]]:
    arguments: dict[str, Any] = {
        "views": 100_000,
        "age_hours": 24.0,
        "likes": 4_000,
        "comments": 300,
        "followers": 500_000,
        "duration_s": 1500,
        "has_chat": False,
        "peak_viewers": None,
        "measured_chat_mpm": None,
        "language": "en",
        "audience_spread": None,
        "history": 0.5,
        "preferred": False,
        "config": SCORE,
    }
    arguments.update(overrides)
    score, parts, _ = scoring.score_vod(**arguments)
    return score, parts


def test_video_kinds() -> None:
    assert scoring.video_kind(1500, False, SCORE) == "edited"
    assert scoring.video_kind(3600, False, SCORE) == "edited"
    assert scoring.video_kind(3601, False, SCORE) == "long_upload"
    assert scoring.video_kind(None, False, SCORE) == "unknown"
    assert scoring.video_kind(1500, True, SCORE) == scoring.video_kind(None, True, SCORE) == "stream"


def test_an_edited_upload_with_strong_engagement_gets_the_bonus() -> None:
    neutral = replace(SCORE, edited_upload_bonus=0.0, thin_chat_factor=1.0)
    base, _ = vod(config=neutral)
    score, parts = vod()  # (4000 + 600) / 100000 = 0.046 >= 0.04: all six points
    assert (parts["kind"], parts["yield_bonus"], parts["yield_factor"]) == ("edited", 6.0, 1.0)
    assert score == pytest.approx(base + 6.0, abs=0.01)
    half, parts = vod(likes=1_800, comments=100)  # 0.02: half
    assert parts["yield_bonus"] == 3.0
    passive, parts = vod(likes=10, comments=0)
    assert parts["yield_bonus"] == pytest.approx(0.02, abs=0.01)
    unknown, parts = vod(likes=None, comments=None)
    assert parts["yield_bonus"] == 3.0  # nothing known: half


def test_a_long_stream_is_scored_by_how_much_chat_it_has() -> None:
    neutral = replace(SCORE, edited_upload_bonus=0.0, thin_chat_factor=1.0)
    stream = {"has_chat": True, "duration_s": 4 * 3600}
    # measured: 3 messages a minute over 4 hours = 720 messages = 48 % of the 1,500 needed
    thin, parts = vod(**stream, measured_chat_mpm=3.0)
    thin_base, base_parts = vod(**stream, measured_chat_mpm=3.0, config=neutral)
    assert (parts["kind"], parts["yield_bonus"], parts["chat_messages_est"]) == ("stream", 0.0, 720)
    assert parts["yield_factor"] == pytest.approx(0.75 + 0.25 * 0.48)
    chat_bonus = base_parts["chat_bonus"]
    assert thin == pytest.approx((thin_base - chat_bonus) * parts["yield_factor"] + chat_bonus, abs=0.02)

    busy, parts = vod(**stream, measured_chat_mpm=20.0)
    busy_base, _ = vod(**stream, measured_chat_mpm=20.0, config=neutral)
    assert parts["yield_factor"] == 1.0 and busy == busy_base
    silent, parts = vod(**stream, measured_chat_mpm=0.0)
    assert parts["yield_factor"] == 0.75

    short_stream, parts = vod(has_chat=True, duration_s=1800, measured_chat_mpm=0.0)
    assert parts["yield_factor"] == 1.0  # only long streams are judged by chat


def test_a_raw_long_upload_without_chat_gets_the_full_factor_and_no_bonus() -> None:
    score, parts = vod(duration_s=3 * 3600)
    assert (parts["kind"], parts["yield_bonus"], parts["yield_factor"]) == ("long_upload", 0.0, 0.75)
    unknown, parts = vod(duration_s=None)
    assert (parts["kind"], parts["yield_bonus"], parts["yield_factor"]) == ("unknown", 0.0, 1.0)


def test_the_25_minute_edit_outranks_the_quiet_four_hour_stream_with_the_same_audience() -> None:
    """The M08 finding in one comparison: the M09 score let the stream win (chat bonus), now the edit does."""
    neutral = replace(SCORE, edited_upload_bonus=0.0, thin_chat_factor=1.0)
    stream = {"has_chat": True, "duration_s": 4 * 3600, "measured_chat_mpm": 2.0}
    assert vod(config=neutral)[0] < vod(**stream, config=neutral)[0]
    assert vod()[0] > vod(**stream)[0]


def test_the_neutral_defaults_of_a_hand_built_config_change_nothing() -> None:
    old = ScoreConfig(SCORE.weights_live, SCORE.weights_vod, 0.85, 15, 5, 0.01, 0.02, 1500, 0.005)
    score, parts = vod(config=old)
    assert (parts["yield_bonus"], parts["yield_factor"]) == (0.0, 1.0)
    score, parts = vod(config=old, has_chat=True, duration_s=4 * 3600, measured_chat_mpm=0.0)
    assert (parts["yield_bonus"], parts["yield_factor"]) == (0.0, 1.0)
