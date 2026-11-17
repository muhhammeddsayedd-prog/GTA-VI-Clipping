/* Local sample-data backend. Each page group registers its routes in its own module. */
import type { Overview, SearchResult } from "@/lib/api/types";
import { accounts, ago, clips, creators, events, posts, spikes } from "./data";
import { routes as contentRoutes } from "./content";
import { routes as clipsRoutes } from "./clips";
import { routes as sourcesRoutes } from "./sources";
import { routes as opsRoutes } from "./ops";

import { MockNotFound, controlState, notFound, type MockResult, type MockRoute } from "./util";

const coreRoutes: MockRoute[] = [
  {
    method: "GET", pattern: /^status$/, handle: () => ({
      env: "dev", posting_mode: controlState.stopped ? "stopped" : controlState.dryRun ? "dry_run" : "live",
      emergency_stop: controlState.stopped, stop_reason: controlState.reason, dry_run: controlState.dryRun, launch_at: "2026-11-18T11:00:00Z",
    }),
  },
  {
    method: "POST", pattern: /^control\/stop$/, handle: (_m, _q, body) => {
      const reason = (body as { reason?: string } | undefined)?.reason || "manual stop (control API)";
      const changed = !controlState.stopped;
      controlState.stopped = true; controlState.reason = reason;
      return { stopped: true, reason, changed };
    },
  },
  {
    method: "POST", pattern: /^control\/start$/, handle: () => {
      const changed = controlState.stopped;
      controlState.stopped = false; controlState.reason = null;
      return { stopped: false, changed };
    },
  },
  { method: "GET", pattern: /^admin\/overview$/, handle: () => overview() },
  { method: "GET", pattern: /^admin\/search$/, handle: (_m, q) => search(q.get("q") ?? "") },
];

function overview(): Overview {
  const upNext = clips.filter((c) => c.status === "approved").sort((a, b) => (b.rank_score ?? 0) - (a.rank_score ?? 0)).slice(0, 5);
  return {
    now: new Date().toISOString(), env: "dev",
    posting_mode: controlState.stopped ? "stopped" : controlState.dryRun ? "dry_run" : "live",
    stop_reason: controlState.reason, launch_at: "2026-11-18T11:00:00Z", dry_run: controlState.dryRun,
    posts_24h: { published: 61, failed: 1, by_hour: [2, 3, 3, 2, 3, 2, 3, 3, 2, 3, 3, 4, 2, 3, 2, 3, 3, 2, 3, 2, 3, 2, 3, 2] },
    queue: { hourly: 15, spike: 3, low_watermark: 12 },
    express: { p50_s: 97, p90_s: 184, series: [140, 120, 132, 110, 104, 118, 99, 101, 97, 95, 99, 97].map((s, i) => ({ t: ago((12 - i) * 7200), s })) },
    spend: { total_usd: 14.82, budget_usd: 30.4, by_provider: [
      { provider: "anthropic", usd: 11.4, budget_usd: 25 }, { provider: "openai", usd: 1.92, budget_usd: 5 },
      { provider: "apify", usd: 0.21, budget_usd: 0.4 }, { provider: "youtube", usd: 0, budget_usd: null }] },
    accounts,
    up_next: upNext.map((c) => ({ clip_id: c.id, series_no: c.series_no, hook: c.hook, title: c.title, creator: c.creator, lane: c.lane,
      score: c.score, rank_score: c.rank_score, duration_s: c.duration_s, created_at: c.created_at,
      age_s: Math.round((Date.now() - Date.parse(c.created_at)) / 1000), expires_at: new Date(Date.parse(c.created_at) + 72 * 3600e3).toISOString() })),
    recent_events: events.slice(0, 8),
    health: { services_ok: 8, services_total: 8, triggers_ok: 4, triggers_total: 4,
      open_incidents: [{ key: "disk", title: "Disk is filling up", opened_at: ago(3120), data: { pct: 81 } }],
      jobs: { queued: 9, running: 6, failed_24h: 3 }, live: { active: 4, max_slots: 6 } },
    growth: {
      accounts: [
        { account_id: "tiktok_main", platform: "tiktok", followers: 2184, delta_24h: 312 },
        { account_id: "instagram_main", platform: "instagram", followers: 906, delta_24h: 88 },
        { account_id: "youtube_main", platform: "youtube", followers: 412, delta_24h: 61 }],
      ypp: { subscribers: 412, subscribers_target: 1000, shorts_views_90d: 1240000, views_target: 10000000 },
    },
  };
}

function search(raw: string): { results: SearchResult[] } {
  const q = raw.trim().toLowerCase().replace(/^#/, "");
  if (!q) return { results: [] };
  const out: SearchResult[] = [];
  const n = Number(q);
  for (const c of clips) {
    if ((Number.isFinite(n) && c.series_no === n) || c.id.startsWith(q) || (q.length > 2 && c.hook?.toLowerCase().includes(q)))
      out.push({ kind: "clip", id: c.id, label: `#${String(c.series_no).padStart(4, "0")} Â· ${c.hook}`, sub: `${c.status} Â· ${c.lane} Â· ${c.creator.handle}`, href: `/clips/${c.id}` });
  }
  for (const p of posts) if (p.id.startsWith(q) || (p.post_url && q.length > 5 && p.post_url.includes(q)))
    out.push({ kind: "post", id: p.id, label: `${p.account_id} Â· #${p.clip_series_no}`, sub: p.status, href: `/posts/${p.id}` });
  for (const c of creators) if (c.handle?.includes(q)) out.push({ kind: "creator", id: c.id, label: c.handle!, sub: `${c.platform} Â· ${c.tier}`, href: `/creators/${c.id}` });
  for (const s of spikes) if (q.length >= 6 && s.id.startsWith(q)) out.push({ kind: "spike", id: s.id, label: `Spike ${s.tier} Â· ${s.creator.handle}`, sub: s.status, href: `/spikes/${s.id}` });
  for (const key of ["gate.hourly", "gate.express", "launch.dry_run", "control.emergency_stop", "posting.min_gap_s", "live.max_slots"])
    if (key.includes(q)) out.push({ kind: "setting", id: key, label: key, sub: "setting", href: `/settings?key=${key}` });
  return { results: out.slice(0, 20) };
}

const ALL: MockRoute[] = [...coreRoutes, ...contentRoutes, ...clipsRoutes, ...sourcesRoutes, ...opsRoutes];

export async function mockHandle(method: "GET" | "POST", path: string, q: URLSearchParams, body: unknown): Promise<MockResult> {
  await new Promise((r) => setTimeout(r, 120 + Math.random() * 180)); // feel like a network
  for (const r of ALL) {
    if (r.method !== method) continue;
    const m = path.match(r.pattern);
    if (!m) continue;
    try {
      const out = r.handle(m, q, body);
      return out && typeof out === "object" && "__status" in (out as object)
        ? { status: (out as { __status: number }).__status, body: (out as { body: unknown }).body }
        : { status: 200, body: out };
    } catch (e) {
      if (e instanceof MockNotFound) return notFound();
      throw e;
    }
  }
  return notFound("route");
}
