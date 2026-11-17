import type { ClipDetail, Event, GateCheck, GateResult, PostDetail, QueueItem, QueueView } from "@/lib/api/types";
import { accounts, ago, clips, events, eventsFor, list, posts, traceFor, uid } from "./data";
import { need, type MockRoute } from "./util";

/* Sample data for the Clips, Queue and Posts pages. */

type ClipRow = (typeof clips)[number];

function gateFor(c: ClipRow): GateResult {
  const express = c.gate_profile === "express";
  const rejected = c.status === "rejected";
  const reason = c.reject_reason;
  const music = c.music_ratio ?? 0;
  const ck = (ok: boolean, value: GateCheck["value"], limit: GateCheck["limit"], code?: string): GateCheck =>
    ({ ok, value, limit, ...(code && !ok ? { codes: [code] } : {}) });
  const checks: Record<string, GateCheck> = {
    creator_tier: ck(true, c.creator.tier, "not restricted/blocked"),
    length: ck(!(rejected && reason === "too_short"), rejected && reason === "too_short" ? 14 : c.duration_s, express ? "15–45 s" : "20–60 s", "too_short"),
    music_ratio: ck(!(rejected && reason === "music_over_limit"), rejected && reason === "music_over_limit" ? Math.max(music, 0.18) : music, express ? "= 0" : "≤ 0.10", "music_over_limit"),
    spoiler: ck(!(rejected && reason === "spoiler_blocked"), rejected && reason === "spoiler_blocked" ? "yes" : "no", "blocked until 3 Dec", "spoiler_blocked"),
    safety: ck(true, "clean", "all false"),
    watermark_logos: ck(true, "none", "none"),
    visual: ck(!(rejected && reason === "black_frames"), rejected && reason === "black_frames" ? "black 2.4 s" : "pass", "< 2 s", "black_frames"),
    subtitle_drift: ck(true, 0.08, "≤ 0.3 s"),
    score: ck((c.score ?? 0) >= (express ? 8 : 7), c.score, express ? "≥ 8" : "≥ 7", "score_too_low"),
    duplicate_topic: ck(!(rejected && reason === "duplicate_topic"), rejected && reason === "duplicate_topic" ? "#0121" : "none", express ? "6 h window" : "48 h window", "duplicate_topic"),
    source_range: ck(true, "none", "± 30 s"),
    file: ck(true, "1080×1920 · audio", "≤ 100 MB"),
  };
  const codes = Object.values(checks).flatMap((x) => x.codes ?? []);
  return {
    profile: c.gate_profile ?? "hourly", passed: !rejected, verdict: rejected ? "reject" : "approve", lane: c.lane, codes,
    reasons: rejected ? [`${reason}`] : [], checks, precheck: "unavailable", risk: [], demoted: false,
    ...(express ? { express: { passed: !rejected, codes: [] } } : {}), decided_at: c.approved_at ?? c.created_at,
  };
}

function detail(id: string): ClipDetail {
  const c = need(clips.find((x) => x.id === id));
  const words = "bro there's no way there's no way I make this okay okay we're taking the tow truck six stars chat six stars here we go off the bridge"
    .split(" ").map((w, i) => ({ w, s: +(i * 0.95).toFixed(2), e: +(i * 0.95 + 0.8).toFixed(2) }));
  return {
    ...c,
    caption: "chat called it before he did #GTA6",
    hashtags: ["gta6", "gtavi", "leonida", "gaming", "fyp", "shorts"],
    commentary: "Streamer bails out at the last second and chat erupts.",
    transcript: { words, text: words.map((x) => x.w).join(" ") },
    keywords: [{ line: 0, words: ["no", "way"] }, { line: 1, words: ["tow", "truck"] }, { line: 3, words: ["off", "the", "bridge"] }],
    safety_flags: { politics: false, gambling: false, sponsored: false, real_violence: false, sexual: false, personal_info: false, hate: false, copyrighted_media: false },
    score_parts: c.score ? { hook: +(c.score + 0.3).toFixed(1), clarity: +(c.score - 0.3).toFixed(1), payoff: +(c.score + 0.5).toFixed(1), rewatch: +(c.score - 0.6).toFixed(1) } : null,
    spoiler: c.reject_reason === "spoiler_blocked",
    profanity_count: 1,
    gate_result: c.status === "rendering" ? null : gateFor(c),
    precheck_result: null,
    render_ms: 64000 + (c.series_no ?? 0) * 37,
    file_size_bytes: c.has_file ? 14_200_000 : null,
    storage_path: c.has_file ? `clips/dev/2026/11/17/${c.id}.mp4` : null,
    start_s: 1200, end_s: 1200 + (c.duration_s ?? 30), original_language: "en",
    posts: posts.filter((p) => p.clip_id === c.id),
    events: eventsFor("clip", c.id),
    trace: traceFor(c),
  };
}

function queueItem(c: ClipRow): QueueItem {
  return {
    clip_id: c.id, series_no: c.series_no, hook: c.hook, title: c.title, creator: c.creator, lane: c.lane, score: c.score,
    rank_score: c.rank_score, duration_s: c.duration_s, created_at: c.created_at, age_s: Math.round((Date.now() - Date.parse(c.created_at)) / 1000),
    expires_at: new Date(Date.parse(c.created_at) + 72 * 3600e3).toISOString(),
  };
}

type SlotState = QueueView["slots"][number]["hours"][number]["state"];

function queue(): QueueView {
  const approved = clips.filter((c) => c.status === "approved").sort((a, b) => (b.rank_score ?? 0) - (a.rank_score ?? 0));
  const now = new Date().getUTCHours();
  const slots = accounts.map((a, ai) => ({
    account_id: a.id, platform: a.platform,
    hours: Array.from({ length: 24 }, (_, h) => {
      const allowed = a.enabled && a.lanes.includes("hourly") && (!a.hourly_hours_utc || a.hourly_hours_utc.includes(h));
      let state: SlotState = !allowed ? "not_allowed" : h >= now ? "planned" : "posted";
      if (allowed && h < now) {
        if (ai === 1 && h === 6) state = "skipped";
        if (ai === 2 && h === 3) state = "failed";
        if (ai === 1 && h === now - 1) state = "skipped";
        if (ai === 0 && h === 9) state = "empty";
      }
      return { hour: h, state, post_id: state === "posted" && posts.length ? uid("post", (ai * 24 + h) % posts.length) : null };
    }),
  }));
  const caps = accounts.filter((a) => a.enabled).map((a) => {
    const free = a.cap_24h - a.used_24h;
    return {
      account_id: a.id, used_24h: a.used_24h, cap_24h: a.cap_24h, free, spike_reserve_remaining: a.spike_reserve_remaining,
      hourly_allowed: a.lanes.includes("hourly") && free > a.spike_reserve_remaining, spike_allowed: a.lanes.includes("spike") && free > 0,
      gap_ok: true, last_post_at: a.last_post_at, paused_until: a.paused_until,
    };
  });
  const dec = (id: number, s: number, level: Event["level"], type: string, acc: string, msg: string, rule?: string): Event => ({
    id, created_at: ago(s), level, type, service: "worker-express", entity_type: "account", entity_id: acc, message: msg,
    data: { account_id: acc, slot_at: ago(s), ...(rule ? { rule } : {}) },
  });
  const next = new Date();
  next.setUTCMinutes(0, 0, 0);
  next.setUTCHours(next.getUTCHours() + 1);
  return {
    ranked: { hourly: approved.filter((c) => c.lane === "hourly").map(queueItem), spike: approved.filter((c) => c.lane === "spike").map(queueItem) },
    slots, caps, next_slot_at: next.toISOString(),
    decisions: [
      dec(9001, 1900, "info", "posting.slot_skipped", "instagram_main", "Spike posted 12 min earlier (skip_hourly_after_spike_s 1800)", "R5"),
      dec(9002, 5400, "warn", "post.failed", "youtube_main", "youtube_channel_daily_limit · retry 13:02 → published"),
      dec(9005, 19800, "warn", "posting.slot_empty", "tiktok_main", "Nothing approved was eligible for this slot"),
      dec(9003, 16200, "info", "posting.slot_skipped", "tiktok_main", "Hour not in hourly_hours_utc", "R3"),
      dec(9004, 30600, "info", "posting.slot_skipped", "instagram_main", "Cap room needed for spike reserve (7/10 left)", "R2"),
    ],
  };
}

function postDetail(id: string): PostDetail {
  const p = need(posts.find((x) => x.id === id));
  const c = clips.find((x) => x.id === p.clip_id);
  const created = Date.parse(p.created_at);
  const at = (s: number) => new Date(created + s * 1000).toISOString();
  const ev = (n: number, iso: string, level: Event["level"], type: string, message: string, data: Record<string, unknown> | null = null): Event =>
    ({ id: n, created_at: iso, level, type, service: "worker-express", entity_type: "post", entity_id: p.id, message, data });
  const evs: Event[] = [
    ev(1, at(0), "info", "post.planned", "Upload requested", { upload_request_id: `up_${p.id.slice(0, 8)}` }),
    ev(2, at(11), "info", "post.status_pending", "Status polled · processing"),
  ];
  if (p.status === "failed") evs.push(ev(3, at(40), "warn", "post.failed", `Upload failed: ${p.error}`, { attempts: p.attempts }));
  if (p.published_at) evs.push(ev(4, p.published_at, "info", "post.published", `Published · platform_post_id ${p.platform_post_id ?? "pending"}`));
  if (p.status === "removed") evs.push(ev(5, at(7200), "warn", "post.removed", "Removed after a claim"));
  if (p.status === "remove_failed") evs.push(ev(5, at(7200), "critical", "post.remove_failed", "Platform has no delete API — delete by hand"));
  return {
    ...p,
    upload_request_id: `up_${p.id.slice(0, 8)}`,
    idempotency_key: `post:${p.clip_id}:${p.account_id}`,
    stats_updated_at: p.status === "published" ? ago(780) : null,
    claim_checked_at: p.published_at ? ago(900) : null,
    removed_at: p.status === "removed" ? at(7200) : null,
    remove_reason: p.status === "removed" || p.status === "remove_failed" ? "creator claim" : null,
    events: [...evs.reverse(), ...events.filter((e) => e.entity_type === "post" && e.entity_id === p.id)],
    trace: traceFor(c),
  };
}

export const routes: MockRoute[] = [
  {
    method: "GET", pattern: /^admin\/clips$/, handle: (_m, q) => {
      const lane = q.get("lane"), creator = q.get("creator_id"), term = (q.get("q") ?? "").trim().toLowerCase().replace(/^#/, "");
      return list(clips, q, "status", (c) =>
        (!lane || c.lane === lane) && (!creator || c.creator.id === creator) &&
        (!term || String(c.series_no) === term || (c.hook ?? "").toLowerCase().includes(term) || (c.topic ?? "").includes(term)));
    },
  },
  {
    method: "GET", pattern: /^admin\/clips\/([^/]+)\/media$/, handle: (m) => {
      const c = need(clips.find((x) => x.id === m[1]));
      // Mock mode never serves real files: "deleted" for rejected/expired clips, otherwise "no_file".
      return { url: null, expires_at: null, reason: c.status === "rejected" || c.status === "expired" ? "deleted" : "no_file" };
    },
  },
  { method: "GET", pattern: /^admin\/clips\/([^/]+)$/, handle: (m) => detail(m[1]) },
  { method: "GET", pattern: /^admin\/queue$/, handle: () => queue() },
  {
    method: "GET", pattern: /^admin\/posts$/, handle: (_m, q) => {
      const acc = q.get("account_id"), lane = q.get("lane"), clip = q.get("clip_id");
      return list(posts, q, "status", (p) => (!acc || p.account_id === acc) && (!lane || p.lane === lane) && (!clip || p.clip_id === clip));
    },
  },
  { method: "GET", pattern: /^admin\/posts\/([^/]+)$/, handle: (m) => postDetail(m[1]) },
];
