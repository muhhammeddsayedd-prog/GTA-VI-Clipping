"use client";

import Link from "next/link";
import { useRef, useState } from "react";

import { DataTable, EventFeed, ExtLink, type Col } from "@/components/kit/data";
import { Badge, Banner, Card, CardHead, ErrorState, KV, PageHead, Plat, SkeletonCards } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { EpisodeDetail, EpisodeMedia, EpisodeMoment } from "@/lib/api/types";
import { fmtClock, series } from "@/lib/format";

const BUILDING = new Set(["planned", "fetching", "rendering", "assembling"]);
const isHttp = (u: string | null | undefined): u is string => !!u && /^https?:\/\//.test(u);

const MOMENT_COLS: Col<EpisodeMoment>[] = [
  { key: "p", head: "#", render: (m) => <span className="num semi">{m.status === "dropped" ? "—" : m.position}</span> },
  { key: "c", head: "Creator", render: (m) => <span className="row" style={{ gap: 8 }}><Plat p={m.creator_platform} />{m.creator_name}</span> },
  {
    key: "t", head: "Moment",
    render: (m) => <Link href={`/clips/${m.clip_id}`} className="pc">{m.series_no ? `${series(m.series_no)} ` : ""}{m.title ?? "Untitled"}</Link>,
  },
  { key: "sc", head: "Score", align: "right", render: (m) => <span className="num">{m.score ?? "—"}</span> },
  {
    key: "l", head: "Length", align: "right",
    render: (m) => <span className="num">{m.duration_s ? fmtClock(m.duration_s) : m.est_s ? `~${fmtClock(m.est_s)}` : "—"}</span>,
  },
  {
    key: "s", head: "Status",
    render: (m) => (
      <span title={m.error ?? undefined}>
        <Badge status={m.status} />
        {m.error ? <span className="small down" style={{ marginLeft: 6 }}>{m.error.slice(0, 80)}</span> : null}
      </span>
    ),
  },
  {
    key: "src", head: "Source",
    render: (m) => (isHttp(m.fetch_url) ? <ExtLink href={m.fetch_url}>{m.source_type === "live" ? "archive" : "VOD"}</ExtLink> : <span className="muted">—</span>),
  },
];

function Player({ episode }: { episode: EpisodeDetail }) {
  // Signed URLs last 1 h: refresh before they expire, and once when playback fails.
  const m = useApi<EpisodeMedia>(episode.has_file ? `admin/episodes/${episode.id}/media` : null, undefined, { interval: 45 * 60 * 1000, staleTime: 40 * 60 * 1000 });
  const video = useRef<HTMLVideoElement>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const url = m.data?.video.reason === "ok" ? m.data.video.url : null;
  const chapters = episode.chapters ?? [];
  const seek = (t: number) => {
    if (!video.current) return;
    video.current.currentTime = t;
    void video.current.play();
  };
  const onError = () => {
    if (failed === url) return;
    setFailed(url);
    void m.refetch();
  };
  const empty = BUILDING.has(episode.status)
    ? "Still building: the video appears here once it is assembled."
    : m.error ? m.error.message : m.data?.video.reason === "deleted" ? "The file was removed from storage." : "No video file.";
  return (
    <Card>
      <CardHead
        title={episode.title ?? "Episode"}
        hint={episode.duration_s ? `${fmtClock(episode.duration_s)} · ${episode.n_moments ?? 0} moments` : undefined}
        actions={url ? <a className="pc semi small" href={url} download>Download MP4</a> : null}
      />
      <div style={{ position: "relative", width: "100%", aspectRatio: "16 / 9", background: "#000", borderRadius: 14, overflow: "hidden" }}>
        {url ? (
          <video ref={video} key={url} src={url} poster={m.data?.thumb.url ?? undefined} controls playsInline preload="metadata"
            aria-label={episode.title ?? "Episode"} onError={onError}
            style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "contain" }} />
        ) : (
          <div role="status" style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", color: "#BDBDD0", textAlign: "center", padding: 24 }}>
            {empty}
          </div>
        )}
      </div>
      {chapters.length > 0 && (
        <ol aria-label="Chapters" style={{ listStyle: "none", padding: 0, margin: "12px 0 0", display: "grid", gap: 2 }}>
          {chapters.map((c) => (
            <li key={`${c.start_s}-${c.label}`}>
              <button type="button" className="row small" disabled={!url} onClick={() => seek(c.start_s)}
                style={{ gap: 10, background: "none", border: 0, padding: "4px 0", cursor: url ? "pointer" : "default", color: "inherit", textAlign: "left" }}>
                <span className="num pc semi" style={{ minWidth: 48 }}>{fmtClock(c.start_s)}</span>
                <span>{c.label}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

const THUMB_SOURCE: Record<string, string> = {
  ai: "AI image",
  ai_no_avatar: "AI image (without the creator picture)",
  frame: "video frame (AI image not available)",
};

function Thumb({ episode }: { episode: EpisodeDetail }) {
  const m = useApi<EpisodeMedia>(episode.has_thumb ? `admin/episodes/${episode.id}/media` : null, undefined, { staleTime: 40 * 60 * 1000 });
  const url = m.data?.thumb.reason === "ok" ? m.data.thumb.url : null;
  if (!episode.has_thumb) return null;
  return (
    <Card>
      <CardHead title="Thumbnail" hint={episode.thumb_source ? THUMB_SOURCE[episode.thumb_source] ?? episode.thumb_source : undefined} />
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 480px) minmax(0, 1fr)", gap: 16, alignItems: "start" }}>
        <div style={{ aspectRatio: "16 / 9", background: "#000", borderRadius: 12, overflow: "hidden" }}>
          {url ? <img src={url} alt={`Thumbnail of ${episode.title ?? "the episode"}`} style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} /> : null}
        </div>
        <KV rows={[
          ["Words", episode.thumb_text || "—"],
          ["Picture brief", episode.thumb_scene || "—"],
        ]} />
      </div>
    </Card>
  );
}

/** One episode: the 16:9 player with clickable chapters, its moments and its YouTube text (M20 L5). */
export function EpisodeDetailView({ id }: { id: string }) {
  const q = useApi<EpisodeDetail>(`admin/episodes/${id}`, undefined, { interval: POLL.detail });
  if (q.error && !q.data) return <><PageHead title="Episode" back="/episodes" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Episode" back="/episodes" sub="Loading…" /><SkeletonCards n={2} h={420} /></>;
  const e = q.data;
  const kept = e.moments.filter((m) => m.status !== "dropped").length;
  const done = e.moments.filter((m) => m.status === "rendered").length;
  return (
    <>
      <PageHead title={`Episode ${e.episode_date}`} back="/episodes"
        sub={<>
          <Badge status={e.status} />{" "}
          <span className="small">{e.format === "creator" ? `Creator day: ${e.featured_name ?? "—"}` : "Mix day"}</span>{" "}
          {BUILDING.has(e.status) ? `${done}/${kept} moments rendered` : null}
        </>} />
      {e.error && <Banner tone={e.status === "failed" ? "red" : "amber"}>{e.error}</Banner>}
      <div className="col" style={{ gap: 16, marginTop: 16 }}>
        <Player episode={e} />
        <Card>
          <CardHead title="Moments" hint="Playback order: the countdown ends with #1. Dropped moments could not be downloaded." />
          <DataTable caption="Moments" cols={MOMENT_COLS} rows={e.moments} rowKey={(m) => m.id} dense />
        </Card>
        <Thumb episode={e} />
        <Card>
          <CardHead title="YouTube text" hint="Written by the episode writer (P14); the template is used if it fails." />
          <KV rows={[
            ["Title", e.title ?? "—"],
            ["Hashtags", e.hashtags?.join(" ") || "—"],
            ["Tags", e.tags?.join(", ") || "—"],
            ["Account", e.account_id ?? "—"],
            ["Privacy", e.privacy ?? "—"],
            ["Post", isHttp(e.post_url) ? <ExtLink href={e.post_url}>open on YouTube</ExtLink> : "not posted"],
          ]} />
          {e.description && <pre className="small" style={{ whiteSpace: "pre-wrap", marginTop: 12 }}>{e.description}</pre>}
        </Card>
        <Card>
          <CardHead title="Events" />
          <EventFeed events={e.events} />
        </Card>
      </div>
    </>
  );
}
