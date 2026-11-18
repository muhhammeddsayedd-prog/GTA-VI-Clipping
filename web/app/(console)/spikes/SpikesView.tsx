"use client";

import { Bolt, Check, Layers, Star } from "lucide-react";

import { DataTable, Kpi, Pager, type Col } from "@/components/kit/data";
import { Badge, Card, ErrorState, Grid, PageHead, RefreshBtn, Seg, SelectField, SkeletonTable, TierChip, Who } from "@/components/kit/primitives";
import { Legend } from "@/components/kit/charts";
import { ClipLink, ModeBadge, PartsMini, spikeAt } from "@/components/domain/content/bits";
import { POLL, useApi } from "@/lib/api/client";
import type { Page, Spike } from "@/lib/api/types";
import { humanize } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

const KEYS = ["status", "tier", "mode"];
const STATUSES = ["detected", "confirmed", "rendering", "clipped", "rejected", "merged", "failed"];

const cols: Col<Spike>[] = [
  { key: "t", head: "Tier", mobile: "lead", render: (s) => <TierChip tier={s.tier} /> },
  { key: "c", head: "Creator", render: (s) => <Who name={s.creator.handle} p={s.creator.platform} size={28} sub={<ModeBadge mode={s.mode} />} /> },
  { key: "at", head: "At", mobile: "meta", render: (s) => <span className="num">{spikeAt(s)}</span> },
  { key: "z", head: "z", align: "right", mobile: "meta", render: (s) => <b className="num">{s.score_z ?? "—"}</b> },
  { key: "p", head: "Parts", mobile: "hide", render: (s) => <PartsMini parts={s.parts as Record<string, number> | null} /> },
  { key: "cf", head: "Conf.", align: "right", mobile: "meta", render: (s) => (s.claude_confidence !== null ? s.claude_confidence.toFixed(2) : "—") },
  { key: "m", head: "Moment", mobile: "meta", render: (s) => <span className="ink2 clamp2" style={{ maxWidth: 260 }}>{s.claude_reason ?? "—"}</span> },
  { key: "st", head: "Status", render: (s) => <Badge status={s.status} /> },
  { key: "cl", head: "Clip", mobile: "hide", render: (s) => <ClipLink id={s.clip_id} no={s.clip_series_no} /> },
  { key: "sp", head: "Spike→post", align: "right", mobile: "hide", render: (s) => (s.spike_to_post_s !== null ? `${s.spike_to_post_s} s` : "—") },
];

export function SpikesView() {
  const lp = useListParams(KEYS);
  const q = useApi<Page<Spike>>("admin/spikes", lp.params, { interval: POLL.list });
  const c = q.data?.counts ?? {};
  const total = c.all ?? null;

  return (
    <>
      <PageHead title="Spikes" sub="Every chat spike detected · z-score against a 10 min baseline, confirmed by Claude"
        actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />
      <Grid cols={4}>
        <Kpi label="Detected" value={total} icon={<Bolt size={15} />} delta={<span>matching the tier and mode filters</span>} />
        <Kpi label="Confirmed or clipped" value={q.data ? (c.confirmed ?? 0) + (c.clipped ?? 0) + (c.rendering ?? 0) : null} icon={<Check size={15} />} iconTone="green"
          delta={<span>passed Claude P1</span>} />
        <Kpi label="Clipped" value={q.data ? c.clipped ?? 0 : null} icon={<Star size={15} />} delta={<span>turned into a clip</span>} />
        <Kpi label="Merged" value={q.data ? c.merged ?? 0 : null} icon={<Layers size={15} />} delta={<span>folded into a stronger spike within 60 s</span>} />
      </Grid>
      <div className="filters mt2">
        <span className="lab">Filters <span className="cnt">{KEYS.filter((k) => lp.get(k)).length}</span></span>
        <SelectField label="Tier" value={lp.get("tier")} onChange={(v) => lp.set("tier", v)} width={140}
          options={[{ value: "", label: "All tiers" }, { value: "S", label: "Tier S" }, { value: "A", label: "Tier A" }, { value: "B", label: "Tier B" }, { value: "rejected", label: "Rejected" }]} />
        <SelectField label="Mode" value={lp.get("mode")} onChange={(v) => lp.set("mode", v)} width={150}
          options={[{ value: "", label: "All modes" }, { value: "live", label: "Live" }, { value: "replay", label: "Replay" }, { value: "transcript", label: "Transcript" }]} />
        <span className="right">
          <Seg label="Spike status" value={lp.get("status")} onChange={(v) => lp.set("status", v)}
            items={[{ value: "", label: "All", count: total }, ...STATUSES.filter((s) => c[s]).map((s) => ({ value: s, label: humanize(s).replace(/^./, (x) => x.toUpperCase()), count: c[s] }))]} />
        </span>
      </div>
      {q.error && !q.data ? <ErrorState error={q.error} retry={() => q.refetch()} /> : !q.data ? <SkeletonTable /> : (
        <Card>
          <DataTable cols={cols} rows={q.data.items} rowKey={(s) => s.id} href={(s) => `/spikes/${s.id}`} caption="Detected spikes" empty="No spikes match these filters." />
          <div className="mt">
            <Legend items={[{ color: "#C9C2FF", label: "messages" }, { color: "#9F94FF", label: "unique chatters" }, { color: "#5A4AF4", label: "reactions" }, { color: "#3A2CC7", label: "clip requests" }]} />
          </div>
          <Pager shown={q.data.items.length} total={lp.get("status") ? c[lp.get("status")] : total ?? undefined} cursors={lp.cursors} next={q.data.next_cursor} onPage={lp.setCursors} noun="spikes" />
        </Card>
      )}
    </>
  );
}
