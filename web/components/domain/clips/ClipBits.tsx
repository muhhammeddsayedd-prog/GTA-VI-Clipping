"use client";

import Link from "next/link";
import { FileX2, Trash2 } from "lucide-react";

import { CheckRow, Thumb } from "@/components/kit/data";
import { Badge, Card, CardHead, Plat } from "@/components/kit/primitives";
import { useApi } from "@/lib/api/client";
import type { Clip, ClipDetail, ClipMedia, GateCheck, GateResult, Json } from "@/lib/api/types";
import { fmtClock, humanize, series } from "@/lib/format";

export function laneTone(lane: string) { return lane === "spike" ? "purple" : "blue"; }

/** Grid card used on /clips and creator pages. */
export function ClipCard({ c }: { c: Clip }) {
  return (
    <Link href={`/clips/${c.id}`} className="clipcard" aria-label={`Clip ${series(c.series_no)}: ${c.hook ?? c.title ?? ""}, ${humanize(c.status)}`}>
      <Thumb seed={c.id} hook={c.hook} dur={fmtClock(c.duration_s)} live={c.lane === "spike" && c.source_type === "live"}>
        <div style={{ position: "absolute", right: 8, bottom: 44 }}><Badge status={c.status} /></div>
      </Thumb>
      <div className="meta">
        <div className="row"><span className="semi pc">{series(c.series_no)}</span><span className="right semi num">{c.score ?? "—"}</span></div>
        <div className="t2"><Plat p={c.source_platform} /><span className="ellipsis">{c.creator.handle}</span></div>
        <div className="t2"><Badge tone={laneTone(c.lane)}>{c.lane}</Badge>{c.reject_reason && <span className="down xs ellipsis">{humanize(c.reject_reason)}</span>}</div>
      </div>
    </Link>
  );
}

/* ---------- gate checklist ---------- */
const CHECK_LABEL: Record<string, string> = {
  creator_tier: "Creator tier", length: "Length", music_ratio: "Music ratio", music: "Music ratio", spoiler: "Spoiler",
  safety: "Safety flags", watermark_logos: "Watermark / logos (P5)", watermark: "Watermark / logos (P5)", visual: "Visual · black / freeze / menu",
  subtitle_drift: "Subtitle drift", subtitles: "Subtitle drift", score: "Writer score", duplicate_topic: "Duplicate topic", duplicate: "Duplicates",
  source_range: "Same source range", file: "File", precheck: "YouTube pre-check",
};
function show(v: Json | undefined): string {
  if (v === undefined || v === null) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

export function firstFailing(g: GateResult | null): string | null {
  if (!g?.checks) return null;
  const e = Object.entries(g.checks).find(([, c]) => c.ok === false);
  return e ? e[0] : null;
}

export function GateChecklist({ gate, status }: { gate: GateResult | null; status: string }) {
  if (!gate) {
    return (
      <Card>
        <CardHead title="Quality gate" hint="Runs after the render finishes" actions={<Badge status={status} />} />
        <div className="muted small">The gate has not decided on this clip yet.</div>
      </Card>
    );
  }
  const entries = Object.entries(gate.checks ?? {}) as [string, GateCheck][];
  const first = firstFailing(gate);
  const verdict = gate.verdict ?? (gate.passed ? "approve" : "reject");
  return (
    <Card>
      <CardHead title="Quality gate" hint={<>Profile <b>{gate.profile ?? "—"}</b> · first failing check becomes the reject reason</>}
        actions={<Badge status={verdict === "approve" ? "approved" : verdict === "hold" ? "precheck_pending" : "rejected"} />} />
      {gate.error && <div className="banner red" style={{ margin: "0 0 10px" }}>Gate error: {gate.error}</div>}
      <div role="list" aria-label="Gate checks">
        {entries.map(([k, c]) => (
          <div role="listitem" key={k}>
            <CheckRow state={c.ok === false ? "no" : c.ok === true ? "ok" : "na"}
              name={<>{CHECK_LABEL[k] ?? humanize(k)}{k === first && <span className="b red nodot" style={{ marginLeft: 8 }}>first fail</span>}{c.codes?.length ? <span className="mono xs down" style={{ marginLeft: 6 }}>{c.codes.join(", ")}</span> : null}</>}
              value={show(c.value)} limit={show(c.limit)} />
          </div>
        ))}
        <CheckRow state="na" name={CHECK_LABEL.precheck} value={gate.precheck ?? "—"} limit="deferred (D103)" />
      </div>
      {gate.hourly_fallback && <div className="muted xs mt">Express failed only express checks, so it was re-gated as hourly: {gate.hourly_fallback.passed ? "passed" : "failed"}.</div>}
      {gate.demoted && <div className="muted xs mt">Demoted to the hourly lane (too old for the spike lane).</div>}
    </Card>
  );
}

/* ---------- player ---------- */
export function ClipPlayer({ clip }: { clip: ClipDetail }) {
  const m = useApi<ClipMedia>(`admin/clips/${clip.id}/media`, undefined, { interval: false, staleTime: 50 * 60 * 1000 });
  const media = m.data;
  const gone = media && (media.reason === "deleted" || media.reason === "no_file" || !media.url);
  const label = `Clip ${series(clip.series_no)}: ${clip.hook ?? clip.title ?? ""}`;
  return (
    <div id="player" style={{ background: "var(--dark)", borderRadius: 30, padding: 12 }}>
      {media?.reason === "ok" && media.url ? (
        <div className="thumb" style={{ width: "100%", background: "#000" }}>
          <video src={media.url} controls playsInline preload="metadata" aria-label={label} style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover" }} />
        </div>
      ) : (
        <Thumb seed={clip.id} hook={gone ? null : clip.hook} dur={fmtClock(clip.duration_s)} live={clip.lane === "spike" && clip.source_type === "live"}>
          {(gone || m.error) && (
            <div style={{ position: "absolute", inset: 0, background: "rgba(20,20,32,.82)", display: "grid", placeItems: "center", textAlign: "center", padding: 24 }} role="status">
              <div>
                <div style={{ width: 60, height: 60, borderRadius: 20, background: "var(--dark3)", color: "#BDBDD0", display: "grid", placeItems: "center", margin: "0 auto 14px" }}>
                  {media?.reason === "deleted" ? <Trash2 size={26} /> : <FileX2 size={26} />}
                </div>
                <div className="semi" style={{ fontSize: 15 }}>{m.error ? "Video unavailable" : media?.reason === "deleted" ? "File deleted" : "No file to play"}</div>
                <div style={{ color: "var(--dmute)", fontSize: 12, marginTop: 6, lineHeight: 1.5 }}>
                  {m.error ? m.error.message : media?.reason === "deleted"
                    ? <>Rejected clips are kept 24 h, posted clips 3 days.<br />Transcript and gate result are kept.</>
                    : <>The rendered file is not in storage{clip.status === "rendering" ? " yet — it is still rendering" : ""}.</>}
                </div>
              </div>
            </div>
          )}
        </Thumb>
      )}
      <div className="row" style={{ color: "#fff", padding: "12px 6px 4px", gap: 8, flexWrap: "wrap" }}>
        <span className="dbadge">1080×1920</span>
        <span className="dbadge">{clip.file_size_bytes ? `${(clip.file_size_bytes / 1e6).toFixed(1)} MB` : "—"}</span>
        <span className="dbadge">{series(clip.series_no)}</span>
        <span className="right" style={{ color: "var(--dmute)", fontSize: 11 }}>{media?.reason === "ok" ? "signed URL · 1h" : m.isLoading ? "loading…" : ""}</span>
      </div>
    </div>
  );
}
