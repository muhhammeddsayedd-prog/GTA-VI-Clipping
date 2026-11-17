"""Every setting one discovery pass reads, loaded once at the start of the pass into `DiscoveryConfig`.

Keys seeded by migration `0002` are required. Keys added by migration `0006_discovery.sql` fall back to
`DEFAULTS` (the values that migration seeds) so that discovery keeps running on a database where the
migration has not been applied yet; an integration test keeps the two in step.
"""

from dataclasses import dataclass
from typing import Any, Final

from sixstars import settings

# Values of migration 0006 (see docs/16-settings-reference.md). Only used when a key is missing.
DEFAULTS: Final[dict[str, Any]] = {
    "discovery.youtube_search_results": 40,
    "discovery.youtube_recent_sorts": ["relevance", "views"],
    "discovery.ytdlp_workers": 6,
    "discovery.ytdlp_timeout_s": 25,
    "discovery.youtube_quota_daily_units": 3000,
    "discovery.rss_max_channels_per_run": 25,
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


def _int(key: str) -> int:
    return settings.get_int(key, DEFAULTS[key]) if key in DEFAULTS else settings.get_int(key)


def _float(key: str) -> float:
    return settings.get_float(key, DEFAULTS[key]) if key in DEFAULTS else settings.get_float(key)


def _strings(key: str) -> tuple[str, ...]:
    values = settings.get_list(key, list(DEFAULTS[key])) if key in DEFAULTS else settings.get_list(key)
    return tuple(str(value) for value in values if str(value).strip())


def _weights(key: str) -> dict[str, float]:
    return {str(name): float(value) for name, value in settings.get_dict(key).items()}


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

    @classmethod
    def from_settings(cls) -> "DiscoveryConfig":
        """Read the settings table (through the 30-second cache)."""
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
        )
