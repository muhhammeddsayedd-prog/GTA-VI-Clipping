"use client";

import Link from "next/link";
import { FileX2, Trash2 } from "lucide-react";

import { CheckRow, Thumb } from "@/components/kit/data";
import { Badge, Card, CardHead, Plat } from "@/components/kit/primitives";
import { useState } from "react";

import { useApi } from "@/lib/api/client";
import type { Clip, ClipDetail, ClipMedia, ClipRender, GateCheck, GateResult } from "@/lib/api/types";
import { fmtClock, humanize, series } from "@/lib/format";
import { isRecord, num, showValue, splitReason } from "@/lib/shape";

export function laneTone(lane: string) { return lane === "spike" ? "purple" : "blue"; }

/** Grid card used on /clips and creator pages. */
export function ClipCard({ c }: { c: Clip }) {
  return (
    <Link href={`/clips/${c.id}`} className="clipcard" aria-label={`Clip ${series(c.series_no)}: ${c.hook ?? c.title ?? ""}, ${humanize(c.status)}`}>
      <Thumb seed={c.id} src={c.thumb_url} kind={c.thumb_kind} hook={c.hook} dur={fmtClock(c.duration_s)} live={c.lane === "spike" && c.source_type === "live"}>
        <div style={{ position: "absolute", right: 8, bottom: 44 }}><Badge status={c.status} /></div>
      </Thumb>
      <div className="meta">
        <div className="row"><span className="semi pc">{series(c.series_no)}</span><span className="right semi num">{c.score ?? "—"}</span></div>
        <div className="t2"><Plat p={c.source_platform} /><span className="ellipsis">{c.creator.handle}</span></div>
        <div className="t2"><Badge tone={laneTone(c.lane)}>{c.lane}</Badge>{c.reject_reason && <span className="down xs ellipsis" title={c.reject_reason}>{rejectLabel(c.reject_reason)}</span>}</div>
      </div>
    </Link>
  );
}

/** Short label for a reject reason: gate codes are humanized, writer sentences get a "Writer:" prefix. */
export function rejectLabel(reason: string | null | undefined): string {
  const r = splitReason(reason);
  return r.kind === "writer" ? `Writer: ${r.text}` : r.kind === "code" ? humanize(r.text) : "";
}

/* ---------- gate checklist ---------- */
const CHECK_LABEL: Record<string, string> = {
  creator_tier: "Creator tier", length: "Length", music_ratio: "Music ratio", music: "Music ratio", spoiler: "Spoiler",
  safety: "Safety flags", watermark_logos: "Watermark / logos (P5)", watermark: "Watermark / logos (P5)", visual: "Visual · black / freeze / menu",
  subtitle_drift: "Subtitle drift", subtitles: "Subtitle drift", score: "Score", duplicate_topic: "Duplicate topic", duplicate: "Duplicates",
  source_range: "Same source range", file: "File", precheck: "YouTube pre-check",
};

export function firstFailing(g: GateResult | null): string | null {
  if (!g?.checks) return null;
  const e = Object.entries(g.checks).find(([, c]) => c.ok === false);
  return e ? e[0] : null;
}

export function GateChecklist({ gate, status, rejectReason }: { gate: GateResult | null; status: string; rejectReason?: string | null }) {
  if (!gate) {
    const writer = splitReason(rejectReason).kind === "writer";
    return (
      <Card>
        <CardHead title="Quality gate" hint="Runs after the render finishes" actions={<Badge status={status} />} />
        <div className="muted small">{writer ? "The gate never ran: the writer rejected this moment before render." : status === "rejected" ? "Rejected before the gate ran." : "The gate has not decided on this clip yet."}</div>
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
              value={showValue(c.value)} limit={showValue(c.limit)} />
          </div>
        ))}
        {!gate.checks?.precheck && <CheckRow state="na" name={CHECK_LABEL.precheck} value={gate.precheck ?? "—"} limit="deferred (D103)" />}
      </div>
      {gate.hourly_fallback && <div className="muted xs mt">Express failed only express checks, so it was re-gated as hourly: {gate.hourly_fallback.passed ? "passed" : "failed"}.</div>}
      {gate.demoted && <div className="muted xs mt">Demoted to the hourly lane (too old for the spike lane).</div>}
    </Card>
  );
}

/* ---------- player ---------- */
export function ClipPlayer({ clip }: { clip: ClipDetail }) {
  // Only ask for a signed URL when the clip has a file; refresh it before it expires (1 h) or when playback fails.
  const m = useApi<ClipMedia>(clip.has_file ? `admin/clips/${clip.id}/media` : null, undefined, { interval: 45 * 60 * 1000, staleTime: 40 * 60 * 1000 });
  const [failed, setFailed] = useState<string | null>(null);
  const media = clip.has_file ? m.data : ({ url: null, expires_at: null, reason: "no_file" } as ClipMedia);
  const gone = media && (media.reason === "deleted" || media.reason === "no_file" || !media.url);
  const fileInfo = isRecord(clip.gate_result?.checks?.file?.value) ? clip.gate_result.checks.file.value : null;
  const dims = fileInfo && num(fileInfo.width) && num(fileInfo.height) ? `${fileInfo.width}×${fileInfo.height}` : null;
  const writerReject = splitReason(clip.reject_reason).kind === "writer";
  const onVideoError = () => {
    if (failed === media?.url) return; // already retried this URL once
    setFailed(media?.url ?? null);
    void m.refetch();
  };
  const label = `Clip ${series(clip.series_no)}: ${clip.hook ?? clip.title ?? ""}`;
  return (
    <div id="player" className="clip-player" style={{ background: "var(--dark)", borderRadius: 30, padding: 12 }}>
      {media?.reason === "ok" && media.url ? (
        <div className="thumb" style={{ width: "100%", background: "#000" }}>
          <video key={media.url} src={media.url} poster={clip.thumb_kind === "frame" && clip.thumb_url ? clip.thumb_url : undefined} controls playsInline preload="metadata" aria-label={label} onError={onVideoError} style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover" }} />
        </div>
      ) : (
        <Thumb seed={clip.id} src={clip.thumb_url} kind={clip.thumb_kind} hook={gone ? null : clip.hook} dur={fmtClock(clip.duration_s)} live={clip.lane === "spike" && clip.source_type === "live"}>
          {(gone || m.error) && (
            <div style={{ position: "absolute", inset: 0, background: "rgba(20,20,32,.82)", display: "grid", placeItems: "center", textAlign: "center", padding: 24 }} role="status">
              <div>
                <div style={{ width: 60, height: 60, borderRadius: 20, background: "var(--dark3)", color: "#BDBDD0", display: "grid", placeItems: "center", margin: "0 auto 14px" }}>
                  {media?.reason === "deleted" ? <Trash2 size={26} /> : <FileX2 size={26} />}
                </div>
                <div className="semi" style={{ fontSize: 15 }}>{m.error ? "Video unavailable" : media?.reason === "deleted" ? "File deleted" : writerReject ? "Never rendered" : "No file to play"}</div>
                <div style={{ color: "var(--dmute)", fontSize: 12, marginTop: 6, lineHeight: 1.5 }}>
                  {m.error ? m.error.message : media?.reason === "deleted"
                    ? <>The file was removed from storage.<br />Transcript and gate result are kept.</>
                    : writerReject ? <>The writer rejected this moment before render, so no video exists.</>
                    : clip.status === "rejected" ? <>Only approved clips are uploaded to storage, so this rejected clip has no video to play.</>
                    : clip.status === "rendering" || clip.status === "gating" ? <>The file is uploaded once the clip is approved.</>
                    : <>The rendered file is not in storage.</>}
                </div>
              </div>
            </div>
          )}
        </Thumb>
      )}
      <div className="row" style={{ color: "#fff", padding: "12px 6px 4px", gap: 8, flexWrap: "wrap" }}>
        {dims && <span className="dbadge">{dims}</span>}
        <span className="dbadge">{clip.file_size_bytes ? `${(clip.file_size_bytes / 1e6).toFixed(1)} MB` : "—"}</span>
        <span className="dbadge">{series(clip.series_no)}</span>
        <span className="right" style={{ color: "var(--dmute)", fontSize: 11 }}>{media?.reason === "ok" ? "signed URL · 1 h" : clip.has_file && m.isLoading ? "loading…" : ""}</span>
      </div>
    </div>
  );
}

/** Human name of a design style id (development-plan/M18). */
export function styleName(id: string | null | undefined): string {
  if (!id) return "—";
  const known: Record<string, string> = { cam_top_viral: "Facecam top · viral", cam_top_title: "Facecam top · title", legacy: "Single style (legacy)" };
  return known[id] ?? humanize(id);
}

/** The renders of a clip, one per design style, each playable on demand (a signed URL per render). */
export function StyleRenders({ clip }: { clip: ClipDetail }) {
  const renders = clip.renders ?? [];
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Card>
      <CardHead title="Design styles" hint="One render per style · the platforms that post it" />
      {renders.length === 0 ? (
        <div className="muted small">Rendered before design styles existed: one file for every platform.</div>
      ) : (
        <div className="col" style={{ gap: 12 }}>
          {renders.map((r) => (
            <div key={r.id} className="col" style={{ gap: 8, padding: 12, border: "1px solid var(--line)", borderRadius: 14 }}>
              <div className="row" style={{ gap: 8, flexWrap: "wrap", justifyContent: "space-between" }}>
                <span className="semi">{styleName(r.style_id)}{r.is_reference && <span className="muted"> · first render</span>}</span>
                <Badge status={r.status} />
              </div>
              <div className="row muted small" style={{ gap: 8, flexWrap: "wrap" }}>
                <span>Layout {r.layout_kind === "C" ? "facecam on top" : r.layout_kind === "A" ? "gameplay on top" : r.layout_kind === "B" ? "no facecam" : "—"}</span>
                {r.render_ms ? <span>· {Math.round(r.render_ms / 1000)} s render</span> : null}
                {r.file_size_bytes ? <span>· {(r.file_size_bytes / 1e6).toFixed(1)} MB</span> : null}
              </div>
              <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                {r.accounts.length ? r.accounts.map((a) => <span key={a} className="chip">{a}</span>) : <span className="muted small">No account planned for it</span>}
              </div>
              {r.error && <div className="small down">{r.error}</div>}
              {r.has_file && (open === r.id ? <RenderPlayer clipId={clip.id} render={r} /> : (
                <button type="button" className="btn sm" onClick={() => setOpen(r.id)}>Play this style</button>
              ))}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function RenderPlayer({ clipId, render }: { clipId: string; render: ClipRender }) {
  const m = useApi<ClipMedia>(`admin/clips/${clipId}/media?render=${render.id}`, undefined, { interval: 45 * 60 * 1000, staleTime: 40 * 60 * 1000 });
  if (m.error) return <div className="small down">Video unavailable: {m.error.message}</div>;
  if (!m.data) return <div className="muted small">Loading…</div>;
  if (m.data.reason !== "ok" || !m.data.url) return <div className="muted small">{m.data.reason === "deleted" ? "The file was removed from storage." : "No file in storage."}</div>;
  return (
    <div className="thumb" style={{ width: "100%", maxWidth: 300, background: "#000", borderRadius: 14 }}>
      <video key={m.data.url} src={m.data.url} controls playsInline preload="metadata" aria-label={`Clip in style ${styleName(render.style_id)}`} style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover" }} />
    </div>
  );
}
