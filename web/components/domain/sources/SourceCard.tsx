"use client";

import type { ReactNode } from "react";

import { Badge, Card, Prog } from "@/components/kit/primitives";
import { fmtNum, humanize } from "@/lib/format";

/**
 * One discovery source as the last pass reported it (`discovery.run` → `data.sources[]`):
 * `{ name, status: "ok" | "skipped" | "failed" | "blocked" | …, found, detail }`.
 */
export interface SourceReport { status: string; found: number | null; detail: string | null }

export function healthState(h: SourceReport | null | undefined): "ok" | "blocked" | "failing" | "skipped" | "n/a" {
  if (!h) return "n/a";
  if (h.status === "ok") return "ok";
  if (h.status === "skipped") return "skipped";
  if (h.status === "blocked") return "blocked";
  return "failing";
}

const NAMES: Record<string, [string, string]> = {
  twitch: ["Twitch", "live streams + archives"],
  twitch_clips: ["Twitch Clips", "clips made by viewers"],
  youtube_search: ["YouTube search", "yt-dlp search"],
  youtube_search_api: ["YouTube search API", "fallback search"],
  youtube_rss: ["YouTube RSS", "followed channels"],
  youtube_api: ["YouTube Data API", "video details"],
  apify: ["Apify", "YouTube search actor"],
};
export const sourceName = (k: string): [string, string] => NAMES[k] ?? [humanize(k), "source"];

export function SourceCard({ k, h, meter }: { k: string; h: SourceReport | null; meter?: { pct: number; label: string; tone?: "amber" | "red" } }) {
  const [name, sub] = sourceName(k);
  const st = healthState(h);
  const found: ReactNode = h?.found !== null && h?.found !== undefined ? <>{fmtNum(h.found)} found</> : "—";
  return (
    <Card as="div">
      <div className="row">
        <span className="semi">{name}</span>
        <span className="right">
          {st === "skipped" ? <Badge tone="gray">skipped</Badge> : st === "failing" ? <Badge status="warn">{h?.status ? humanize(h.status) : "failing"}</Badge> : <Badge status={st} />}
        </span>
      </div>
      <div className="muted xs" style={{ marginTop: 4 }}>{sub}</div>
      <div className="small semi" style={{ marginTop: 14 }}>{found}</div>
      {h?.detail && <div className={`xs ${st === "failing" || st === "blocked" ? "down" : "muted"}`} style={{ marginTop: 6, lineHeight: 1.45 }} title={h.detail}>{h.detail}</div>}
      {meter && <div className="mt"><Prog pct={meter.pct} tone={meter.tone} thin label={meter.label} /><div className="muted xs" style={{ marginTop: 6 }}>{meter.label}</div></div>}
    </Card>
  );
}
