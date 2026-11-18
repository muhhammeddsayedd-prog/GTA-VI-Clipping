"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { AlertTriangle, Bolt, Clock, Compass, Film, Plus, Radio, SlidersHorizontal } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { DataTable, Kpi, type Col } from "@/components/kit/data";
import { Avatar, Badge, Btn, Card, CardHead, Empty, ErrorState, Grid, PageHead, Plat, RefreshBtn, Seg, SkeletonCards, TierChip, When, Who } from "@/components/kit/primitives";
import { useNow } from "@/components/domain/content/bits";
import { BlockBanners, readBlocks, type SourceBlock } from "@/components/domain/sources/BlockBanner";
import { useSettings } from "@/lib/use-settings";
import { POLL, useApi } from "@/lib/api/client";
import type { LiveSession, Page } from "@/lib/api/types";
import { fmtDuration, fmtTime, humanize } from "@/lib/format";

type LivePage = Page<LiveSession> & { meta?: { max_slots?: number; blocked?: Record<string, string>; source_health?: unknown } };
const ACTIVE = new Set(["recording", "assigned"]);

export function LiveView() {
  const q = useApi<LivePage>("admin/live-sessions", { limit: 200 }, { interval: POLL.status });
  const [filter, setFilter] = useState("all");
  const now = useNow();
  const st = useSettings();
  const head = (
    <PageHead title="Live sessions" sub="Streams being recorded right now · 60 s segments, 30 min rolling buffer"
      actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />
  );
  const items = useMemo(() => q.data?.items ?? [], [q.data]);
  if (q.error && !q.data) return <>{head}<ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <>{head}<SkeletonCards n={4} h={100} /><div className="mt2"><SkeletonCards n={3} h={200} /></div></>;

  const active = items.filter((s) => ACTIVE.has(s.status));
  const ended = items.filter((s) => !ACTIVE.has(s.status));
  const maxSlots = q.data.meta?.max_slots ?? Math.max(active.length, 6);
  // Prefer the whole health record (a platform stays refused between back-offs); older APIs send only `blocked`.
  const blocks: SourceBlock[] = q.data.meta?.source_health !== undefined
    ? readBlocks(q.data.meta.source_health)
    : Object.entries(q.data.meta?.blocked ?? {}).filter(([, until]) => until && Date.parse(until) > now)
      .map(([platform, until]) => ({ platform, since: null, error: null, access: null, blocks: null, lastBlockedAt: null, skipUntil: until }));
  const releaseMin = Math.round((st.num("discovery.release_after_below_s") ?? 1200) / 60);
  const maxReconnects = st.num("live.max_reconnects");
  const spikesToday = active.reduce((a, s) => a + s.spikes.S + s.spikes.A + s.spikes.B, 0);
  const tierS = active.reduce((a, s) => a + s.spikes.S, 0);
  const clipsLive = active.reduce((a, s) => a + s.clips, 0);
  const endedCounts: Record<string, number> = {};
  for (const s of ended) endedCounts[s.status] = (endedCounts[s.status] ?? 0) + 1;
  const shown = filter === "all" ? ended : ended.filter((s) => s.status === filter);

  const cols: Col<LiveSession>[] = [
    { key: "c", head: "Creator", render: (s) => <Who name={s.creator.handle} avatar={s.creator.avatar_url} p={s.platform} size={28} /> },
    { key: "st", head: "Status", render: (s) => <Badge status={s.status} /> },
    { key: "start", head: "Started", render: (s) => <When iso={s.started_at} /> },
    { key: "dur", head: "Duration", render: (s) => (s.started_at ? fmtDuration(((s.ended_at ? Date.parse(s.ended_at) : now) - Date.parse(s.started_at)) / 1000) : "—") },
    { key: "sp", head: "Spikes", align: "right", render: (s) => s.spikes.S + s.spikes.A + s.spikes.B },
    { key: "cl", head: "Clips", align: "right", render: (s) => s.clips },
    { key: "rc", head: "Reconnects", align: "right", render: (s) => <span className={maxReconnects !== null && s.reconnects >= maxReconnects ? "down semi" : undefined}>{s.reconnects}</span> },
  ];

  return (
    <>
      {head}
      <Grid cols={4}>
        <Kpi label="Slots in use" value={active.length} unit={`/ ${maxSlots}`} icon={<Radio size={15} />} delta={<span>live.max_slots = {maxSlots}</span>} />
        <Kpi label="Spikes · live sessions" value={spikesToday} icon={<Bolt size={15} />} delta={<span>{tierS} tier S in recording sessions</span>} />
        <Kpi label="Clips from live" value={clipsLive} icon={<Film size={15} />} delta={<span>S-tier goes express, A/B to the hourly queue</span>} />
        <Kpi label="Platform blocks" value={blocks.length} icon={<AlertTriangle size={15} />} iconTone={blocks.length ? "amber" : undefined}
          delta={blocks.length ? <span className="warn">{blocks.map((b) => `${humanize(b.platform)}${b.skipUntil && Date.parse(b.skipUntil) > now ? ` · next try ${fmtTime(b.skipUntil)}` : ""}`).join(" · ")}</span> : <span>no platform is blocked</span>} />
      </Grid>
      <div className="mt"><BlockBanners blocks={blocks} what="live recordings" /></div>

      {active.length === 0 ? (
        <Card style={{ marginTop: 24 }}>
          <Empty icon={<Radio size={34} />} title="No streams are being recorded"
            actions={<><Btn kind="p" href="/discovery" icon={<Compass size={15} />}>Last discovery run</Btn><Btn href="/settings?q=live." icon={<SlidersHorizontal size={15} />}>live.* settings</Btn></>}>
            Discovery assigns a slot when a live GTA stream scores above the live cutoff. The next pass runs within 10 minutes; VOD processing carries on meanwhile.
          </Empty>
        </Card>
      ) : (
        <AnimatedContent distance={20} duration={0.5} stagger={0.07} trigger="mount">
          <div className="g g3 mt2">
            {active.map((s) => <SlotCard key={s.id} s={s} now={now} releaseMin={releaseMin} />)}
            {Array.from({ length: Math.max(0, maxSlots - active.length) }).map((_, i) => (
              <div key={`free${i}`} className="card slot-free" role="note">
                <div>
                  <div className="cic" style={{ margin: "0 auto" }}><Plus size={15} /></div>
                  <div className="semi" style={{ marginTop: 10 }}>Free slot</div>
                  <div className="muted xs" style={{ marginTop: 4, maxWidth: 220 }}>Filled by the next discovery pass when a live stream scores above the cutoff</div>
                </div>
              </div>
            ))}
          </div>
        </AnimatedContent>
      )}

      <Card style={{ marginTop: 24 }}>
        <CardHead title="Ended sessions" hint="The replay pass scans each VOD after the stream ends"
          actions={<Seg label="Filter ended sessions" value={filter} onChange={setFilter}
            items={[{ value: "all", label: "All", count: ended.length }, ...Object.entries(endedCounts).map(([k, n]) => ({ value: k, label: humanize(k), count: n }))]} />} />
        <DataTable cols={cols} rows={shown} rowKey={(s) => s.id} href={(s) => `/live/${s.id}`} dense caption="Ended live sessions" empty="No ended sessions yet." />
      </Card>
    </>
  );
}

function SlotCard({ s, now, releaseMin }: { s: LiveSession; now: number; releaseMin: number }) {
  const up = s.started_at ? fmtDuration((now - Date.parse(s.started_at)) / 1000) : null;
  const below = s.below_cutoff_since ? Math.round((now - Date.parse(s.below_cutoff_since)) / 60000) : null;
  return (
    <Link href={`/live/${s.id}`} aria-label={`${s.creator.handle}: ${s.status}`}>
      <Card as="div" className="hover-lift">
        {s.thumbnail_url && <img className="live-prev" src={s.thumbnail_url} alt="" loading="lazy" decoding="async" referrerPolicy="no-referrer" />}
        <div className="row">
          <Avatar name={s.creator.handle} size={40} src={s.creator.avatar_url} />
          <div style={{ minWidth: 0 }}>
            <div className="semi row" style={{ gap: 6 }}><span className="ellipsis">{s.creator.handle}</span><Plat p={s.platform} /></div>
            <div className="muted xs">{up ? `stream up ${up}` : "stream start unknown"}{s.reconnects ? ` · ${s.reconnects} reconnects` : ""}</div>
          </div>
          <span className="right">{s.status === "recording" ? <Badge tone="green"><span className="sr-only">status </span>recording</Badge> : <Badge status={s.status} />}</span>
        </div>
        <div className="row" style={{ marginTop: 18, gap: 18, flexWrap: "wrap" }}>
          <div><div className="muted xs">Trend score</div><div className="semi num" style={{ fontSize: 22 }}>{s.trend_score ?? "—"}</div></div>
          <div>
            <div className="muted xs">Spikes</div>
            <div className="row" style={{ gap: 4, marginTop: 4 }}>
              <TierChip tier="S" /><b>{s.spikes.S}</b><TierChip tier="A" /><b>{s.spikes.A}</b><TierChip tier="B" /><b>{s.spikes.B}</b>
            </div>
          </div>
          <div className="right" style={{ textAlign: "right" }}><div className="muted xs">Clips</div><div className="semi num" style={{ fontSize: 22 }}>{s.clips}</div></div>
        </div>
        <div className="row muted xs" style={{ marginTop: 14, gap: 6 }}>
          <Clock size={13} />{s.status === "recording" ? <>recording since <When iso={s.assigned_at} /></> : <>assigned <When iso={s.assigned_at} mode="ago" /></>}
        </div>
        {below !== null && (
          <div className="banner amber" style={{ margin: "12px 0 0", padding: "9px 12px", fontSize: 11.5 }}>
            <Clock size={14} />Below the live cutoff for {below} min · released after {releaseMin} min
          </div>
        )}
      </Card>
    </Link>
  );
}
