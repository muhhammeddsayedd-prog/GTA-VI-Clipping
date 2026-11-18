import type { Alert, Event, Health, Job, Json, Setting, Usage } from "@/lib/api/types";
import { accounts, ago, ahead, events, list, rng, uid } from "./data";
import { need, withStatus, type MockRoute } from "./util";

/* ---------------- health ---------------- */
function health(): Health {
  const hb = (name: string, kind: string, age: number, info: Record<string, unknown> | null = null) => ({
    name, kind, last_seen: ago(age), age_s: age, stale: age > 300, info,
  });
  return {
    heartbeats: [
      hb("scheduler", "scheduler", 8, { pid: 41 }),
      hb("live-manager", "live-manager", 12, { active: 4, max_slots: 6, blocked: { youtube: ahead(2400) } }),
      hb("worker-express-a1f3-112", "worker-express", 5, { queue: "express", pid: 112 }),
      hb("worker-express-b7c2-118", "worker-express", 7, { queue: "express", pid: 118 }),
      hb("worker-normal-c1d0-201", "worker-normal", 9, { queue: "normal", pid: 201 }),
      hb("worker-normal-c3e9-207", "worker-normal", 11, { queue: "normal", pid: 207 }),
      hb("worker-normal-d4a8-213", "worker-normal", 6, { queue: "normal", pid: 213 }),
      hb("worker-normal-e5b1-219", "worker-normal", 14, { queue: "normal", pid: 219 }),
      hb("api", "api", 2, null),
    ],
    triggers: [
      { name: "discovery", last_seen: ago(240), grace_s: 1500, source: "n8n", stale: false },
      { name: "hourly", last_seen: ago(1920), grace_s: 4200, source: "n8n", stale: false },
      { name: "guardian", last_seen: ago(360), grace_s: 2700, source: "n8n", stale: false },
      { name: "stats", last_seen: ago(6480), grace_s: 14400, source: "failover", stale: false },
    ],
    open_incidents: [
      { key: "disk", title: "Disk is filling up", opened_at: ago(3120), data: { pct: 81, warn_pct: 80, path: "/data", live_buffers_gb: 14, vod_windows_gb: 9, clips_gb: 4, chat_gb: 2 } },
      { key: "vod_download_blocked:youtube", title: "Video downloads are blocked (YouTube)", opened_at: ago(19500), data: { platform: "youtube", failures: 4 } },
    ],
    incidents: incidentEvents,
    jobs: { queued: 9, running: 6, failed_24h: 3 },
  };
}
const INC: [string, string, string, number][] = [
  ["health.incident", "disk", "Disk is filling up · 80.4%", 3120],
  ["health.recovered", "queue_low", "Recovered: clip queue is low · back to 18", 3780],
  ["health.incident", "queue_low", "Clip queue is low · 11 approved", 9000],
  ["health.recovered", "trigger:stats", "n8n trigger is back: stats", 15000],
  ["health.incident", "trigger:stats", "n8n trigger missing: stats · scheduler ran it", 15500],
  ["health.incident", "vod_download_blocked:youtube", "Video downloads are blocked (YouTube)", 19500],
  ["health.recovered", "restart_storm:worker-normal", "Restart storm over: worker-normal", 86400 + 3000],
  ["health.incident", "restart_storm:worker-normal", "Restart storm: worker-normal · 4 restarts in 1 h", 86400 + 3840],
];
const incidentEvents: Event[] = INC.map(([type, key, msg, age], i) => ({
  id: 9100 - i, created_at: ago(age), level: type === "health.incident" ? "warn" : "info", type, service: "scheduler",
  entity_type: "incident", entity_id: key, message: msg, data: { key, ...(key === "disk" ? { pct: 80.4 } : {}) },
}));

/* ---------------- alerts ---------------- */
const AL: [Alert["severity"], string, string, number, boolean][] = [
  ["critical", "Takedown email needs you: nothing of ours matched", "From legal@agency… · “Removal request: unauthorised use of content”", 2460, true],
  ["warn", "Disk is filling up", "/data at 80.4% · live buffers 14 GB", 3120, true],
  ["warn", "Post failed on youtube_main", "youtube_channel_daily_limit · retry in 2 min", 5400, true],
  ["warn", "Rate limit on youtube_main", "Paused until 15:12 · cap 30 → 28", 4800, true],
  ["info", "Recovered: clip queue is low", "Queue back to 18", 3780, true],
  ["warn", "Video downloads are blocked", "YouTube refuses downloads from this server", 19500, true],
  ["critical", "Emergency stop OFF", "Posting may run again. The stop reason was: checking a takedown", 23000, true],
  ["info", "Setting changed: gate.hourly", "{\"min_score\":7} → {\"min_score\":7.5}", 25000, true],
  ["info", "Daily report", "Sent at 00:00 UTC", 52000, true],
  ["warn", "Hourly queue empty", "2 slots in a row had nothing to post", 90000, false],
];
const alerts: Alert[] = Array.from({ length: 34 }, (_, i) => {
  const [severity, title, body, age, sent] = AL[i % AL.length];
  const a = age + Math.floor(i / AL.length) * 86400;
  return { id: 700 - i, created_at: ago(a), severity, title, body, sent_at: sent ? ago(a - 2) : null, attempts: sent ? 1 : 3 };
}).sort((x, y) => Date.parse(y.created_at) - Date.parse(x.created_at));

/* ---------------- jobs ---------------- */
const JT: [string, string, number][] = [
  ["express.render", "express", 100], ["clip.render", "normal", 60], ["vod.process", "normal", 30], ["clip.gate", "normal", 0],
  ["vod.process", "normal", 30], ["post.retry", "express", 90], ["creator.permission", "normal", 20], ["replay.find", "normal", 25],
  ["post.hourly", "express", 50], ["replay.scan", "normal", 25], ["vod.cleanup", "normal", 0], ["clip.render", "normal", 40],
];
const jobs: Job[] = Array.from({ length: 140 }, (_, i) => {
  const [type, queue, priority] = JT[i % JT.length];
  const status = i < 3 ? "running" : i === 3 || i === 7 ? "queued" : i === 4 || i === 31 || i === 57 ? "failed" : i === 20 ? "cancelled" : i < 12 && i % 3 === 0 ? "queued" : "done";
  const failed = status === "failed";
  const created = 60 + i * 431;
  return {
    id: 88231 - i, queue, type, priority, status, attempts: failed ? 3 : status === "queued" ? 0 : 1 + (i % 7 === 0 ? 1 : 0), max_attempts: 3,
    run_after: status === "queued" ? ahead(i === 7 ? 1080 : 0) : ago(created), locked_by: status === "running" ? `worker-${queue}-${["a1f3", "c3e9", "c1d0"][i % 3]}` : null,
    locked_at: status === "running" ? ago(30 + i * 20) : null,
    last_error: failed ? (type === "vod.process" ? "ERROR: [youtube] Sign in to confirm you’re not a bot.\nThis helps protect our community." : "TimeoutError: render exceeded 300 s") : null,
    dedupe_key: type.startsWith("vod") ? `vod:${uid("video", i % 14)}` : type.includes("render") ? `${type}:${uid("spike", i)}` : null,
    created_at: ago(created), updated_at: ago(Math.max(created - 120, 5)),
    payload: (type.startsWith("vod") ? { video_id: uid("video", i % 14), url: "https://youtube.com/watch?v=…", attempt: failed ? 3 : 1 }
      : type.includes("render") ? { spike_id: uid("spike", i), lane: queue === "express" ? "spike" : "hourly" }
      : type === "post.hourly" ? { slot_at: ago(created) } : { clip_id: uid("clip", i % 40) }) as Json,
  };
});

/* ---------------- usage ---------------- */
function usage(range: string): Usage & { range: string } {
  const days = range === "today" ? 1 : range === "7d" ? 7 : 18;
  const rr = rng(5);
  const daily: Usage["daily"] = [];
  for (let d = days - 1; d >= 0; d--) {
    const day = ago(d * 86400).slice(0, 10);
    const grow = 1 + (days - d) / days;
    daily.push({ day, provider: "anthropic", usd: +(4 + rr() * 4 * grow + (d === 0 ? 3 : 0)).toFixed(2) });
    daily.push({ day, provider: "openai", usd: +(0.8 + rr() * 1.4).toFixed(2) });
    daily.push({ day, provider: "apify", usd: +(0.2 + rr() * 0.2).toFixed(2) });
    daily.push({ day, provider: "youtube", usd: 0.05 });
  }
  const sum = (p: string) => +daily.filter((x) => x.provider === p).reduce((a, x) => a + x.usd, 0).toFixed(2);
  const scale = range === "today" ? 1 : range === "7d" ? 6.4 : 15;
  return {
    range,
    totals: [{ provider: "anthropic", usd: sum("anthropic") }, { provider: "openai", usd: sum("openai") }, { provider: "apify", usd: sum("apify") }, { provider: "youtube", usd: sum("youtube") }],
    budgets: { anthropic: 25, transcribe: 5, apify_daily: 0.4, apify_monthly: 12, month_ceiling: 840 },
    daily,
    operations: [
      { provider: "anthropic", operation: "clip_writer", model: "claude-sonnet-5-5", calls: 58, input_tokens: 412000, output_tokens: 61000, units: null, usd: 6.21 },
      { provider: "anthropic", operation: "spike_confirm", model: "claude-haiku-5-5", calls: 1240, input_tokens: 3100000, output_tokens: 88000, units: null, usd: 2.3 },
      { provider: "anthropic", operation: "visual_check", model: "claude-haiku-5-5", calls: 58, input_tokens: 1900000, output_tokens: 12000, units: null, usd: 1.96 },
      { provider: "anthropic", operation: "vod_moment_picker", model: "claude-sonnet-5-5", calls: 3, input_tokens: 220000, output_tokens: 4000, units: null, usd: 0.48 },
      { provider: "anthropic", operation: "takedown_classify", model: "claude-haiku-5-5", calls: 15, input_tokens: 40000, output_tokens: 3000, units: null, usd: 0.01 },
      { provider: "openai", operation: "transcribe", model: "whisper-1", calls: 61, input_tokens: null, output_tokens: null, units: 19200, usd: 1.92 },
      { provider: "apify", operation: "youtube_search", model: null, calls: 1, input_tokens: null, output_tokens: null, units: 70, usd: 0.21 },
      { provider: "youtube", operation: "videos.list", model: null, calls: 124, input_tokens: null, output_tokens: null, units: 1240, usd: 0 },
    ].map((o) => ({
      ...o, calls: Math.round(o.calls * scale), usd: +(o.usd * scale).toFixed(2),
      input_tokens: o.input_tokens && Math.round(o.input_tokens * scale), output_tokens: o.output_tokens && Math.round(o.output_tokens * scale), units: o.units && Math.round(o.units * scale),
    })),
    quotas: { youtube_units_today: 1240, youtube_units_limit: 9000, search_calls_today: 14, search_calls_limit: 60, transcribe_local_s_today: 1710, transcribe_local_s_limit: 21600, month_usd: 214.3 },
  };
}

/* ---------------- settings ---------------- */
const CODE_WRITTEN = new Set(["discovery.apify_last_run", "discovery.rss_cursor", "discovery.twitch_game_cache", "discovery.search_api_state", "discovery.source_health", "live.source_health", "vod.source_health", "vod.paused_until"]);
const S: [string, Json, string][] = [
  ["control.emergency_stop", false, "Stop all posting. Only a human clears it."],
  ["control.stop_reason", null, "Why posting was stopped."],
  ["launch.dry_run", false, "Pick and log posts but never upload."],
  ["launch.launch_at", "2026-11-18T11:00:00Z", "No real posts before this time."],
  ["launch.summary_lead_s", 900, "Send the health summary this long before launch."],
  ["env.target_game", "Grand Theft Auto VI", "Game name used by discovery."],
  ["env.search_terms", ["GTA 6", "GTA VI", "Grand Theft Auto VI"], "Search terms for discovery."],
  ["env.game_release_at", "2026-11-19T00:00:00Z", "Game release time."],
  ["discovery.interval_s", 600, "Seconds between discovery passes."],
  ["discovery.vod_lookback_h", 48, "How far back to look for finished streams."],
  ["discovery.live_score_cutoff", 55, "Minimum trend score to take a live slot."],
  ["discovery.vod_score_cutoff", 50, "Minimum trend score to queue a VOD."],
  ["discovery.max_vod_per_run", 6, "VODs queued per pass at most."],
  ["discovery.release_after_below_s", 1200, "Release a live slot after this long below the cutoff."],
  ["discovery.non_english_penalty", 0.85, "Score multiplier for non-English streams."],
  ["discovery.preferred_bonus", 15, "Score bonus for preferred creators."],
  ["discovery.title_blocklist", ["giveaway", "24/7", "rerun"], "Titles containing these are skipped."],
  ["discovery.apify_daily_budget_usd", 0.4, "Apify spend cap per day."],
  ["discovery.apify_monthly_budget_usd", 12, "Apify spend cap per month."],
  ["discovery.youtube_quota_daily_units", 9000, "YouTube Data API units per day."],
  ["discovery.excluded_channels", [], "Channels discovery never picks."],
  ["discovery.source_health", { yt_dlp: { blocked_since: ago(19500), failures: 4 } }, "Written by discovery."],
  ["discovery.apify_last_run", { run_id: "k2Jx…", cost_usd: 0.21, results: 70 }, "Written by discovery."],
  ["live.max_slots", 6, "Streams recorded at once."],
  ["live.segment_s", 60, "Recording segment length."],
  ["live.buffer_s", 1800, "Rolling buffer kept per stream."],
  ["live.max_reconnects", 3, "Recorder reconnects before giving up."],
  ["live.ab_clips", true, "Also render tier A and B spikes from live streams."],
  ["live.source_health", { youtube: { blocked_until: ahead(2400) } }, "Written by the live manager."],
  ["spikes.z_s", 3.4, "z-score for tier S."],
  ["spikes.z_a", 2.6, "z-score for tier A."],
  ["spikes.z_b", 2.0, "z-score for tier B."],
  ["spikes.s_min_confidence", 0.8, "Claude confidence needed to keep S."],
  ["spikes.ab_min_confidence", 0.55, "Claude confidence needed to keep A or B."],
  ["spikes.warmup_s", 900, "Ignore the first seconds of a stream."],
  ["spikes.lead_s", 20, "Clip starts this long before the peak."],
  ["spikes.weights", { msgs: 0.15, uniq: 0.15, react: 0.55, clipreq: 0.1, audio: 0.05 }, "Weights of the z-score parts."],
  ["vod.max_spikes_per_video", 25, "Spikes kept per video."],
  ["vod.fallback_enabled", true, "Use the transcript picker when chat is thin."],
  ["vod.fallback_below_spikes", 3, "Use the fallback below this many spikes."],
  ["vod.paused_until", null, "Written by guardian when spend is over budget."],
  ["vod.source_health", { youtube: { blocked_since: ago(19500) } }, "Written by the VOD lane."],
  ["factory.crf", 21, "Video quality (lower is better)."],
  ["factory.loudness_lufs", -14, "Target loudness."],
  ["factory.watermark", "@6starsclipping", "Watermark text."],
  ["factory.bleep", true, "Bleep profanity."],
  ["ai.model_writer", "claude-sonnet-5-5", "Model for the clip writer."],
  ["ai.model_fast", "claude-haiku-5-5", "Model for fast checks."],
  ["ai.daily_budget_usd", 25, "Anthropic spend cap per day."],
  ["transcribe.daily_budget_usd", 5, "Transcription spend cap per day."],
  ["gate.hourly", { min_len: 20, max_len: 60, max_music_ratio: 0.1, min_score: 7.5, topic_dedupe_h: 48, precheck_risky: true }, "Quality-gate thresholds for hourly-lane clips."],
  ["gate.express", { min_len: 15, max_len: 45, max_music_ratio: 0, min_score: 8, topic_dedupe_h: 6 }, "Quality-gate thresholds for express clips."],
  ["gate.spoiler_block_until", "2026-12-03T00:00:00Z", "Block story spoilers until this date."],
  ["gate.auto_creator_daily_cap", 4, "Posts per day for creators with no statement."],
  ["gate.strict_max_music_ratio", 0.0, "Music allowed in clips needing the strict check."],
  ["precheck.enabled", true, "Upload privately to YouTube first to catch claims."],
  ["precheck.daily_max", 20, "Pre-checks per day."],
  ["posting.min_gap_s", 900, "Minimum gap between two posts on one account."],
  ["posting.spike_max_per_hour", 2, "Spike posts per account per hour."],
  ["posting.queue_low_watermark", 12, "Alert when fewer approved clips wait."],
  ["posting.max_clip_age_h", 72, "Approved clips expire after this."],
  ["posting.platform_max_24h", { tiktok: 15, instagram: 50, youtube: 30 }, "Hard ceilings per platform."],
  ["posting.staging_allow_instagram", false, "Allow Instagram posts from staging (always public)."],
  ["guardian.strike_limit", 2, "Strikes in the window that trigger the emergency stop."],
  ["guardian.strike_window_d", 7, "Strike window in days."],
  ["guardian.heartbeat_stale_s", 300, "A service is down after this long without a heartbeat."],
  ["guardian.failover_enabled", true, "Run n8n triggers from the scheduler when n8n misses them."],
  ["guardian.disk_warn_pct", 80, "Warn when the data volume is this full."],
  ["stats.post_lookback_d", 7, "Refresh post stats for this many days."],
  ["reports.ypp_targets", { subscribers: 1000, shorts_views_90d: 10000000 }, "YouTube Partner Program targets."],
  ["cleanup.posted_clip_days", 3, "Delete posted clip files after this many days."],
  ["cleanup.events_days", 30, "Keep events this many days."],
];
const settingsStore: Setting[] = S.map(([key, value, description], i) => ({
  key, value, description, updated_at: ago(86400 * (1 + (i % 4)) + i * 60), group: key.split(".")[0], written_by_code: CODE_WRITTEN.has(key),
}));
settingsStore.find((s) => s.key === "gate.hourly")!.updated_at = ago(25000);
const settingHistory: Event[] = [
  { id: 9301, created_at: ago(25000), level: "warn", type: "setting.changed", service: "api", entity_type: "setting", entity_id: "gate.hourly", message: "gate.hourly changed through the control API", data: { key: "gate.hourly", old: { min_score: 7 }, new: { min_score: 7.5 }, changed: true } },
  { id: 9300, created_at: ago(23000), level: "warn", type: "control.start", service: "api", entity_type: "setting", entity_id: "control.emergency_stop", message: "emergency stop cleared", data: { changed: true } },
  { id: 9299, created_at: ago(26000), level: "critical", type: "control.stop", service: "api", entity_type: "setting", entity_id: "control.emergency_stop", message: "emergency stop set: checking a takedown", data: { changed: true } },
];

function kind(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  return typeof v === "object" ? "object" : typeof v === "boolean" ? "boolean" : typeof v === "number" ? "number" : "string";
}

/* ---------------- daily report (mirrors stats/report.py build()) ---------------- */
function report(dayParam: string | null) {
  const day = dayParam && /^\d{4}-\d{2}-\d{2}$/.test(dayParam) ? dayParam : ago(86400).slice(0, 10);
  const rr = rng(Number(day.replace(/-/g, "")) % 9973);
  const k = 0.75 + rr() * 0.5;
  return {
    day, timezone: "UTC", generated_at: new Date().toISOString(), env: "dev",
    accounts: accounts.map((a, i) => ({
      account_id: a.id, platform: a.platform, handle: a.handle, enabled: a.enabled, paused_until: a.paused_until,
      followers: a.enabled ? Math.round([2184, 906, 412, 0][i] * k) : null, followers_change: a.enabled ? Math.round([312, 88, 61, 0][i] * k) : null,
      views_total: a.enabled ? Math.round([248000, 71000, 93000, 0][i] * k) : null, views_change: a.enabled ? Math.round([41000, 9000, 12000, 0][i] * k) : null,
      published: a.enabled ? [14, 27, 27, 0][i] : 0, failed: i === 2 ? 1 : 0, dry_run: 0,
    })),
    views: { total: Math.round(412000 * k), change: Math.round(62000 * k), median_by_platform: { instagram: 2600, tiktok: 11200, youtube: 3400 } },
    top_clips: [
      { title: "6 stars in 40 seconds", views: Math.round(61200 * k), platform: "tiktok", post_url: "https://tiktok.com/@6starsclipping/video/1" },
      { title: "The gator won", views: Math.round(44800 * k), platform: "tiktok", post_url: "https://tiktok.com/@6starsclipping/video/2" },
      { title: "Chat called it", views: Math.round(21000 * k), platform: "youtube", post_url: "https://youtube.com/shorts/x" },
    ],
    ypp: { subscribers: Math.round(412 * k), subscribers_target: 1000, shorts_views_90d: Math.round(1240000 * k), shorts_views_target: 10000000 },
    posts: { hourly_published: 24, spike_published: 9, failed: 1, dry_run: 0, slots_used: 23, slots_skipped_events: 3 },
    spikes: { detected: 301, S: 11, A: 34, B: 47, rejected: 209, median_spike_to_post_s: 97 },
    clips: { rendered: 58, approved: 41, rejected: 17, rejected_by_reason: { score_too_low: 6, music_over_limit: 4, duplicate_topic: 3, spoiler_blocked: 2, too_short: 1, black_frames: 1 }, expired: 3, queue: { spike: 3, hourly: 15, low_watermark: 12 } },
    discovery: { videos_processed: 12, candidates_checked: 1284, new_creators: 9, new_by_tier: { auto: 7, preferred: 1, restricted: 1 } },
    guardian: { claims: 0, takedowns: 1, emails_ignored: 14, emails_open: 1, strikes: 1, strike_limit: 2, emergency_stop: false, stop_reason: null, manual_removals: [{ platform: "tiktok", post_url: "https://tiktok.com/@6starsclipping/video/119" }] },
    spend: [
      { provider: "Anthropic", spent_usd: +(22.1 * k).toFixed(2), budget_usd: 25 },
      { provider: "Transcription", spent_usd: +(3.9 * k).toFixed(2), budget_usd: 5 },
      { provider: "Apify", spent_usd: 0.38, budget_usd: 0.4 },
    ],
    issues: ["09:12 Video downloads are blocked (YouTube)", "13:12 Rate limit on youtube_main · cap now 28", "15:40 1 post still needs a manual delete (#0119 on tiktok_main)"],
  };
}

function allEvents(): Event[] {
  return [...events, ...incidentEvents, ...settingHistory].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
}

export const routes: MockRoute[] = [
  { method: "GET", pattern: /^admin\/health$/, handle: () => health() },
  {
    method: "GET", pattern: /^admin\/alerts$/, handle: (_m, q) => {
      const d = q.get("delivered");
      return list(alerts, q, "severity", (a) => !d || (d === "true" ? !!a.sent_at : !a.sent_at));
    },
  },
  {
    method: "GET", pattern: /^admin\/jobs$/, handle: (_m, q) => {
      const res = list(jobs.map((j) => ({ ...j, payload: undefined })), q, "status",
        (j) => (!q.get("queue") || j.queue === q.get("queue")) && (!q.get("type") || j.type === q.get("type")));
      const by_queue: Record<string, Record<string, number>> = { express: {}, normal: {} };
      for (const j of jobs) by_queue[j.queue][j.status] = (by_queue[j.queue][j.status] ?? 0) + 1;
      return { ...res, meta: { by_queue } };
    },
  },
  { method: "GET", pattern: /^admin\/jobs\/(\d+)$/, handle: (m) => need(jobs.find((j) => j.id === Number(m[1]))) },
  {
    method: "GET", pattern: /^admin\/events$/, handle: (_m, q) => {
      const prefix = q.get("type_prefix");
      return list(allEvents(), q, "level", (e) =>
        (!prefix || e.type.startsWith(prefix)) && (!q.get("service") || e.service === q.get("service")) &&
        (!q.get("entity_type") || e.entity_type === q.get("entity_type")) && (!q.get("entity_id") || e.entity_id === q.get("entity_id")));
    },
  },
  {
    method: "GET", pattern: /^admin\/events\/(\d+)$/, handle: (m) => {
      const all = allEvents();
      const e = need(all.find((x) => x.id === Number(m[1])));
      const t = Date.parse(e.created_at);
      const related = all.filter((x) => x.id !== e.id && x.entity_type === e.entity_type && Math.abs(Date.parse(x.created_at) - t) < 3_600_000).slice(0, 8);
      return { ...e, related };
    },
  },
  { method: "GET", pattern: /^admin\/usage$/, handle: (_m, q) => usage(q.get("range") ?? "today") },
  { method: "GET", pattern: /^admin\/settings$/, handle: () => ({ items: settingsStore, next_cursor: null }) },
  { method: "GET", pattern: /^admin\/settings\/history$/, handle: (_m, q) => list(settingHistory, q, "level") },
  { method: "GET", pattern: /^reports\/daily$/, handle: (_m, q) => report(q.get("day")) },
  {
    method: "POST", pattern: /^control\/settings$/, handle: (_m, _q, body) => {
      const b = body as { key?: string; value?: Json } | undefined;
      // error bodies mirror the control API: {error: "<message>", code, request_id}
      const err = (code: string, message: string) => ({ error: message, code, request_id: "mock" });
      if (!b || typeof b.key !== "string" || !("value" in b)) return withStatus(422, err("invalid_value", "key and value are required"));
      const s = settingsStore.find((x) => x.key === b.key);
      if (!s) return withStatus(404, err("unknown_setting", `there is no setting named ${b.key}`));
      const ok = kind(s.value) === kind(b.value) || kind(s.value) === "null" || (kind(s.value) === "string" && b.value === null);
      if (!ok) return withStatus(422, err("type_mismatch", `${b.key} is a JSON ${kind(s.value)}; a ${kind(b.value)} was sent`));
      const previous = s.value;
      const changed = JSON.stringify(previous) !== JSON.stringify(b.value);
      s.value = b.value as Json;
      s.updated_at = new Date().toISOString();
      if (changed) {
        settingHistory.unshift({ id: 9400 + settingHistory.length, created_at: s.updated_at, level: "warn", type: "setting.changed", service: "api", entity_type: "setting", entity_id: s.key, message: `${s.key} changed through the control API`, data: { key: s.key, old: previous, new: b.value as Json, changed } });
      }
      return { key: s.key, value: s.value, previous, description: s.description, updated_at: s.updated_at, changed };
    },
  },
];
