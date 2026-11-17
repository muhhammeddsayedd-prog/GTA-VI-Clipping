"use client";

import type { ReactNode } from "react";

import { Badge, Card, Prog } from "@/components/kit/primitives";
import { fmtAgo, humanize } from "@/lib/format";

/** Health record written by code into settings (discovery/live/vod .source_health). Shape varies by source; read defensively. */
export type HealthRec = Record<string, unknown>;

export function healthState(h: HealthRec | null | undefined): "ok" | "blocked" | "failing" | "n/a" {
  if (!h) return "n/a";
  const until = typeof h.backoff_until === "string" ? h.backoff_until : typeof h.until === "string" ? h.until : null;
  if (h.status === "blocked" || h.blocked_since || (until && Date.parse(until) > Date.now())) return "blocked";
  if (h.ok === false || (typeof h.failures === "number" && h.failures > 0)) return "failing";
  return "ok";
}

const NAMES: Record<string, [string, string]> = {
  twitch_helix: ["Twitch Helix", "live streams + archives"],
  youtube_rss: ["YouTube RSS", "followed channels"],
  yt_dlp_search: ["yt-dlp search", "YouTube search"],
  data_api_search: ["Data API search", "fallback search"],
  apify: ["Apify", "YouTube search actor"],
  youtube: ["YouTube", "downloads"],
  twitch: ["Twitch", "downloads"],
};
export const sourceName = (k: string): [string, string] => NAMES[k] ?? [humanize(k), "source"];

export function SourceCard({ k, h, meter }: { k: string; h: HealthRec | null; meter?: { pct: number; label: string; tone?: "amber" | "red" } }) {
  const [name, sub] = sourceName(k);
  const st = healthState(h);
  const until = (h?.backoff_until ?? h?.until) as string | undefined;
  let detail: ReactNode = (h?.detail as string | undefined) ?? (h?.last_ok_at ? `last ok ${fmtAgo(h.last_ok_at as string)}` : "—");
  if (st === "blocked") detail = <>backoff{until ? <> until {fmtAgo(until).replace("in ", "")}</> : null} · {String(h?.failures ?? h?.count ?? "?")} fails</>;
  if (st === "failing") detail = `${String(h?.failures)} recent failures`;
  return (
    <Card as="div">
      <div className="row"><span className="semi">{name}</span><span className="right"><Badge status={st === "failing" ? "warn" : st}>{st === "failing" ? "failing" : undefined}</Badge></span></div>
      <div className="muted xs" style={{ marginTop: 4 }}>{sub}</div>
      <div className="small semi" style={{ marginTop: 14 }}>{detail}</div>
      {typeof h?.last_error === "string" && <div className="xs down ellipsis" style={{ marginTop: 6 }} title={h.last_error}>{h.last_error}</div>}
      {meter && <div className="mt"><Prog pct={meter.pct} tone={meter.tone} thin label={meter.label} /><div className="muted xs" style={{ marginTop: 6 }}>{meter.label}</div></div>}
    </Card>
  );
}
