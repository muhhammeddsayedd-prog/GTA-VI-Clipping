/** Types for the control API read routes. Source of truth: docs/26-admin-console.md. */

export type Iso = string;
export type Platform = "twitch" | "youtube";
export type PostPlatform = "tiktok" | "instagram" | "youtube";
export type Tier = "preferred" | "auto" | "restricted" | "blocked";
export type SpikeTier = "S" | "A" | "B" | "rejected";
export type Lane = "spike" | "hourly";
export type Level = "debug" | "info" | "warn" | "error" | "critical";
export type PostingMode = "live" | "dry_run" | "stopped";
export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  counts?: Record<string, number>;
  meta?: Record<string, unknown>;
}

export interface Ref { id: string; label: string; status: string | null }
export interface Trace {
  creator: Ref | null; live_session: Ref | null; video: Ref | null; spike: Ref | null; clip: Ref | null; posts: Ref[];
}

export interface CreatorRef { id: string; handle: string; display_name: string | null; platform: Platform; tier: Tier }

export interface Event {
  id: number; created_at: Iso; level: Level; type: string; service: string | null;
  entity_type: string | null; entity_id: string | null; message: string | null; data: Record<string, unknown> | null;
}
export interface Incident { key: string; title: string; opened_at: Iso; data: Record<string, unknown> | null }

export interface QueueItem {
  clip_id: string; series_no: number | null; hook: string | null; title: string | null; creator: CreatorRef; lane: Lane;
  score: number | null; rank_score: number | null; duration_s: number | null; created_at: Iso; age_s: number; expires_at: Iso | null;
}

export interface AccountSummary {
  id: string; platform: PostPlatform; handle: string | null; lanes: Lane[]; enabled: boolean;
  paused_until: Iso | null; pause_reason: string | null; cap_24h: number; spike_reserve: number; used_24h: number;
  spike_reserve_remaining: number; last_post_at: Iso | null; privacy: string | null; hourly_hours_utc: number[] | null; min_gap_s: number;
}

export interface CapRow {
  account_id: string; used_24h: number; cap_24h: number; free: number; spike_reserve_remaining: number;
  hourly_allowed: boolean; spike_allowed: boolean; gap_ok: boolean; last_post_at: Iso | null; paused_until: Iso | null;
}

export interface Overview {
  now: Iso; env: string; posting_mode: PostingMode; stop_reason: string | null; launch_at: Iso | null; dry_run: boolean;
  posts_24h: { published: number; failed: number; by_hour: number[] };
  queue: { hourly: number; spike: number; low_watermark: number };
  express: { p50_s: number | null; p90_s: number | null; series: { t: Iso; s: number }[] };
  spend: { total_usd: number; budget_usd: number; by_provider: { provider: string; usd: number; budget_usd: number | null }[] };
  accounts: AccountSummary[];
  up_next: QueueItem[];
  recent_events: Event[];
  health: {
    services_ok: number; services_total: number; triggers_ok: number; triggers_total: number;
    open_incidents: Incident[]; jobs: { queued: number; running: number; failed_24h: number }; live: { active: number; max_slots: number };
  };
  growth: {
    accounts: { account_id: string; platform: PostPlatform; followers: number | null; delta_24h: number | null }[];
    ypp: { subscribers: number | null; subscribers_target: number; shorts_views_90d: number | null; views_target: number };
  };
}

export interface Status {
  env: string; posting_mode: PostingMode; emergency_stop: boolean; stop_reason: string | null; dry_run: boolean; launch_at: Iso | null;
  [k: string]: unknown;
}

export interface Pipeline {
  window: string;
  nodes: {
    discovery: { candidates: number; picked: number };
    live: { active: number; max_slots: number; sessions: number };
    vod: { new: number; processing: number; done: number; failed: number; skipped: number };
    spikes: { detected: number; confirmed: number; rejected: number; merged: number };
    render: { rendered: number; failed: number };
    gate: { approved: number; rejected: number };
    queue: { waiting: number; expired: number };
    posts: { published: number; failed: number };
  };
  funnel: { tier: string; detected: number; confirmed: number; clipped: number; posted: number }[];
  rejects: { reason: string; n: number }[];
  express_stages: { stage: string; median_s: number }[];
  express_series: { t: Iso; s: number }[];
  approved_by_hour: { t: Iso; n: number }[];
}

export interface LiveSession {
  id: string; creator: CreatorRef; platform: Platform; status: string; trend_score: number | null; url: string | null;
  channel_login: string | null; reconnects: number; assigned_at: Iso | null; started_at: Iso | null; ended_at: Iso | null;
  below_cutoff_since: Iso | null; spikes: { S: number; A: number; B: number }; clips: number;
}
export interface LiveSessionDetail extends LiveSession {
  bot_names: string[] | null; clipped_ranges: { start_wall: Iso; end_wall: Iso; clip_id: string }[] | null;
  vod_source_id: string | null; buffer_dir: string | null; spikes_list?: Spike[]; events: Event[]; trace: Trace;
}

export interface Video {
  id: string; creator: CreatorRef; platform: Platform; source_id: string; url: string | null; title: string | null;
  duration_s: number | null; published_at: Iso | null; trend_score: number | null; has_chat: boolean | null; status: string;
  skip_reason: string | null; attempts: number; created_at: Iso; updated_at: Iso; clips: number;
}
export interface Job {
  id: number; queue: string; type: string; priority: number; status: string; attempts: number; max_attempts: number;
  run_after: Iso | null; locked_by: string | null; locked_at: Iso | null; last_error: string | null; dedupe_key: string | null;
  created_at: Iso; updated_at: Iso; payload?: Json;
}
export interface VideoDetail extends Video {
  chat_file: { messages_count: number | null; duration_s: number | null; baseline_rate: number | null; bot_names: string[] | null; storage_path: string | null; downloaded_at: Iso | null } | null;
  spikes: Spike[]; jobs: Job[]; events: Event[]; trace: Trace;
}

export interface Spike {
  id: string; mode: "live" | "replay" | "transcript"; creator: CreatorRef; live_session_id: string | null; video_id: string | null;
  detected_at: Iso; spike_wall: Iso | null; spike_offset_s: number | null; score_z: number | null;
  parts: Record<string, number> | null; tier: SpikeTier | null; claude_confidence: number | null; claude_reason: string | null;
  status: string; merged_into: string | null; clip_id: string | null; clip_series_no: number | null; spike_to_post_s: number | null;
  window_start_s: number | null; window_end_s: number | null; window_start_wall: Iso | null; window_end_wall: Iso | null;
}
export interface SpikeDetail extends Spike {
  chat_sample: { t?: string; ts?: string; user?: string; author?: string; text?: string; message?: string }[] | null;
  msgs_in_bucket: number | null; unique_chatters: number | null; reaction_count: number | null; clip_requests: number | null;
  events: Event[]; trace: Trace;
}

export interface Clip {
  id: string; series_no: number | null; spike_id: string | null; creator: CreatorRef; source_platform: Platform;
  source_type: "live" | "vod"; lane: Lane; duration_s: number | null; hook: string | null; title: string | null; topic: string | null;
  score: number | null; rank_score: number | null; music_ratio: number | null; status: string; reject_reason: string | null;
  gate_profile: string | null; created_at: Iso; approved_at: Iso | null; posted_at: Iso | null; has_file: boolean;
}
export interface GateCheck { ok?: boolean; value?: Json; limit?: Json; codes?: string[]; reasons?: string[] }
export interface GateResult {
  profile?: string; passed?: boolean; verdict?: "approve" | "reject" | "hold"; lane?: string; codes?: string[]; reasons?: string[];
  checks?: Record<string, GateCheck>; precheck?: string; risk?: string[]; demoted?: boolean;
  express?: { passed: boolean; codes: string[] }; hourly_fallback?: { passed: boolean; codes: string[] }; decided_at?: Iso; error?: string;
}
export interface ClipDetail extends Clip {
  caption: string | null; hashtags: string[] | null; commentary: string | null;
  transcript: { words?: { w?: string; word?: string; s?: number; start?: number; e?: number; end?: number }[]; text?: string } | null;
  keywords: { line: number; words: string[] }[] | null; safety_flags: Record<string, boolean> | null; score_parts: Record<string, number> | null;
  spoiler: boolean | null; profanity_count: number | null; gate_result: GateResult | null; precheck_result: Json;
  render_ms: number | null; file_size_bytes: number | null; storage_path: string | null; start_s: number | null; end_s: number | null;
  original_language: string | null; posts: Post[]; events: Event[]; trace: Trace;
}
export interface ClipMedia { url: string | null; expires_at: Iso | null; reason: "ok" | "no_file" | "deleted" }

export interface Post {
  id: string; clip_id: string; clip_series_no: number | null; clip_hook: string | null; account_id: string; platform: PostPlatform;
  lane: Lane; slot_at: Iso | null; status: string; dry_run: boolean; post_url: string | null; platform_post_id: string | null;
  published_at: Iso | null; views: number | null; likes: number | null; comments: number | null; shares: number | null;
  claim_status: string | null; error: string | null; attempts: number; created_at: Iso;
}
export interface PostDetail extends Post {
  upload_request_id: string | null; idempotency_key: string; stats_updated_at: Iso | null; claim_checked_at: Iso | null;
  removed_at: Iso | null; remove_reason: string | null; events: Event[]; trace: Trace;
}

export interface QueueView {
  ranked: { hourly: QueueItem[]; spike: QueueItem[] };
  slots: { account_id: string; platform: PostPlatform; hours: { hour: number; state: "posted" | "failed" | "skipped" | "planned" | "not_allowed" | "empty"; post_id: string | null }[] }[];
  caps: CapRow[]; decisions: Event[]; next_slot_at: Iso | null;
}

export interface Creator {
  id: string; platform: Platform; channel_id: string; handle: string | null; display_name: string | null; language: string | null;
  tier: Tier; tier_reason: string | null; follower_count: number | null; avg_clip_views: number | null; claims_count: number;
  description_checked_at: Iso | null; discovered_at: Iso; clips_posted: number;
}
export interface CreatorDetail extends Creator {
  layout: { source_width: number | null; source_height: number | null; facecam_box: { x: number; y: number; w: number; h: number } | null; gameplay_box: { x: number; y: number; w: number; h: number } | null; confidence: number | null; detected_at: Iso | null; expires_at: Iso | null } | null;
  counts: { live_sessions: number; videos: number; spikes: number; clips: number; posts: number; takedowns: number };
  clips: Clip[]; events: Event[]; trace?: Trace;
}

export interface AccountDetail extends AccountSummary {
  caps: CapRow; stats: { captured_at: Iso; followers: number | null; views_total: number | null }[];
  views_by_day: { day: string; views: number }[]; posts: Post[]; events: Event[];
}

export interface Discovery {
  runs: Event[];
  source_health: { discovery: Record<string, unknown> | null; live: Record<string, unknown> | null; vod: Record<string, unknown> | null };
  apify_last_run: Record<string, unknown> | null;
  quotas: {
    youtube_units_today: number; youtube_units_limit: number; search_calls_today: number; search_calls_limit: number;
    apify_today_usd: number; apify_daily_budget_usd: number; apify_month_usd: number; apify_monthly_budget_usd: number;
  };
}
export interface Candidate {
  id: number; creator: CreatorRef; source_type: "live" | "vod"; platform: Platform; source_id: string; url: string | null; title: string | null;
  language: string | null; viewers: number | null; views: number | null; views_per_hour: number | null; likes: number | null; comments: number | null;
  published_at: Iso | null; started_at: Iso | null; trend_score: number; score_parts: Record<string, number> | null; checked_at: Iso; picked: boolean;
}

export interface Guardian {
  strikes: { count: number; limit: number; window_d: number; items: { at: Iso; creator: CreatorRef | null; takedown_id: string | null }[] };
  emergency_stop: boolean; stop_reason: string | null;
  manual_removals: Post[];
  claims: { last_run_at: Iso | null; ok: number; suspect: number; unverifiable: number; removed_by_platform: number };
  takedown_counts: { open: number; actioned: number; partial: number; ignored: number };
}
export interface Takedown {
  id: string; received_at: Iso; source: string; sender: string | null; subject: string | null; body_excerpt: string | null;
  is_takedown: boolean | null; creator: CreatorRef | null; matched_post_ids: string[] | null; actions: Json; status: string; message_id: string | null;
}
export interface TakedownDetail extends Takedown { matched_posts: Post[]; events: Event[] }

export interface Heartbeat { name: string; kind: string; last_seen: Iso; age_s: number; stale: boolean; info: Record<string, unknown> | null }
export interface Health {
  heartbeats: Heartbeat[];
  triggers: { name: string; last_seen: Iso | null; grace_s: number; source: string | null; stale: boolean }[];
  open_incidents: Incident[]; incidents: Event[]; jobs: { queued: number; running: number; failed_24h: number };
}
export interface Alert { id: number; created_at: Iso; severity: "info" | "warn" | "critical"; title: string; body: string | null; sent_at: Iso | null; attempts: number }

export interface Usage {
  totals: { provider: string; usd: number }[];
  budgets: { anthropic: number | null; transcribe: number | null; apify_daily: number | null; apify_monthly: number | null; month_ceiling: number | null };
  daily: { day: string; provider: string; usd: number }[];
  operations: { provider: string; operation: string; model: string | null; calls: number; input_tokens: number | null; output_tokens: number | null; units: number | null; usd: number }[];
  quotas: Record<string, number>;
}

export interface Setting { key: string; value: Json; description: string | null; updated_at: Iso; group: string; written_by_code: boolean }
export interface SearchResult { kind: string; id: string; label: string; sub: string | null; href: string }

export interface DailyReport { day: string; [k: string]: unknown }
