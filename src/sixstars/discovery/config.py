"""Every setting one discovery pass reads, loaded once at the start of the pass into `DiscoveryConfig`.

Keys seeded by migration `0002` are required. Keys added by migration `0006_discovery.sql` fall back to
`DEFAULTS` and keys added by `0010_sources.sql` to `SOURCES_DEFAULTS` (the values those migrations seed) so
that discovery keeps running on a database where a migration has not been applied yet; integration tests
keep code and migrations in step.
"""

from dataclasses import dataclass, field
from typing import Any, Final

from sixstars import settings

# Values of migration 0006 (see docs/16-settings-reference.md). Only used when a key is missing.
DEFAULTS: Final[dict[str, Any]] = {
    "discovery.youtube_search_results": 40,
    "discovery.youtube_recent_sorts": ["relevance", "views"],
    "discovery.ytdlp_workers": 6,
    "discovery.ytdlp_timeout_s": 25,
    "discovery.youtube_quota_daily_units": 9000,
    "discovery.rss_max_channels_per_run": 50,  # 25 in 0006, raised by 0010
    "discovery.rss_cursor": None,
    "discovery.apify_interval_s": 21600,
    "discovery.apify_max_results": 5,
    "discovery.apify_price_per_result_usd": 0.003,
    "discovery.apify_daily_budget_usd": 0.4,
    "discovery.apify_monthly_budget_usd": 12,
    "discovery.twitch_max_streams": 300,
    "discovery.twitch_max_videos": 50,
    "discovery.twitch_follower_lookups_per_run": 20,
    "discovery.twitch_game_cache": None,
    "discovery.growth_window_s": 1800,
    "discovery.min_observation_gap_s": 120,
    "discovery.min_live_viewers": 50,
    "discovery.chat_mpm_per_viewer": 0.01,
    "discovery.chat_msgs_per_view": 0.02,
    "discovery.chat_min_messages": 1500,
    "discovery.chat_replay_bonus": 5,
    "discovery.passive_views_engagement": 0.005,
    "discovery.max_vod_per_creator_per_run": 1,
    "discovery.max_vod_backlog": 24,
    "discovery.permission_recheck_days": 14,
    "discovery.require_permission_check": True,
    "discovery.permission_checks_per_run": 12,
    "discovery.permission_jobs_per_run": 25,
    "discovery.excluded_channels": [],
}

# Values of migration 0010 (source hardening). Only used when a key is missing.
SOURCES_DEFAULTS: Final[dict[str, Any]] = {
    # Data API search.list as a discovery source
    "discovery.search_api_mode": "fallback",
    "discovery.search_api_calls_per_pass": 1,
    "discovery.search_api_interval_s": 1800,
    "discovery.search_api_daily_calls": 60,
    "discovery.search_api_modes": ["live", "recent"],
    "discovery.search_api_combined_modes": ["recent", "completed"],
    "discovery.search_api_durations": {"recent": "long", "completed": "long"},
    "discovery.search_api_orders": {"live": "viewCount", "recent": "viewCount", "completed": "viewCount"},
    "discovery.search_api_relevance_language": "en",
    "discovery.search_api_category_id": "20",
    "discovery.search_api_state": None,
    # live streams seen before are looked at again each pass (1 quota unit per 50)
    "discovery.live_recheck_max": 50,
    "discovery.live_recheck_window_s": 5400,
    # source health
    "discovery.source_failure_notify_after": 3,
    "discovery.ytdlp_backoff_after_failures": 2,
    "discovery.ytdlp_backoff_s": 3600,
    "discovery.ytdlp_use_cookies": False,
    "discovery.source_health": None,
    # language
    "discovery.non_latin_share": 0.3,
    "discovery.non_english_terms": [
        "hindi",
        "\u0939\u093f\u0902\u0926\u0940",
        "\u0939\u093f\u0928\u094d\u0926\u0940",
        "urdu",
        "tamil",
        "telugu",
        "malayalam",
        "kannada",
        "bangla",
        "bengali",
        "marathi",
        "punjabi",
        "nepali",
        "indonesia",
        "tagalog",
        "pinoy",
        "espa\u00f1ol",
        "espanol",
        "en vivo",
        "en directo",
        "portugu\u00eas",
        "portugues",
        "ao vivo",
        "brasil",
        "deutsch",
        "fran\u00e7ais",
        "francais",
        "t\u00fcrk\u00e7e",
        "turkce",
        # not a language: the "Shorts feed" mod-showcase streams (no usable speech), seen 2026-11-17
        "shortsfeed",
        "shortslive",
        "secret mod",
    ],
    # yield
    "discovery.edited_upload_bonus": 6,
    "discovery.edited_max_duration_s": 3600,
    "discovery.edited_engagement_full": 0.04,
    "discovery.long_stream_min_s": 3600,
    "discovery.thin_chat_factor": 0.75,
}

# Values of migration 0013 (blocked platforms). Only used when a key is missing.
BLOCKED_DEFAULTS: Final[dict[str, Any]] = {
    "discovery.platform_weight": {},
    "discovery.max_blocked_waiting": 12,
    "discovery.blocked_platform_stale_s": 7200,
}

_ALL_DEFAULTS: Final[dict[str, Any]] = {**DEFAULTS, **SOURCES_DEFAULTS, **BLOCKED_DEFAULTS}
SEARCH_API_MODES: Final[tuple[str, ...]] = ("off", "fallback", "always")


def _int(key: str) -> int:
    return settings.get_int(key, _ALL_DEFAULTS[key]) if key in _ALL_DEFAULTS else settings.get_int(key)


def _float(key: str) -> float:
    if key in _ALL_DEFAULTS:
        return settings.get_float(key, _ALL_DEFAULTS[key])
    return settings.get_float(key)


def _bool(key: str) -> bool:
    return settings.get_bool(key, bool(_ALL_DEFAULTS[key]))


def _str(key: str) -> str:
    value = settings.get(key, _ALL_DEFAULTS[key])
    return str(value).strip() if value is not None else ""


def _strs(key: str) -> dict[str, str]:
    values = settings.get_dict(key, dict(_ALL_DEFAULTS[key]))
    return {str(name): str(value) for name, value in values.items()}


def _strings(key: str) -> tuple[str, ...]:
    if key in _ALL_DEFAULTS:
        values = settings.get_list(key, list(_ALL_DEFAULTS[key]))
    else:
        values = settings.get_list(key)
    return tuple(str(value) for value in values if str(value).strip())


def _weights(key: str) -> dict[str, float]:
    return {str(name): float(value) for name, value in settings.get_dict(key).items()}


def _platform_weights(key: str) -> dict[str, float]:
    """`{platform: factor}`; entries that are not numbers are ignored and a factor is never negative."""
    values = settings.get_dict(key, dict(_ALL_DEFAULTS[key]))
    return {
        str(name).casefold(): max(0.0, float(value))
        for name, value in values.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


@dataclass(frozen=True)
class ScoreConfig:
    """Settings of the trend score (pure scoring takes this, never the database)."""

    weights_live: dict[str, float]
    weights_vod: dict[str, float]
    non_english_penalty: float
    preferred_bonus: float
    chat_replay_bonus: float
    chat_mpm_per_viewer: float
    chat_msgs_per_view: float
    chat_min_messages: float
    passive_views_engagement: float
    # Yield (migration 0010). The defaults are neutral so that callers which build a config by hand keep
    # the M09 behaviour; `DiscoveryConfig.from_settings` always passes the settings.
    edited_upload_bonus: float = 0.0
    edited_max_duration_s: float = 3600.0
    edited_engagement_full: float = 0.04
    long_stream_min_s: float = 3600.0
    thin_chat_factor: float = 1.0


@dataclass(frozen=True)
class DiscoveryConfig:
    """Snapshot of the settings of one pass."""

    target_game: str
    search_terms: tuple[str, ...]
    interval_s: int
    vod_lookback_h: int
    min_vod_duration_s: int
    live_score_cutoff: float
    vod_score_cutoff: float
    max_vod_per_run: int
    max_vod_per_creator_per_run: int
    max_vod_backlog: int
    release_after_below_s: int
    title_blocklist: tuple[str, ...]
    live_max_slots: int
    queue_low_watermark: int
    score: ScoreConfig
    # sources
    youtube_search_results: int
    youtube_recent_sorts: tuple[str, ...]
    ytdlp_workers: int
    ytdlp_timeout_s: float
    youtube_quota_daily_units: int
    rss_max_channels_per_run: int
    apify_interval_s: int
    apify_max_results: int
    apify_price_per_result_usd: float
    apify_daily_budget_usd: float
    apify_monthly_budget_usd: float
    twitch_max_streams: int
    twitch_max_videos: int
    twitch_follower_lookups_per_run: int
    # history and ranking
    growth_window_s: int
    min_observation_gap_s: int
    min_live_viewers: int
    # creators
    permission_recheck_days: int
    require_permission_check: bool
    permission_checks_per_run: int
    permission_jobs_per_run: int
    excluded_channels: tuple[str, ...]
    # sources hardening (migration 0010); the defaults switch the additions off for hand-built configs
    search_api_mode: str = "off"  # off | fallback | always
    search_api_calls_per_pass: int = 1
    search_api_interval_s: int = 1800
    search_api_daily_calls: int = 60
    search_api_modes: tuple[str, ...] = ("live", "recent")
    search_api_combined_modes: tuple[str, ...] = ("recent", "completed")
    search_api_durations: dict[str, str] = field(default_factory=dict)
    search_api_orders: dict[str, str] = field(default_factory=dict)
    search_api_relevance_language: str = "en"
    search_api_category_id: str = "20"
    live_recheck_max: int = 0
    live_recheck_window_s: int = 5400
    source_failure_notify_after: int = 3
    ytdlp_backoff_after_failures: int = 2
    ytdlp_backoff_s: int = 3600
    ytdlp_use_cookies: bool = False
    non_latin_share: float = 0.3
    non_english_terms: tuple[str, ...] = ()
    # blocked platforms (migration 0013); the defaults leave hand-built configs as they were
    platform_weight: dict[str, float] = field(default_factory=dict)  # missing platform = 1.0
    max_blocked_waiting: int = 12
    blocked_platform_stale_s: int = 7200

    @classmethod
    def from_settings(cls) -> "DiscoveryConfig":
        """Read the settings table (through the 30-second cache)."""
        mode = _str("discovery.search_api_mode").casefold()
        return cls(
            target_game=settings.get_str("env.target_game"),
            search_terms=_strings("env.search_terms"),
            interval_s=_int("discovery.interval_s"),
            vod_lookback_h=_int("discovery.vod_lookback_h"),
            min_vod_duration_s=_int("discovery.min_vod_duration_s"),
            live_score_cutoff=_float("discovery.live_score_cutoff"),
            vod_score_cutoff=_float("discovery.vod_score_cutoff"),
            max_vod_per_run=_int("discovery.max_vod_per_run"),
            max_vod_per_creator_per_run=_int("discovery.max_vod_per_creator_per_run"),
            max_vod_backlog=_int("discovery.max_vod_backlog"),
            release_after_below_s=_int("discovery.release_after_below_s"),
            title_blocklist=_strings("discovery.title_blocklist"),
            live_max_slots=_int("live.max_slots"),
            queue_low_watermark=_int("posting.queue_low_watermark"),
            score=ScoreConfig(
                weights_live=_weights("discovery.weights_live"),
                weights_vod=_weights("discovery.weights_vod"),
                non_english_penalty=_float("discovery.non_english_penalty"),
                preferred_bonus=_float("discovery.preferred_bonus"),
                chat_replay_bonus=_float("discovery.chat_replay_bonus"),
                chat_mpm_per_viewer=_float("discovery.chat_mpm_per_viewer"),
                chat_msgs_per_view=_float("discovery.chat_msgs_per_view"),
                chat_min_messages=_float("discovery.chat_min_messages"),
                passive_views_engagement=_float("discovery.passive_views_engagement"),
                edited_upload_bonus=_float("discovery.edited_upload_bonus"),
                edited_max_duration_s=_float("discovery.edited_max_duration_s"),
                edited_engagement_full=_float("discovery.edited_engagement_full"),
                long_stream_min_s=_float("discovery.long_stream_min_s"),
                thin_chat_factor=_float("discovery.thin_chat_factor"),
            ),
            youtube_search_results=_int("discovery.youtube_search_results"),
            youtube_recent_sorts=_strings("discovery.youtube_recent_sorts"),
            ytdlp_workers=max(1, _int("discovery.ytdlp_workers")),
            ytdlp_timeout_s=_float("discovery.ytdlp_timeout_s"),
            youtube_quota_daily_units=_int("discovery.youtube_quota_daily_units"),
            rss_max_channels_per_run=_int("discovery.rss_max_channels_per_run"),
            apify_interval_s=_int("discovery.apify_interval_s"),
            apify_max_results=_int("discovery.apify_max_results"),
            apify_price_per_result_usd=_float("discovery.apify_price_per_result_usd"),
            apify_daily_budget_usd=_float("discovery.apify_daily_budget_usd"),
            apify_monthly_budget_usd=_float("discovery.apify_monthly_budget_usd"),
            twitch_max_streams=_int("discovery.twitch_max_streams"),
            twitch_max_videos=_int("discovery.twitch_max_videos"),
            twitch_follower_lookups_per_run=_int("discovery.twitch_follower_lookups_per_run"),
            growth_window_s=_int("discovery.growth_window_s"),
            min_observation_gap_s=_int("discovery.min_observation_gap_s"),
            min_live_viewers=_int("discovery.min_live_viewers"),
            permission_recheck_days=_int("discovery.permission_recheck_days"),
            require_permission_check=settings.get_bool(
                "discovery.require_permission_check", DEFAULTS["discovery.require_permission_check"]
            ),
            permission_checks_per_run=_int("discovery.permission_checks_per_run"),
            permission_jobs_per_run=_int("discovery.permission_jobs_per_run"),
            excluded_channels=_strings("discovery.excluded_channels"),
            search_api_mode=mode if mode in SEARCH_API_MODES else "off",
            search_api_calls_per_pass=max(0, _int("discovery.search_api_calls_per_pass")),
            search_api_interval_s=max(0, _int("discovery.search_api_interval_s")),
            search_api_daily_calls=max(0, _int("discovery.search_api_daily_calls")),
            search_api_modes=_strings("discovery.search_api_modes"),
            search_api_combined_modes=_strings("discovery.search_api_combined_modes"),
            search_api_durations=_strs("discovery.search_api_durations"),
            search_api_orders=_strs("discovery.search_api_orders"),
            search_api_relevance_language=_str("discovery.search_api_relevance_language"),
            search_api_category_id=_str("discovery.search_api_category_id"),
            live_recheck_max=max(0, _int("discovery.live_recheck_max")),
            live_recheck_window_s=max(0, _int("discovery.live_recheck_window_s")),
            source_failure_notify_after=max(1, _int("discovery.source_failure_notify_after")),
            ytdlp_backoff_after_failures=max(1, _int("discovery.ytdlp_backoff_after_failures")),
            ytdlp_backoff_s=max(0, _int("discovery.ytdlp_backoff_s")),
            ytdlp_use_cookies=_bool("discovery.ytdlp_use_cookies"),
            non_latin_share=_float("discovery.non_latin_share"),
            non_english_terms=_strings("discovery.non_english_terms"),
            platform_weight=_platform_weights("discovery.platform_weight"),
            max_blocked_waiting=max(0, _int("discovery.max_blocked_waiting")),
            blocked_platform_stale_s=max(0, _int("discovery.blocked_platform_stale_s")),
        )
