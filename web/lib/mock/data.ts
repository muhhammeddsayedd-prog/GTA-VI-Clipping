/* Deterministic sample world for local development (never used on Vercel). Mirrors docs/26-admin-console.md. */
import type {
  AccountSummary, Clip, CreatorRef, Creator, Event, Level, Post, PostPlatform, Spike, Trace,
} from "@/lib/api/types";

export function rng(seed: number) {
  let s = seed >>> 0 || 1;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}
export const rand = rng(42);
export function pick<T>(arr: T[], r = rand): T { return arr[Math.floor(r() * arr.length)]; }
export function uid(kind: string, n: number): string {
  const h = (x: number) => (x >>> 0).toString(16).padStart(8, "0");
  let a = 2166136261;
  for (const c of `${kind}:${n}`) a = Math.imul(a ^ c.charCodeAt(0), 16777619);
  const b = Math.imul(a, 2654435761), c = Math.imul(b ^ n, 40503), d = Math.imul(c, 97);
  return `${h(a)}-${h(b).slice(0, 4)}-4${h(c).slice(1, 4)}-a${h(d).slice(1, 4)}-${h(a ^ d)}${h(b ^ c).slice(0, 4)}`;
}
export const NOW = () => Date.now();
export function ago(sec: number): string { return new Date(NOW() - sec * 1000).toISOString(); }
export function ahead(sec: number): string { return new Date(NOW() + sec * 1000).toISOString(); }

/* ---------------- creators ---------------- */
const C: [string, "twitch" | "youtube", Creator["tier"], string, number, number, number, number][] = [
  ["vicecityvibes", "twitch", "preferred", "“Clip my streams! Tag me” — channel About", 412000, 18200, 41, 0],
  ["leonida_rp", "twitch", "auto", "No statement found in description", 96000, 9400, 12, 0],
  ["sunsetheists", "twitch", "auto", "No statement found", 41000, 6100, 8, 0],
  ["nightdrive_tv", "twitch", "auto", "No statement found", 22000, 3000, 6, 0],
  ["portgellhorn", "youtube", "preferred", "“Clippers welcome, credit the channel”", 1200000, 21700, 4, 0],
  ["lucia_mains", "youtube", "restricted", "“Do not reupload my content” — P6 evidence", 88000, 0, 0, 0],
  ["stuntking_tv", "youtube", "blocked", "Blocked by guardian · claim on post #0119", 640000, 4400, 3, 1],
  ["mayhem_mike", "twitch", "blocked", "Blocked by guardian · takedown email", 51000, 7700, 5, 1],
  ["copchase_live", "twitch", "auto", "No statement found", 33000, 5200, 9, 0],
  ["keysrunner", "twitch", "auto", "No statement found", 14000, 2100, 2, 0],
];
export const creators: Creator[] = C.map(([handle, platform, tier, reason, fol, avg, clips, claims], i) => ({
  id: uid("creator", i), platform, channel_id: `${platform === "twitch" ? "" : "UC"}${100200 + i * 7}`, handle,
  display_name: handle, language: "en", tier, tier_reason: reason, follower_count: fol, avg_clip_views: avg || null,
  claims_count: claims, description_checked_at: ago(86400 * (1 + (i % 5))), discovered_at: ago(86400 * (6 + i)), clips_posted: clips,
}));
export function cref(c: Creator): CreatorRef {
  return { id: c.id, handle: c.handle ?? "", display_name: c.display_name, platform: c.platform, tier: c.tier };
}

/* ---------------- accounts ---------------- */
export const accounts: AccountSummary[] = [
  { id: "tiktok_main", platform: "tiktok", handle: "@6starsclipping", lanes: ["hourly"], enabled: true, paused_until: null, pause_reason: null, cap_24h: 14, spike_reserve: 0, used_24h: 12, spike_reserve_remaining: 0, last_post_at: ago(540), privacy: "PUBLIC_TO_EVERYONE", hourly_hours_utc: [0, 1, 2, 11, 12, 14, 15, 17, 18, 19, 20, 21, 22, 23], min_gap_s: 900 },
  { id: "instagram_main", platform: "instagram", handle: "@6starsclippy", lanes: ["hourly", "spike"], enabled: true, paused_until: null, pause_reason: null, cap_24h: 45, spike_reserve: 10, used_24h: 23, spike_reserve_remaining: 7, last_post_at: ago(540), privacy: null, hourly_hours_utc: null, min_gap_s: 900 },
  { id: "youtube_main", platform: "youtube", handle: "@6starsclips-m3x", lanes: ["hourly", "spike"], enabled: true, paused_until: ahead(2400), pause_reason: "429 youtube_channel_daily_limit", cap_24h: 28, spike_reserve: 4, used_24h: 26, spike_reserve_remaining: 2, last_post_at: ago(2280), privacy: "public", hourly_hours_utc: null, min_gap_s: 900 },
  { id: "tiktok_live", platform: "tiktok", handle: "@6starsclipping", lanes: ["spike"], enabled: false, paused_until: null, pause_reason: null, cap_24h: 14, spike_reserve: 14, used_24h: 0, spike_reserve_remaining: 14, last_post_at: null, privacy: "PUBLIC_TO_EVERYONE", hourly_hours_utc: null, min_gap_s: 900 },
];

/* ---------------- spikes / clips / posts ---------------- */
export const HOOKS = ["He took the tow truck off the bridge", "Cop chase ends in the canal", "Heist van, zero brakes", "Lucia’s first wanted level",
  "6 stars in 40 seconds", "The gator won", "Wrong car, wrong guy", "Boat jump over the Keys", "Chat called it", "NPC road rage",
  "Stole the cop car, got arrested in it", "Skydive into a pool", "Helicopter clips the crane", "Vault opens on the first try"];
const MOMENTS = ["chase_fail", "funny_npc", "heist_win", "close_call", "stunt", "noise · giveaway spam", "rage_moment"];
const REJECTS = ["score_too_low", "music_over_limit", "duplicate_topic", "spoiler_blocked", "too_short", "black_frames"];
const CLIP_STATUS = ["posted", "posted", "approved", "posted", "rejected", "approved", "posted", "gating", "rendering", "expired", "approved", "posted", "rejected"];

export const spikes: Spike[] = [];
export const clips: Clip[] = [];
export const posts: Post[] = [];
const r2 = rng(7);
for (let i = 0; i < 90; i++) {
  const cr = creators[i % 9 === 8 ? 8 : i % 5 === 4 ? 4 : i % 4];
  const live = i % 3 === 0;
  const tierRoll = r2();
  const tier = tierRoll > 0.88 ? "S" : tierRoll > 0.62 ? "A" : tierRoll > 0.35 ? "B" : "rejected";
  const status = tier === "rejected" ? (i % 4 === 0 ? "merged" : "rejected") : i < 60 ? "clipped" : i % 3 ? "confirmed" : "rendering";
  const z = tier === "S" ? 3.5 + r2() : tier === "A" ? 2.6 + r2() * 0.8 : 2 + r2() * 0.6;
  const det = 60 * (8 + i * 21);
  spikes.push({
    id: uid("spike", i), mode: live ? "live" : i % 11 === 5 ? "transcript" : "replay", creator: cref(cr),
    live_session_id: live ? uid("live", i % 6) : null, video_id: live ? null : uid("video", i % 14),
    detected_at: ago(det), spike_wall: live ? ago(det) : null, spike_offset_s: live ? null : Math.round(600 + r2() * 12000),
    score_z: Math.round(z * 10) / 10,
    parts: { msgs_z: +(z * 0.8).toFixed(1), uniq_z: +(z * 0.9).toFixed(1), react_z: +(z * 1.1).toFixed(1), clipreq_z: +(z * 0.7).toFixed(1), audio_z: +(z * 0.5).toFixed(1) },
    tier, claude_confidence: Math.round((0.35 + r2() * 0.6) * 100) / 100,
    claude_reason: `${pick(MOMENTS, r2)} · ${HOOKS[i % HOOKS.length].toLowerCase()}`, status,
    merged_into: status === "merged" ? uid("spike", i - 1) : null, clip_id: status === "clipped" ? uid("clip", i) : null,
    clip_series_no: status === "clipped" ? 145 - i : null, spike_to_post_s: live && tier === "S" ? 70 + Math.round(r2() * 120) : null,
    window_start_s: live ? null : 1200 + i * 30, window_end_s: live ? null : 1238 + i * 30,
    window_start_wall: live ? ago(det + 20) : null, window_end_wall: live ? ago(det - 18) : null,
  });
  if (status === "clipped") {
    const st = CLIP_STATUS[i % CLIP_STATUS.length];
    const lane = live && tier === "S" ? "spike" : "hourly";
    const score = st === "rejected" ? 5.8 + r2() : 7 + r2() * 2;
    clips.push({
      id: uid("clip", i), series_no: 145 - i, spike_id: uid("spike", i), creator: cref(cr), source_platform: cr.platform,
      source_type: live ? "live" : "vod", lane, duration_s: Math.round(20 + r2() * 35), hook: HOOKS[i % HOOKS.length].toUpperCase(),
      title: `${HOOKS[i % HOOKS.length]} 😭`, topic: HOOKS[i % HOOKS.length].toLowerCase().replace(/[^a-z]+/g, "_").replace(/_$/, ""),
      score: Math.round(score * 10) / 10, rank_score: st === "approved" ? Math.round((score - 0.5 - i * 0.03) * 100) / 100 : null,
      music_ratio: st === "rejected" && i % 2 ? 0.18 : 0, status: st, reject_reason: st === "rejected" ? REJECTS[i % REJECTS.length] : null,
      gate_profile: lane === "spike" ? "express" : "hourly", created_at: ago(det - 120), approved_at: ["approved", "posted", "expired"].includes(st) ? ago(det - 200) : null,
      posted_at: st === "posted" ? ago(det - 400) : null, has_file: st !== "rejected" && st !== "expired",
    });
    if (st === "posted") {
      const accs = lane === "spike" ? ["youtube_main", "instagram_main"] : [pick(["tiktok_main", "instagram_main", "youtube_main"], r2)];
      accs.forEach((acc, k) => {
        const a = accounts.find((x) => x.id === acc)!;
        const n = posts.length;
        const pst = n === 3 ? "pending" : n === 5 ? "failed" : n === 7 ? "removed" : n === 8 ? "remove_failed" : "published";
        posts.push({
          id: uid("post", n), clip_id: uid("clip", i), clip_series_no: 145 - i, clip_hook: HOOKS[i % HOOKS.length], account_id: acc,
          platform: a.platform as PostPlatform, lane, slot_at: lane === "hourly" ? ago(Math.floor((det - 400) / 3600) * 3600) : null,
          status: pst, dry_run: false, post_url: pst === "published" || pst === "removed" || pst === "remove_failed" ? `https://${a.platform}.com/${a.handle?.slice(1)}/${uid("p", n).slice(0, 11)}` : null,
          platform_post_id: pst === "published" ? uid("pp", n).slice(0, 11) : null, published_at: pst === "pending" || pst === "failed" ? null : ago(det - 420 - k * 7),
          views: pst === "published" ? Math.round(800 + r2() * 30000) : null, likes: pst === "published" ? Math.round(50 + r2() * 2500) : null,
          comments: pst === "published" ? Math.round(r2() * 80) : null, shares: pst === "published" ? Math.round(r2() * 40) : null,
          claim_status: pst === "removed" ? "claimed" : pst === "remove_failed" ? "blocked" : pst === "published" ? "ok" : null,
          error: pst === "failed" ? "youtube_channel_daily_limit" : null, attempts: pst === "failed" ? 3 : 1, created_at: ago(det - 410),
        });
      });
    }
  }
}

/* ---------------- events ---------------- */
const EV: [Level, string, string, string, string, string][] = [
  ["info", "post.published", "worker-express", "post", "youtube_main", "posted #0145 on youtube_main"],
  ["info", "clip.approved", "worker-express", "clip", "", "#0145 approved · express profile"],
  ["info", "discovery.run", "api", "", "", "312 live seen · 0 assigned · 2 videos queued"],
  ["info", "spike.detected", "live-manager", "spike", "", "S · leonida_rp · z 4.2"],
  ["critical", "takedown.unmatched", "api", "takedown", "", "legal@agency… nothing of ours matched"],
  ["warn", "health.incident", "scheduler", "incident", "disk", "disk is filling up · 80.4%"],
  ["warn", "account.rate_limited", "worker-express", "account", "youtube_main", "youtube_main paused 1 h"],
  ["info", "posting.slot_skipped", "worker-express", "post", "", "R5 instagram_main · spike posted 12 min earlier"],
  ["error", "video.failed", "worker-normal", "video", "", "yt-dlp failed 3 times"],
  ["info", "setting.changed", "api", "setting", "gate.hourly", "gate.hourly changed through the control API"],
  ["warn", "health.recovered", "scheduler", "incident", "queue_low", "clip queue is low → recovered"],
  ["info", "live.express_done", "worker-express", "spike", "", "express done · posted to 2 accounts"],
];
export const events: Event[] = Array.from({ length: 160 }, (_, i) => {
  const [level, type, service, et, eid, msg] = EV[i % EV.length];
  return {
    id: 8412 - i, created_at: ago(40 + i * 173), level, type, service, entity_type: et || null,
    entity_id: eid || (et === "clip" ? uid("clip", i % 40) : et === "spike" ? uid("spike", i % 40) : et === "post" ? uid("post", i % 20) : et === "video" ? uid("video", i % 14) : et === "takedown" ? uid("takedown", 0) : null),
    message: msg, data: type === "account.rate_limited"
      ? { account_id: "youtube_main", status: 429, error_code: "youtube_channel_daily_limit", paused_until: ahead(2400), cap_before: 30, cap_after: 28 }
      : type === "setting.changed" ? { key: "gate.hourly", old: { min_score: 7 }, new: { min_score: 7.5 }, changed: true }
      : type === "discovery.run" ? { live_seen: 312, assigned: i % 3 === 0 ? 1 : 0, released: 0, videos_queued: 2, new_creators: 1, duration_s: 21, source: i % 5 === 4 ? "failover" : "n8n" }
      : type === "posting.slot_skipped" ? { rule: "R5", account_id: "instagram_main", slot_at: ago(3600 * (1 + (i % 6))), why: "spike posted 12 min earlier" }
      : { note: "sample" },
  };
});
export function eventsFor(entityType: string, id: string): Event[] {
  const own = events.filter((e) => e.entity_type === entityType && e.entity_id === id);
  return (own.length ? own : events.filter((e) => e.entity_type === entityType).slice(0, 4)).slice(0, 50);
}

export function traceFor(clip: Clip | undefined, spike?: Spike): Trace {
  const sp = spike ?? (clip ? spikes.find((s) => s.id === clip.spike_id) : undefined);
  const cr = clip?.creator ?? sp?.creator;
  const ps = clip ? posts.filter((p) => p.clip_id === clip.id) : [];
  return {
    creator: cr ? { id: cr.id, label: `${cr.handle} · ${cr.tier}`, status: cr.tier } : null,
    live_session: sp?.live_session_id ? { id: sp.live_session_id, label: "live session", status: "recording" } : null,
    video: sp?.video_id ? { id: sp.video_id, label: "VOD", status: "done" } : null,
    spike: sp ? { id: sp.id, label: `${sp.tier} · z ${sp.score_z}`, status: sp.status } : null,
    clip: clip ? { id: clip.id, label: `#${String(clip.series_no).padStart(4, "0")} · ${clip.status}`, status: clip.status } : null,
    posts: ps.map((p) => ({ id: p.id, label: `${p.account_id} · ${p.status}`, status: p.status })),
  };
}

/* ---------------- paging helper ---------------- */
export function paginate<T>(all: T[], q: URLSearchParams, statusKey?: (t: T) => string) {
  const limit = Math.min(Number(q.get("limit") ?? 50) || 50, 200);
  const start = Number(q.get("cursor") ?? 0) || 0;
  const items = all.slice(start, start + limit);
  const next_cursor = start + limit < all.length ? String(start + limit) : null;
  return { items, next_cursor, statusKey };
}
export function countsBy<T>(all: T[], key: (t: T) => string | null | undefined): Record<string, number> {
  const out: Record<string, number> = { all: all.length };
  for (const t of all) {
    const k = key(t);
    if (k) out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}
/** Standard list answer: filter by `status` param (or another field), counts ignore that filter. */
export function list<T>(all: T[], q: URLSearchParams, field: keyof T & string, extra?: (t: T) => boolean) {
  const base = extra ? all.filter(extra) : all;
  const want = q.get(field);
  const filtered = want ? base.filter((t) => String(t[field]) === want) : base;
  const { items, next_cursor } = paginate(filtered, q);
  return { items, next_cursor, counts: countsBy(base, (t) => String(t[field])) };
}
