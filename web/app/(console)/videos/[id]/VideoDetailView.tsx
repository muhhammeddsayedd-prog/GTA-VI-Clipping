"use client";

import Link from "next/link";
import { ArrowUpRight, List } from "lucide-react";

import { DataTable, EventFeed, Steps, TraceBar, type Col } from "@/components/kit/data";
import { Badge, Btn, Card, CardHead, Chip, ErrorState, KV, Mono, PageHead, Plat, SkeletonCards, TierChip, When } from "@/components/kit/primitives";
import { ClipLink, Timeline, TierLegend, spikeAt } from "@/components/domain/content/bits";
import { POLL, useApi } from "@/lib/api/client";
import type { Spike, VideoDetail } from "@/lib/api/types";
import { fmtClock, fmtDuration, fmtNum, humanize } from "@/lib/format";

const spikeCols: Col<Spike>[] = [
  { key: "t", head: "Tier", mobile: "lead", render: (s) => <TierChip tier={s.tier} /> },
  { key: "o", head: "Offset", render: (s) => <span className="num">{spikeAt(s)}</span> },
  { key: "z", head: "z", align: "right", render: (s) => <b className="num">{s.score_z ?? "—"}</b> },
  { key: "c", head: "Confidence", align: "right", render: (s) => (s.claude_confidence !== null ? s.claude_confidence.toFixed(2) : "—") },
  { key: "m", head: "Moment", mobile: "primary", render: (s) => <span className="ink2">{s.claude_reason ?? "—"}</span> },
  { key: "st", head: "Status", render: (s) => <Badge status={s.status} /> },
  { key: "cl", head: "Clip", render: (s) => <ClipLink id={s.clip_id} no={s.clip_series_no} /> },
];

export function VideoDetailView({ id }: { id: string }) {
  const q = useApi<VideoDetail>(`admin/videos/${id}`, undefined, { interval: POLL.detail });
  if (q.error && !q.data) return <><PageHead title="Video" back="/videos" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Video" back="/videos" sub="Loading…" /><SkeletonCards n={2} h={220} /></>;
  const v = q.data;
  const spikes = [...v.spikes].sort((a, b) => (b.score_z ?? 0) - (a.score_z ?? 0));
  const confirmed = spikes.filter((s) => s.status !== "rejected" && s.status !== "merged" && s.tier !== "rejected");
  const clipped = spikes.filter((s) => s.clip_id);
  const job = v.jobs[0];
  const dur = v.duration_s ?? Math.max(...spikes.map((s) => s.spike_offset_s ?? 0), 1);
  const marks = spikes.filter((s) => s.spike_offset_s !== null).map((s) => ({ at: (s.spike_offset_s ?? 0) / dur, tier: s.tier ?? "rejected", title: `Tier ${s.tier} at ${fmtClock(s.spike_offset_s)} · z ${s.score_z}` }));
  const ticks = Array.from({ length: 6 }, (_, i) => fmtClock((dur * i) / 5));
  const st = v.status;
  const done = st === "done";
  const steps = [
    { state: v.chat_file ? "done" as const : st === "skipped" ? "" as const : v.has_chat === false ? "done" as const : "" as const,
      title: v.has_chat === false ? "No chat replay" : "Chat replay downloaded",
      sub: v.chat_file ? `${fmtNum(v.chat_file.messages_count)} messages · ${v.chat_file.bot_names?.length ?? 0} bots removed · baseline ${v.chat_file.baseline_rate ?? "—"} msgs / 10 s` : v.has_chat === false ? "Transcript fallback picks the moments" : "Waiting" },
    { state: spikes.length ? "done" as const : "" as const, title: "Spikes detected", sub: spikes.length ? `${spikes.length} kept after merging (cap 25 per video)` : "Not yet" },
    { state: spikes.length ? "done" as const : "" as const, title: "Claude confirmation (P1, batched)", sub: spikes.length ? `${confirmed.length} confirmed · ${spikes.length - confirmed.length} rejected as noise or merged` : "Not yet" },
    { state: st === "failed" ? "fail" as const : done ? "done" as const : st === "processing" ? "run" as const : "" as const,
      title: "Downloading windows", sub: st === "failed" ? job?.last_error ?? "download failed" : done ? "All windows downloaded" : st === "processing" ? "yt-dlp sections in progress" : v.skip_reason ? humanize(v.skip_reason) : "Waiting for a worker" },
    { state: clipped.length ? "done" as const : "" as const, title: "Render jobs", sub: clipped.length ? `${clipped.length} clip${clipped.length > 1 ? "s" : ""} rendered` : "clip.render is queued per confirmed spike" },
    { state: done || v.has_chat === false ? "done" as const : "" as const, title: "Transcript fallback",
      sub: v.has_chat === false ? "Used: no chat on this video" : confirmed.length >= 3 ? `Not needed — ${confirmed.length} confirmed spikes ≥ 3` : "Used when fewer than 3 spikes are confirmed" },
  ];

  return (
    <>
      <PageHead back="/videos" title={<span className="clamp2">{v.title ?? v.source_id}</span>}
        sub={<span className="row" style={{ gap: 8, flexWrap: "wrap" }}><Plat p={v.platform} />{v.creator.handle} · {v.platform === "twitch" ? "Twitch" : "YouTube"} VOD · {fmtDuration(v.duration_s)} · <Badge status={v.status} /></span>}
        actions={<>{v.url && <Btn href={v.url} external icon={<ArrowUpRight size={15} />}>Open VOD</Btn>}<Btn kind="p" href={`/events?entity_type=video&entity_id=${v.id}`} icon={<List size={15} />}>Video events</Btn></>} />
      <TraceBar trace={v.trace} on="video" />

      <div className="g collapse-md" style={{ gridTemplateColumns: "1fr 380px" }}>
        <Card>
          <CardHead title="Spikes over the stream" hint="Each marker is a kept spike at its offset into the video" actions={<TierLegend />} />
          {marks.length ? <Timeline marks={marks} ticks={ticks} label={`${marks.length} spikes over ${fmtDuration(dur)}`} height={150} />
            : <div className="muted small" style={{ padding: "40px 0", textAlign: "center" }}>{v.skip_reason ? `Skipped: ${humanize(v.skip_reason)}.` : "No spikes yet."}</div>}
        </Card>
        <Card>
          <CardHead title="Processing" hint={job ? <>vod.process · job <Link className="pc" href="/jobs?type=vod.process">#{job.id}</Link></> : "vod.process"} />
          <Steps steps={steps} />
        </Card>
      </div>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1fr 380px" }}>
        <Card>
          <CardHead title="Spikes kept" hint={`${spikes.length} · sorted by z-score`} actions={<Btn sm href="/spikes?mode=replay">All spikes</Btn>} />
          <DataTable cols={spikeCols} rows={spikes} rowKey={(s) => s.id} href={(s) => `/spikes/${s.id}`} dense caption="Spikes kept for this video" empty="No spikes kept." />
        </Card>
        <div className="col">
          <Card>
            <CardHead title="Chat file" />
            {v.chat_file ? (
              <>
                <KV rows={[
                  ["Messages", fmtNum(v.chat_file.messages_count)],
                  ["Duration", fmtDuration(v.chat_file.duration_s)],
                  ["Baseline rate", `${v.chat_file.baseline_rate ?? "—"} / 10 s`],
                  ["Downloaded", <When key="d" iso={v.chat_file.downloaded_at} />],
                  ["Stored", <Mono key="p">{v.chat_file.storage_path ?? "—"}</Mono>],
                  ["Deleted after", "7 days"],
                ]} />
                {!!v.chat_file.bot_names?.length && <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 12 }}>{v.chat_file.bot_names.map((b) => <Chip key={b}>{b}</Chip>)}</div>}
              </>
            ) : <div className="muted small">{v.has_chat === false ? "This video has no chat replay." : "Not downloaded yet."}</div>}
          </Card>
          {job && (
            <Card>
              <CardHead title="Job" />
              <KV rows={[["Status", <Badge key="s" status={job.status} />], ["Attempts", `${job.attempts} of ${job.max_attempts}`], ["Worker", job.locked_by ?? "—"], ["Run after", <When key="r" iso={job.run_after} />]]} />
              {job.last_error && <pre className="code wrap" style={{ marginTop: 12, color: "#FFB4B6" }}>{job.last_error}</pre>}
            </Card>
          )}
        </div>
      </div>

      <Card style={{ marginTop: 16 }}>
        <CardHead title="Video events" />
        <EventFeed events={v.events.slice(0, 10)} />
      </Card>
    </>
  );
}
