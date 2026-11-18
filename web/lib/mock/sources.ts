import type {
  AccountDetail, Candidate, CapRow, CreatorDetail, Discovery, Guardian, Post, Takedown, TakedownDetail,
} from "@/lib/api/types";
import { accounts, ago, ahead, clips, cref, creators, events, list, posts, rng, uid } from "./data";
import { controlState, need, type MockRoute } from "./util";

/* ---------------- discovery ---------------- */
const r = rng(99);
const CAND_TITLES = ["Bridge chaos, 6 stars or bust", "Boats in the Keys", "FIRST TIME IN LEONIDA (no commentary)", "GTA VI en español",
  "every wanted level explained", "RP server day 1", "Heist night w/ the crew", "Cops only RP, day 1", "Night drive, chat picks the route", "Stunt jumps 1–50"];

const candidates: Candidate[] = Array.from({ length: 64 }, (_, i) => {
  const c = creators[i % creators.length];
  const live = i % 3 !== 2;
  const score = Math.max(12, Math.round(88 - i * 1.1 - r() * 8));
  const cutoff = live ? 55 : 50;
  const picked = score >= cutoff && c.tier !== "restricted" && c.tier !== "blocked";
  return {
    id: 90000 - i, creator: cref(c), source_type: live ? "live" : "vod", platform: c.platform,
    source_id: live ? `${4120000 + i}` : `v${2291847301 + i}`, url: `https://${c.platform === "twitch" ? "twitch.tv" : "youtube.com"}/${c.handle}`,
    title: CAND_TITLES[i % CAND_TITLES.length], language: i % 9 === 3 ? "es" : "en",
    viewers: live ? Math.round(400 + r() * 8000) : null, views: live ? null : Math.round(20000 + r() * 200000),
    views_per_hour: live ? null : Math.round(800 + r() * 9000), likes: live ? null : Math.round(r() * 9000), comments: live ? null : Math.round(r() * 900),
    published_at: live ? null : ago(3600 * (2 + i)), started_at: live ? ago(600 + i * 400) : null, trend_score: score,
    score_parts: (live
      ? { viewers: +(score * 0.38).toFixed(1), growth: +(score * 0.22).toFixed(1), chat_rate: +(score * 0.2).toFixed(1), global_reach: +(score * 0.11).toFixed(1), history: +(score * 0.09).toFixed(1) }
      : { views_per_hour: +(score * 0.4).toFixed(1), engagement: +(score * 0.25).toFixed(1), size_ratio: +(score * 0.15).toFixed(1), global_reach: +(score * 0.1).toFixed(1), history: +(score * 0.1).toFixed(1) }) as Record<string, number>,
    checked_at: ago(240 + Math.floor(i / 8) * 600), picked,
  };
});

function discovery(): Discovery {
  const runs = Array.from({ length: 12 }, (_, i) => ({
    id: 9100 - i, created_at: ago(240 + i * 600), level: (i === 3 ? "warn" : "info") as "warn" | "info",
    type: i === 3 ? "discovery.failed" : "discovery.run", service: "api", entity_type: null, entity_id: null,
    message: i === 3 ? "discovery pass timed out after 60 s" : `${312 - i * 4} live seen · ${i % 4 === 1 ? 1 : 0} assigned · ${(i * 3) % 5} videos queued`,
    data: i === 3 ? { error: "timeout", duration_s: 60, source: "n8n" } : {
      live_seen: 312 - i * 4, assigned: i % 4 === 1 ? 1 : 0, released: i % 6 === 1 ? 1 : 0, videos_queued: (i * 3) % 5,
      new_creators: i % 3 === 0 ? 1 : 0, duration_s: 19 + (i % 5), source: i === 4 ? "failover" : "n8n",
    },
  }));
  return {
    runs,
    source_health: {
      discovery: {
        twitch_helix: { ok: true, last_ok_at: ago(240), failures: 0, detail: "312 live · 50 videos" },
        youtube_rss: { ok: true, last_ok_at: ago(240), failures: 0, detail: "50 of 214 channels per run" },
        yt_dlp_search: { ok: false, blocked_since: ago(19200), backoff_until: ahead(2280), failures: 2, last_error: "Sign in to confirm you’re not a bot" },
        data_api_search: { ok: true, last_ok_at: ago(1800), failures: 0, detail: "fallback mode" },
        apify: { ok: true, last_ok_at: ago(9000), failures: 0, detail: "every 6 h · max 5 results" },
      },
      live: { youtube: { blocked_since: ago(7200), until: ahead(2280), failures: 2 } },
      vod: { youtube: { blocked_since: ago(19200), last_error: "Sign in to confirm you’re not a bot", count: 14 } },
    },
    apify_last_run: { run_id: "kT9xQ2mPz", started_at: ago(9000), status: "SUCCEEDED", results: 5, cost_usd: 0.21 },
    quotas: {
      youtube_units_today: 1240, youtube_units_limit: 9000, search_calls_today: 14, search_calls_limit: 60,
      apify_today_usd: 0.21, apify_daily_budget_usd: 0.4, apify_month_usd: 6.12, apify_monthly_budget_usd: 12,
    },
  };
}

/* ---------------- creators ---------------- */
function creatorDetail(id: string): CreatorDetail {
  const c = need(creators.find((x) => x.id === id));
  const own = clips.filter((x) => x.creator.id === c.id);
  const i = creators.indexOf(c);
  return {
    ...c,
    layout: c.platform === "twitch" || i % 2 === 0 ? {
      source_width: 1920, source_height: 1080,
      facecam_box: i % 3 === 2 ? null : { x: 1400, y: 690, w: 460, h: 340 },
      gameplay_box: { x: 80, y: 60, w: 1190, h: 930 }, confidence: 0.91, detected_at: ago(86400 * 2), expires_at: ahead(86400 * 5),
    } : null,
    counts: {
      live_sessions: Math.max(0, 14 - i), videos: Math.max(0, 9 - i), spikes: Math.max(0, 211 - i * 17), clips: own.length,
      posts: posts.filter((p) => own.some((cl) => cl.id === p.clip_id)).length, takedowns: c.tier === "blocked" ? 1 : 0,
    },
    clips: own.slice(0, 12),
    events: [
      ...(c.tier === "preferred" ? [{ id: 7001 + i, created_at: ago(86400 * 2), level: "info" as const, type: "creator.tier_changed", service: "worker-normal", entity_type: "creator", entity_id: c.id, message: "auto → preferred · P6 permission_read", data: { old: "auto", new: "preferred" } }] : []),
      ...(c.tier === "blocked" ? [{ id: 7101 + i, created_at: ago(86400 * 3), level: "critical" as const, type: "creator.blocked", service: "api", entity_type: "creator", entity_id: c.id, message: c.tier_reason ?? "blocked by guardian", data: { reason: "takedown" } }] : []),
      { id: 7201 + i, created_at: ago(86400 * 6), level: "info" as const, type: "creator.discovered", service: "api", entity_type: "creator", entity_id: c.id, message: "first seen by discovery", data: null },
    ],
  };
}

/* ---------------- accounts ---------------- */
function capsFor(id: string): CapRow {
  const a = need(accounts.find((x) => x.id === id));
  const free = a.cap_24h - a.used_24h;
  const paused = !!a.paused_until && Date.parse(a.paused_until) > Date.now();
  return {
    account_id: a.id, used_24h: a.used_24h, cap_24h: a.cap_24h, free, spike_reserve_remaining: a.spike_reserve_remaining,
    hourly_allowed: a.enabled && !paused && a.lanes.includes("hourly") && free > a.spike_reserve_remaining,
    spike_allowed: a.enabled && !paused && a.lanes.includes("spike") && free > 0,
    gap_ok: !a.last_post_at || Date.now() - Date.parse(a.last_post_at) >= a.min_gap_s * 1000,
    last_post_at: a.last_post_at, paused_until: a.paused_until,
  };
}
function accountDetail(id: string): AccountDetail {
  const a = need(accounts.find((x) => x.id === id));
  const base = ({ tiktok_main: 2184, instagram_main: 906, youtube_main: 412, tiktok_live: 0 } as Record<string, number>)[a.id] ?? 100;
  const days = 14;
  return {
    ...a, caps: capsFor(id),
    stats: Array.from({ length: days * 2 }, (_, k) => {
      const t = days * 2 - 1 - k;
      const f = Math.round(base * Math.pow(0.93, t / 2));
      return { captured_at: ago(t * 43200), followers: a.enabled ? f : 0, views_total: a.enabled ? f * 120 : 0 };
    }),
    views_by_day: Array.from({ length: 7 }, (_, k) => ({ day: ago((6 - k) * 86400).slice(0, 10), views: a.enabled ? Math.round(base * (4 + k * 2.4)) : 0 })),
    posts: posts.filter((p) => p.account_id === a.id).slice(0, 20),
    events: a.id === "youtube_main" ? [
      { id: 8801, created_at: ago(1180), level: "warn", type: "account.rate_limited", service: "worker-express", entity_type: "account", entity_id: a.id, message: "429 youtube_channel_daily_limit · paused 1 h · cap 30 → 28", data: { status: 429, cap_before: 30, cap_after: 28 } },
      { id: 8800, created_at: ago(1800), level: "info", type: "post.retry_scheduled", service: "worker-express", entity_type: "post", entity_id: a.id, message: "attempt 2 in 2 min", data: null },
      { id: 8700, created_at: ago(86400 * 2), level: "warn", type: "setting.changed", service: "api", entity_type: "account", entity_id: a.id, message: "migration 0005 · cap_24h 40 → 28 · reserve 8 → 4", data: null },
    ] : events.filter((e) => e.entity_type === "account").slice(0, 3).map((e) => ({ ...e, entity_id: a.id })),
  };
}

/* ---------------- guardian & takedowns ---------------- */
const byCreator = (h: string) => cref(creators.find((c) => c.handle === h)!);
const ids = (...ps: (Post | undefined)[]) => ps.filter(Boolean).map((p) => p!.id);
const takedowns: Takedown[] = [
  {
    id: uid("takedown", 0), received_at: ago(2460), source: "email", sender: "legal@agency-rights.example", subject: "Removal request: unauthorised use of content",
    body_excerpt: "To whom it may concern, we represent a content creator whose streams appear on your channels without a licence. Please remove the material listed in the attached schedule within 48 hours.",
    is_takedown: true, creator: null, matched_post_ids: [], actions: [], status: "open", message_id: "18c2f0a1",
  },
  {
    id: uid("takedown", 1), received_at: ago(86400 * 3 + 3000), source: "email", sender: "support@stuntking-media.example", subject: "Please remove my clips",
    body_excerpt: "Hi, I noticed your channel is posting clips of my streams (StuntKing). I never gave permission. Please remove all of them from TikTok and YouTube, specifically youtube.com/shorts/Xy… Thanks, Dan",
    is_takedown: true, creator: byCreator("stuntking_tv"), matched_post_ids: ids(posts[7], posts[8]), actions: [
      { action: "block_creator", outcome: "done", detail: "stuntking_tv → blocked · claims_count 1", at: ago(86400 * 3 + 2940) },
      { action: "cancel_pending", outcome: "done", detail: "2 posts → skipped · 1 clip → removed", at: ago(86400 * 3 + 2940) },
      { action: "remove_post", platform: "youtube", outcome: "removed", detail: "Upload-Post unpublish", at: ago(86400 * 3 + 2880) },
      { action: "remove_post", platform: "tiktok", outcome: "manual", detail: "remove_failed · in the Delete by hand list", at: ago(86400 * 3 + 2880) },
      { action: "strike", outcome: "counted", detail: "1 of 2 in 7 days · no emergency stop", at: ago(86400 * 3 + 2880) },
      { action: "notify", outcome: "sent", detail: "critical · “Takedown actioned: stuntking_tv”", at: ago(86400 * 3 + 2880) },
    ], status: "partial", message_id: "18bfe220",
  },
  {
    id: uid("takedown", 2), received_at: ago(86400 * 4), source: "email", sender: "mike@mayhem.example", subject: "Take this down",
    body_excerpt: "Stop posting my stuff. Take every clip down now.", is_takedown: true, creator: byCreator("mayhem_mike"), matched_post_ids: ids(posts[9]),
    actions: [{ action: "block_creator", outcome: "done" }, { action: "remove_post", platform: "youtube", outcome: "removed" }, { action: "strike", outcome: "counted" }],
    status: "actioned", message_id: "18bf0010",
  },
  ...Array.from({ length: 14 }, (_, k): Takedown => ({
    id: uid("takedown", 10 + k), received_at: ago(Math.round(86400 * (1 + k * 0.6))), source: "email",
    sender: ["newsletter@creatortips.example", "noreply@platform.example", "hello@brand-deals.example"][k % 3],
    subject: ["Your weekly creator tips", "Your account summary", "Partnership opportunity"][k % 3], body_excerpt: "Not a takedown — marketing or account email.",
    is_takedown: false, creator: null, matched_post_ids: [], actions: [], status: "ignored", message_id: `18a${k}`,
  })),
];

function guardian(): Guardian {
  const manual = posts.filter((p) => p.status === "remove_failed");
  const extra: Post[] = manual.length < 2 && posts[1]
    ? [{ ...posts[1], id: uid("post", 901), account_id: "instagram_main", platform: "instagram", status: "remove_failed", claim_status: "blocked", clip_series_no: 88, clip_hook: "NPC road rage" }]
    : [];
  return {
    strikes: { count: 1, limit: 2, window_d: 7, items: [{ at: ago(86400 * 3 + 3000), creator: byCreator("stuntking_tv"), takedown_id: uid("takedown", 1) }] },
    emergency_stop: controlState.stopped, stop_reason: controlState.reason,
    manual_removals: [...manual, ...extra],
    claims: { last_run_at: ago(360), ok: 77, suspect: 2, unverifiable: 1, removed_by_platform: 0 },
    takedown_counts: { open: 1, actioned: 1, partial: 1, ignored: 14 },
  };
}

export const routes: MockRoute[] = [
  { method: "GET", pattern: /^admin\/discovery$/, handle: () => discovery() },
  {
    method: "GET", pattern: /^admin\/candidates$/, handle: (_m, q) => {
      const picked = q.get("picked");
      return list(candidates, q, "source_type", picked ? (c) => String(c.picked) === picked : undefined);
    },
  },
  {
    method: "GET", pattern: /^admin\/creators$/, handle: (_m, q) => {
      const term = (q.get("q") ?? "").toLowerCase();
      const plat = q.get("platform");
      return list(creators, q, "tier", (c) => (!term || (c.handle ?? "").includes(term)) && (!plat || c.platform === plat));
    },
  },
  { method: "GET", pattern: /^admin\/creators\/([^/]+)$/, handle: (m) => creatorDetail(m[1]) },
  { method: "GET", pattern: /^admin\/accounts$/, handle: () => ({ items: accounts, next_cursor: null }) },
  { method: "GET", pattern: /^admin\/accounts\/([^/]+)$/, handle: (m) => accountDetail(m[1]) },
  { method: "GET", pattern: /^admin\/guardian$/, handle: () => guardian() },
  { method: "GET", pattern: /^admin\/takedowns$/, handle: (_m, q) => list(takedowns, q, "status") },
  {
    method: "GET", pattern: /^admin\/takedowns\/([^/]+)$/, handle: (m): TakedownDetail => {
      const t = need(takedowns.find((x) => x.id === m[1]));
      const own = events.filter((e) => e.entity_type === "takedown" && e.entity_id === t.id);
      const synth = t.status === "ignored" ? [] : [{
        id: 6000 + takedowns.indexOf(t), created_at: t.received_at, level: (t.status === "open" ? "critical" : "warn") as "critical" | "warn",
        type: t.status === "open" ? "takedown.unmatched" : "takedown.actioned", service: "api", entity_type: "takedown", entity_id: t.id,
        message: t.status === "open" ? "takedown email needs you: nothing of ours matched" : `takedown actioned: ${t.creator?.handle ?? "unknown"}`, data: null,
      }];
      return { ...t, matched_posts: posts.filter((p) => t.matched_post_ids?.includes(p.id)), events: own.length ? own : synth };
    },
  },
];
