"use client";

import { ArrowUpRight, List } from "lucide-react";

import { DataTable, EventFeed, TraceBar, type Col } from "@/components/kit/data";
import { Badge, Btn, Card, CardHead, Chip, ErrorState, KV, Mono, PageHead, Plat, SkeletonCards, TierChip, When } from "@/components/kit/primitives";
import { ClipLink, Timeline, TierLegend, spikeAt, useNow } from "@/components/domain/content/bits";
import { POLL, useApi } from "@/lib/api/client";
import type { Event, LiveSession, Spike, Trace } from "@/lib/api/types";
import { fmtDuration, fmtTime } from "@/lib/format";
import { useSettings } from "@/lib/use-settings";

/** The contract reuses `spikes` for the list on the detail route; accept both shapes. */
type LiveDetail = Omit<LiveSession, "spikes"> & {
  spikes: Spike[] | LiveSession["spikes"]; spikes_list?: Spike[];
  bot_names: string[] | null; clipped_ranges: { start_wall: string; end_wall: string; clip_id: string }[] | null;
  vod_source_id: string | null; buffer_dir: string | null; events: Event[]; trace: Trace;
};

const spikeCols: Col<Spike>[] = [
  { key: "t", head: "Tier", mobile: "lead", render: (s) => <TierChip tier={s.tier} /> },
  { key: "at", head: "Detected", render: (s) => <span className="num">{spikeAt(s)}</span> },
  { key: "z", head: "z-score", align: "right", render: (s) => <b className="num">{s.score_z ?? "—"}</b> },
  { key: "c", head: "Claude", align: "right", render: (s) => (s.claude_confidence !== null ? s.claude_confidence.toFixed(2) : "—") },
  { key: "m", head: "Moment", mobile: "primary", render: (s) => <span className="ink2">{s.claude_reason ?? "—"}</span> },
  { key: "st", head: "Status", render: (s) => <Badge status={s.status} /> },
  { key: "clip", head: "Clip", render: (s) => <ClipLink id={s.clip_id} no={s.clip_series_no} /> },
];

export function LiveDetailView({ id }: { id: string }) {
  const maxReconnects = useSettings().num("live.max_reconnects");
  const q = useApi<LiveDetail>(`admin/live-sessions/${id}`, undefined, { interval: POLL.detail });
  const now = useNow();
  if (q.error && !q.data) return <><PageHead title="Live session" back="/live" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Live session" back="/live" sub="Loading…" /><SkeletonCards n={2} h={220} /></>;
  const d = q.data;
  const list = Array.isArray(d.spikes) ? d.spikes : d.spikes_list ?? [];
  const recording = d.status === "recording";
  const start = d.started_at ? Date.parse(d.started_at) : d.assigned_at ? Date.parse(d.assigned_at) : now;
  const end = d.ended_at ? Date.parse(d.ended_at) : now;
  const span = Math.max(end - start, 60_000);
  const pos = (iso: string) => (Date.parse(iso) - start) / span;
  const inside = (x: number) => x >= -0.01 && x <= 1.01;
  const marks = list.map((s) => ({
    at: pos(s.spike_wall ?? s.detected_at), tier: s.tier ?? "rejected", title: `Tier ${s.tier} · z ${s.score_z} · ${s.claude_reason ?? ""}`,
  })).filter((m) => inside(m.at));
  const ranges = (d.clipped_ranges ?? []).map((r) => ({ from: pos(r.start_wall), to: pos(r.end_wall), title: `clipped ${fmtTime(r.start_wall, true)}–${fmtTime(r.end_wall, true)}` }))
    .filter((r) => inside(r.from) && inside(r.to));
  const ticks = Array.from({ length: 6 }, (_, i) => fmtTime(new Date(start + (span * i) / 5).toISOString()));

  return (
    <>
      <PageHead back="/live"
        title={<span className="row" style={{ gap: 12 }}>{d.creator.handle}{recording && <span className="dotlive" aria-label="recording now" />}</span>}
        sub={<span className="row" style={{ gap: 8, flexWrap: "wrap" }}><Plat p={d.platform} />{d.platform === "twitch" ? "Twitch" : "YouTube"} live session · <Badge status={d.status} /> · {d.started_at ? fmtDuration((end - start) / 1000) : "not started"}</span>}
        actions={<>{d.url && <Btn href={d.url} external icon={<ArrowUpRight size={15} />}>Open stream</Btn>}<Btn kind="p" href={`/events?entity_type=live_session&entity_id=${d.id}`} icon={<List size={15} />}>Session events</Btn></>} />
      <TraceBar trace={d.trace} on="live_session" />

      <div className="g collapse-md" style={{ gridTemplateColumns: "1fr 340px" }}>
        <Card>
          <CardHead title="Session timeline" hint="Spikes by tier over the session · purple bars are clipped ranges" actions={<TierLegend />} />
          {marks.length ? <Timeline marks={marks} ranges={ranges} ticks={ticks} label={`Session timeline with ${marks.length} spikes`} height={150} />
            : <div className="muted small" style={{ padding: "40px 0", textAlign: "center" }}>No spikes detected yet. The first 15 minutes are a warm-up for the chat baseline.</div>}
          <div className="muted xs" style={{ marginTop: 10 }}>Clipped ranges are skipped by the replay pass so the same moment is never clipped twice.</div>
        </Card>
        <Card>
          <CardHead title="Session" />
          <KV rows={[
            ["Status", <Badge key="s" status={d.status} />],
            ["Assigned", <When key="a" iso={d.assigned_at} />],
            ["Stream started", <When key="b" iso={d.started_at} />],
            ...(d.ended_at ? [["Ended", <When key="e" iso={d.ended_at} />] as [string, React.ReactNode]] : []),
            ["Trend score", d.trend_score ?? "—"],
            ["Reconnects", <span key="r" className={maxReconnects !== null && d.reconnects >= maxReconnects ? "down" : undefined}>{d.reconnects}{maxReconnects !== null ? ` of ${maxReconnects}` : ""}</span>],
            ["Below cutoff", d.below_cutoff_since ? <When key="bc" iso={d.below_cutoff_since} mode="ago" /> : "no"],
            ["VOD id", d.vod_source_id ? <Mono key="v">{d.vod_source_id}</Mono> : "—"],
            ["Buffer", d.buffer_dir ? <Mono key="bd">{d.buffer_dir}</Mono> : "—"],
          ]} />
          <div className="flabel" style={{ marginTop: 14 }}>Learned bot names</div>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            {(d.bot_names ?? []).length ? d.bot_names!.map((b) => <Chip key={b}>{b}</Chip>) : <span className="muted small">none yet</span>}
          </div>
        </Card>
      </div>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1fr 400px" }}>
        <Card>
          <CardHead title="Spikes from this session" hint={`${list.length} detected`} actions={<Btn sm href="/spikes?mode=live">All spikes</Btn>} />
          <DataTable cols={spikeCols} rows={list} rowKey={(s) => s.id} href={(s) => `/spikes/${s.id}`} dense caption="Spikes from this session" empty="No spikes yet." />
        </Card>
        <Card>
          <CardHead title="Recorder log" hint="Latest events" />
          <EventFeed events={d.events.slice(0, 10)} />
        </Card>
      </div>
    </>
  );
}
