/* Sample data for pipeline, live sessions, videos and spikes. Shapes: docs/26-admin-console.md. */
import type { Job, LiveSession, Pipeline, Spike, Video } from "@/lib/api/types";
import { ago, ahead, clips, creators, cref, events, eventsFor, list, posts, rng, spikes, traceFor, uid } from "./data";
import { need, type MockRoute } from "./util";

const r = rng(99);

/* ---------------- live sessions ---------------- */
const LIVE: [number, string, number, number, number | null][] = [
  // creator index, status, trend, started (s ago), below-cutoff since (s ago)
  [0, "recording", 82, 11520, null],
  [1, "recording", 77, 3900, null],
  [3, "recording", 61, 2880, 360],
  [9, "assigned", 58, 0, null],
  [2, "replay_done", 71, 19400, null],
  [8, "replay_queued", 68, 24800, null],
  [5, "failed", 52, 23500, null],
  [7, "ended", 61, 27300, null],
];
const sessions: LiveSession[] = LIVE.map(([ci, status, trend, started, below], i) => {
  const c = creators[ci];
  const sp = spikes.filter((s) => s.live_session_id === uid("live", i));
  const active = status === "recording" || status === "assigned";
  return {
    id: uid("live", i), creator: cref(c), platform: c.platform, status, trend_score: trend,
    url: c.platform === "twitch" ? `https://twitch.tv/${c.handle}` : `https://youtube.com/@${c.handle}/live`, channel_login: c.handle,
    reconnects: status === "failed" ? 3 : i === 7 ? 1 : 0, assigned_at: ago(started + 120), started_at: status === "assigned" ? null : ago(started),
    ended_at: active ? null : ago(Math.max(600, started - 14000)), below_cutoff_since: below ? ago(below) : null,
    spikes: { S: sp.filter((s) => s.tier === "S").length, A: sp.filter((s) => s.tier === "A").length, B: sp.filter((s) => s.tier === "B").length },
    clips: sp.filter((s) => s.clip_id).length,
  };
});
const BOTS = ["Nightbot", "StreamElements", "Fossabot", "Moobot", "Sery_Bot"];

function liveDetail(id: string) {
  const s = need(sessions.find((x) => x.id === id));
  const idx = sessions.indexOf(s);
  // Spread this session's sample spikes across its own recording window.
  const t0 = Date.parse(s.started_at ?? s.assigned_at ?? new Date().toISOString());
  const t1 = s.ended_at ? Date.parse(s.ended_at) : Date.now();
  const raw = spikes.filter((x) => x.live_session_id === id);
  const sp = raw.map((x, k) => {
    const at = t0 + ((k + 0.5) / Math.max(raw.length, 1)) * (t1 - t0) * 0.96;
    const iso = new Date(at).toISOString();
    return { ...x, detected_at: iso, spike_wall: iso, window_start_wall: new Date(at - 20_000).toISOString(), window_end_wall: new Date(at + 18_000).toISOString() };
  }).reverse();
  const clipped = sp.filter((x) => x.clip_id && x.window_start_wall && x.window_end_wall).slice(0, 4)
    .map((x) => ({ start_wall: x.window_start_wall!, end_wall: x.window_end_wall!, clip_id: x.clip_id! }));
  const first = sp.find((x) => x.clip_id) ?? sp[0];
  const clip = first ? clips.find((c) => c.id === first.clip_id) : undefined;
  const trace = traceFor(clip, first);
  return {
    ...s, bot_names: BOTS.slice(0, 3 + (idx % 2)), clipped_ranges: clipped,
    vod_source_id: s.status === "recording" || s.status === "assigned" ? null : `v22918${47301 + idx}`,
    buffer_dir: s.status === "recording" ? `/data/live/${s.channel_login}/` : null,
    spikes: sp,
    events: events.filter((e) => e.type.startsWith("live.") || e.type.startsWith("spike.")).slice(0, 8),
    trace: {
      ...trace, creator: { id: s.creator.id, label: `${s.creator.handle} · ${s.creator.tier}`, status: s.creator.tier },
      live_session: { id: s.id, label: s.status, status: s.status }, video: null,
    },
  };
}

/* ---------------- videos ---------------- */
const V: [number, string, string, number, boolean, number, string | null, number][] = [
  // creator, title, status, duration_s, has_chat, trend, skip_reason, attempts
  [2, "Heist night w/ the crew — 6 stars", "processing", 14520, true, 71, null, 1],
  [8, "Cops only RP, day 1", "done", 19860, true, 68, null, 1],
  [4, "FIRST TIME IN LEONIDA (no commentary)", "new", 8280, false, 54, "download_blocked · youtube", 0],
  [5, "Lucia speedrun attempts", "skipped", 6240, true, 52, "platform_blocked", 0],
  [7, "Chaos mode is back", "done", 7800, true, 61, null, 1],
  [6, "Stunt jumps 1–50", "skipped", 10860, true, 57, "creator forbids clipping", 0],
  [9, "Boats in the Keys", "failed", 4320, true, 55, null, 3],
  [3, "Night drive, chat picks the route", "done", 13200, true, 63, null, 1],
  [0, "Bridge chaos marathon", "done", 21000, true, 79, null, 1],
  [1, "Leonida RP — first robbery", "new", 9900, true, 60, null, 0],
  [4, "Every wanted level explained", "new", 1980, false, 51, "download_blocked · youtube", 0],
  [2, "Vault speedrun", "done", 5400, true, 58, null, 2],
  [8, "Traffic stop gone wrong", "processing", 7200, true, 64, null, 1],
  [0, "Night heist practice", "done", 11000, true, 66, null, 1],
];
const videos: Video[] = V.map(([ci, title, status, dur, chat, trend, skip, att], i) => {
  const c = creators[ci];
  const sp = spikes.filter((s) => s.video_id === uid("video", i));
  return {
    id: uid("video", i), creator: cref(c), platform: c.platform, source_id: c.platform === "twitch" ? `22918${47300 + i}` : `yt${i}Xa9k${i * 7}`,
    url: c.platform === "twitch" ? `https://twitch.tv/videos/22918${47300 + i}` : `https://youtube.com/watch?v=yt${i}Xa9k${i * 7}`,
    title, duration_s: dur, published_at: ago(3600 * (2 + i * 1.5)), trend_score: trend, has_chat: chat, status, skip_reason: skip, attempts: att,
    created_at: ago(3600 * (2 + i * 1.5) - 300), updated_at: ago(600 + i * 400),
    clips: status === "done" || status === "processing" ? sp.filter((s) => s.clip_id).length : 0,
  };
});

function videoDetail(id: string) {
  const v = need(videos.find((x) => x.id === id));
  const i = videos.indexOf(v);
  const sp = v.status === "done" || v.status === "processing" ? spikes.filter((s) => s.video_id === id) : [];
  const first = sp.find((s) => s.clip_id) ?? sp[0];
  const clip = first ? clips.find((c) => c.id === first.clip_id) : undefined;
  const t = traceFor(clip, first);
  const job: Job = {
    id: 88214 - i, queue: "normal", type: "vod.process", priority: 30,
    status: v.status === "processing" ? "running" : v.status === "failed" ? "failed" : v.status === "done" ? "done" : "queued",
    attempts: v.attempts, max_attempts: 3, run_after: v.status === "new" ? ahead(2280) : null,
    locked_by: v.status === "processing" ? "worker-normal-c1" : null, locked_at: v.status === "processing" ? ago(900) : null,
    last_error: v.status === "failed" ? "ERROR: [youtube] Sign in to confirm you’re not a bot." : null, dedupe_key: `vod:${id}`,
    created_at: v.created_at, updated_at: v.updated_at,
  };
  return {
    ...v,
    chat_file: v.has_chat && v.status !== "skipped"
      ? { messages_count: 9400 + i * 3911, duration_s: v.duration_s, baseline_rate: +(4 + (i % 5) * 1.8).toFixed(1), bot_names: BOTS.slice(0, 4), storage_path: `/data/chat/${v.source_id}.jsonl`, downloaded_at: ago(3600 + i * 100) }
      : null,
    spikes: sp, jobs: [job],
    events: eventsFor("video", id).concat(events.filter((e) => e.type.startsWith("video.") || e.type.startsWith("discovery."))).slice(0, 8),
    trace: {
      ...t, creator: { id: v.creator.id, label: `${v.creator.handle} · ${v.creator.tier}`, status: v.creator.tier },
      live_session: null, video: { id: v.id, label: `${v.status} · attempt ${v.attempts}`, status: v.status },
    },
  };
}

/* ---------------- spikes ---------------- */
const CHAT_USERS = ["mike_r", "leo_fan22", "gtaenjoyer", "nora", "bigsmoke_ii", "z3n", "vicefan", "cass", "olly", "dex_99", "kaya", "rook"];
const CHAT_LINES = ["bro is NOT making that", "KEKW KEKW", "CLIP IT", "OMEGALUL THE TOW TRUCK", "clip that 😭", "KEKW", "he really thought", "6 stars speedrun", "LMAOOO", "nahhh", "chat called it", "W streamer"];
function spikeDetail(id: string) {
  const s = need(spikes.find((x) => x.id === id));
  const i = spikes.indexOf(s);
  const base = s.spike_wall ? Date.parse(s.spike_wall) : Date.now();
  const sample = Array.from({ length: 24 }, (_, k) => ({
    ts: new Date(base + (k - 12) * 1500).toISOString(), user: CHAT_USERS[(k + i) % CHAT_USERS.length], text: CHAT_LINES[(k * 7 + i) % CHAT_LINES.length],
  }));
  const clip = s.clip_id ? clips.find((c) => c.id === s.clip_id) : undefined;
  const z = s.score_z ?? 2;
  return {
    ...s, chat_sample: s.mode === "transcript" ? null : sample, msgs_in_bucket: Math.round(40 + z * 25), unique_chatters: Math.round(30 + z * 16),
    reaction_count: Math.round(z * 14), clip_requests: Math.round(z * 3), events: eventsFor("spike", id), trace: traceFor(clip, s),
  };
}

/* ---------------- pipeline ---------------- */
function pipeline(window: string): Pipeline {
  const f = window === "30d" ? 22 : window === "7d" ? 6 : 1;
  const n = (x: number) => Math.round(x * f);
  const postedIds = new Set(posts.map((p) => p.clip_id));
  const tierRows = ["S", "A", "B"].map((t) => {
    const s = spikes.filter((x) => x.tier === t);
    const clipped = s.filter((x) => x.clip_id);
    return { tier: t, detected: n(s.length * 3.2), confirmed: n(s.length), clipped: n(clipped.length), posted: n(clipped.filter((x) => postedIds.has(x.clip_id!)).length) };
  });
  tierRows.push({ tier: "rejected", detected: n(spikes.filter((x) => x.tier === "rejected").length * 3.4), confirmed: 0, clipped: 0, posted: 0 });
  return {
    window,
    nodes: {
      discovery: { candidates: n(1284), picked: n(42) }, live: { active: 4, max_slots: 6, sessions: n(11) },
      vod: { new: 3, processing: 2, done: n(6), failed: n(1), skipped: n(2) },
      spikes: { detected: n(318), confirmed: n(64), rejected: n(178), merged: n(41) }, render: { rendered: n(58), failed: n(2) },
      gate: { approved: n(41), rejected: n(17) }, queue: { waiting: 18, expired: n(3) }, posts: { published: n(61), failed: n(1) },
    },
    funnel: tierRows,
    rejects: ([["score_too_low", 6], ["music_over_limit", 4], ["duplicate_topic", 3], ["spoiler_blocked", 2], ["too_short", 1], ["black_frames", 1]] as const)
      .map(([reason, x]) => ({ reason, n: n(x) })),
    express_stages: ([["detect", 12], ["queue", 6], ["render", 58], ["gate", 9], ["post", 12]] as const)
      .map(([stage, s]) => ({ stage, median_s: s })),
    express_series: [140, 120, 132, 110, 104, 118, 99, 101, 97, 95, 99, 97, 102, 93].map((s, i) => ({ t: ago((14 - i) * 3600 * f), s })),
    approved_by_hour: Array.from({ length: 24 }, (_, i) => ({ t: ago((24 - i) * 3600), n: Math.max(0, Math.round(1 + r() * 3 - (i % 7 === 0 ? 1 : 0))) })),
  };
}

export const routes: MockRoute[] = [
  { method: "GET", pattern: /^admin\/pipeline$/, handle: (_m, q) => pipeline(q.get("window") ?? "24h") },
  {
    method: "GET", pattern: /^admin\/live-sessions$/, handle: (_m, q) => ({
      ...list(sessions, q, "status"),
      meta: { max_slots: 6, blocked: { youtube: ahead(2280) } },
    }),
  },
  { method: "GET", pattern: /^admin\/live-sessions\/([^/]+)$/, handle: (m) => liveDetail(m[1]) },
  {
    method: "GET", pattern: /^admin\/videos$/, handle: (_m, q) => ({
      ...list(videos, q, "status", (v) => !q.get("platform") || v.platform === q.get("platform")),
      meta: { source_health: { youtube: { blocked_since: ago(19200), last_error: "Sign in to confirm you’re not a bot", count: 14, next_retry_at: ahead(2280) }, twitch: { blocked_since: null } } },
    }),
  },
  { method: "GET", pattern: /^admin\/videos\/([^/]+)$/, handle: (m) => videoDetail(m[1]) },
  {
    method: "GET", pattern: /^admin\/spikes$/, handle: (_m, q) => {
      const tier = q.get("tier"), mode = q.get("mode");
      return list<Spike>(spikes, q, "status", (s) => (!tier || s.tier === tier) && (!mode || s.mode === mode));
    },
  },
  { method: "GET", pattern: /^admin\/spikes\/([^/]+)$/, handle: (m) => spikeDetail(m[1]) },
];
