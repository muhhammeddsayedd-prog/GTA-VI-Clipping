"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

/** Current time, refreshed every `ms` (keeps render pure). */
export function useNow(ms = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

import { Badge } from "@/components/kit/primitives";
import type { Spike } from "@/lib/api/types";
import { fmtClock, fmtTime, series } from "@/lib/format";

export const TIER_COLOR: Record<string, string> = { S: "#5A4AF4", A: "#2F7CF6", B: "#D27A06", rejected: "#9a9aae" };
const PART_COLOR = ["#C9C2FF", "#9F94FF", "#5A4AF4", "#3A2CC7", "#1B1B27"];
export const PART_LABELS: Record<string, string> = {
  msgs_z: "Messages", uniq_z: "Unique chatters", react_z: "Reactions (KEKW, OMEGALUL…)", clipreq_z: "Clip requests (“clip it”)", audio_z: "Audio loudness",
};

/** Tiny bar group of the z-score parts. */
export function PartsMini({ parts }: { parts: Record<string, number> | null }) {
  if (!parts) return <span className="muted xs">no chat</span>;
  const keys = ["msgs_z", "uniq_z", "react_z", "clipreq_z"];
  const label = keys.map((k) => `${PART_LABELS[k]} ${parts[k] ?? 0}`).join(", ");
  return (
    <div className="row" style={{ gap: 3, alignItems: "flex-end", height: 22 }} role="img" aria-label={label} title={label}>
      {keys.map((k, i) => <i key={k} style={{ width: 7, height: Math.max(3, Math.min(22, (parts[k] ?? 0) * 5)), borderRadius: 2, background: PART_COLOR[i] }} />)}
    </div>
  );
}

export function ModeBadge({ mode }: { mode: string }) {
  return <Badge tone={mode === "live" ? "purple" : mode === "transcript" ? "amber" : "blue"} dot={false}>{mode}</Badge>;
}

/** Where a spike happened: wall clock for live, offset into the video for replay/transcript. */
export function spikeAt(s: Spike): string {
  if (s.spike_wall) return fmtTime(s.spike_wall, true);
  if (s.spike_offset_s !== null) return fmtClock(s.spike_offset_s);
  return fmtTime(s.detected_at);
}

export function ClipLink({ id, no }: { id: string | null; no: number | null }) {
  if (!id) return <span className="muted">—</span>;
  return <Link href={`/clips/${id}`} className="pc semi" onClick={(e) => e.stopPropagation()}>{series(no)}</Link>;
}

/** Horizontal timeline with spike markers and highlighted ranges. Positions are 0..1. */
export function Timeline({ marks, ranges, ticks, label, height = 120 }: {
  marks: { at: number; tier: string; title: string; href?: string }[];
  ranges?: { from: number; to: number; title: string }[];
  ticks: string[];
  label: string;
  height?: number;
}) {
  const W = 1000;
  const lane = height - 34;
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${height}`} width="100%" height={height} role="img" aria-label={label} preserveAspectRatio="none" style={{ display: "block" }}>
        <title>{label}</title>
        <rect x={0} y={lane - 10} width={W} height={12} rx={6} fill="#F0F0F5" />
        {ranges?.map((r, i) => (
          <rect key={i} x={r.from * W} y={lane - 10} width={Math.max(6, (r.to - r.from) * W)} height={12} rx={6} fill="#5A4AF4" opacity={0.85}><title>{r.title}</title></rect>
        ))}
        {marks.map((m, i) => {
          const x = Math.max(6, Math.min(W - 6, m.at * W));
          const h = m.tier === "S" ? 70 : m.tier === "A" ? 54 : m.tier === "B" ? 40 : 26;
          return (
            <g key={i}>
              <line x1={x} x2={x} y1={lane - 10 - h + 20} y2={lane - 4} stroke={TIER_COLOR[m.tier] ?? "#9a9aae"} strokeWidth={2} strokeDasharray={m.tier === "rejected" ? "3 3" : undefined} />
              <circle cx={x} cy={lane - 10 - h + 20} r={m.tier === "S" ? 6 : 4.5} fill={TIER_COLOR[m.tier] ?? "#9a9aae"} stroke="#fff" strokeWidth={2}><title>{m.title}</title></circle>
            </g>
          );
        })}
      </svg>
      <div className="row muted xs" style={{ justifyContent: "space-between", marginTop: 4 }}>{ticks.map((t, i) => <span key={i}>{t}</span>)}</div>
    </div>
  );
}

export function TierLegend() {
  return (
    <div className="legend">
      {["S", "A", "B", "rejected"].map((t) => <span key={t}><i style={{ background: TIER_COLOR[t] }} />{t === "rejected" ? "rejected" : `Tier ${t}`}</span>)}
    </div>
  );
}

export function Section({ children }: { children: ReactNode }) {
  return <div className="mt">{children}</div>;
}
