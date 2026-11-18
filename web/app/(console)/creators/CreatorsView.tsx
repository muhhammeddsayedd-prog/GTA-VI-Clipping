"use client";

import { useEffect, useState } from "react";
import { Lock, Shield, Star, Users } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { DataTable, Kpi, Pager, type Col } from "@/components/kit/data";
import { Badge, Card, ErrorState, Grid, PageHead, RefreshBtn, SearchField, Seg, SelectField, SkeletonTable, Who, When } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Creator, Page } from "@/lib/api/types";
import { fmtCompact } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

const KEYS = ["tier", "platform", "q"];
const DEFAULTS = {};

const COLS: Col<Creator>[] = [
  { key: "who", head: "Creator", render: (c) => <Who name={c.handle ?? c.display_name ?? c.channel_id} avatar={c.avatar_url} p={c.platform} size={32} sub={c.language ? c.language.toUpperCase() : undefined} /> },
  { key: "tier", head: "Tier", mobile: "aside", render: (c) => <Badge status={c.tier} /> },
  { key: "reason", head: "Reason / evidence", render: (c) => <span className="ink2 clamp2" style={{ maxWidth: 340 }}>{c.tier_reason ?? "—"}</span> },
  { key: "fol", head: "Followers", align: "right", render: (c) => <span className="num">{fmtCompact(c.follower_count)}</span> },
  { key: "avg", head: "Avg clip views", align: "right", mobile: "hide", render: (c) => <span className="num">{fmtCompact(c.avg_clip_views)}</span> },
  { key: "clips", head: "Clips", align: "right", render: (c) => <span className="num">{c.clips_posted}</span> },
  { key: "claims", head: "Claims", align: "right", render: (c) => <b className={c.claims_count ? "down" : undefined}>{c.claims_count}</b> },
  { key: "checked", head: "Checked", render: (c) => <When iso={c.description_checked_at} mode="ago" /> },
];

export function CreatorsView() {
  const list = useListParams(KEYS, DEFAULTS);
  const [q, setQ] = useState(list.get("q"));
  useEffect(() => { const t = setTimeout(() => { if (q !== list.get("q")) list.set("q", q); }, 300); return () => clearTimeout(t); }, [q, list]);
  const res = useApi<Page<Creator>>("admin/creators", list.params, { interval: POLL.slow });
  const c = res.data?.counts ?? {};
  const tier = list.get("tier");

  return (
    <>
      <PageHead title="Creators" sub="Every streamer discovery has seen · the permission tier decides what can be clipped"
        actions={<RefreshBtn onClick={() => res.refetch()} spinning={res.isFetching} />} />
      <AnimatedContent distance={20} duration={0.5} stagger={0.06} trigger="mount">
        <Grid cols={4} className="mb">
          <Kpi label="Preferred" value={c.preferred ?? null} icon={<Star size={15} />} iconTone="green" delta={<span>clipping explicitly allowed</span>} />
          <Kpi label="Auto" value={c.auto ?? null} icon={<Users size={15} />} delta={<span>no statement · capped posts per day each</span>} />
          <Kpi label="Restricted" value={c.restricted ?? null} icon={<Lock size={15} />} iconTone="amber" delta={<span>forbid clips · never used</span>} />
          <Kpi label="Blocked" value={c.blocked ?? null} icon={<Shield size={15} />} iconTone="red" delta={<span>after a claim or takedown</span>} />
        </Grid>
      </AnimatedContent>
      <div className="filters">
        <span className="lab">Filters <span className="cnt">{[list.get("platform"), list.get("q"), tier].filter(Boolean).length}</span></span>
        <SelectField label="Platform" value={list.get("platform")} onChange={(v) => list.set("platform", v)} width={160}
          options={[{ value: "", label: "All platforms" }, { value: "twitch", label: "Twitch" }, { value: "youtube", label: "YouTube" }]} />
        <SearchField label="Search handle" value={q} onChange={setQ} placeholder="Search handle" width={220} />
        <span className="right">
          <Seg label="Tier" value={tier} onChange={(v) => list.set("tier", v)} items={[
            { value: "", label: "All", count: c.all }, { value: "preferred", label: "Preferred", count: c.preferred ?? 0 }, { value: "auto", label: "Auto", count: c.auto ?? 0 },
            { value: "restricted", label: "Restricted", count: c.restricted ?? 0 }, { value: "blocked", label: "Blocked", count: c.blocked ?? 0 }]} />
        </span>
      </div>
      {res.error && !res.data ? <ErrorState error={res.error} retry={() => res.refetch()} /> : !res.data ? <SkeletonTable /> : (
        <Card>
          <DataTable cols={COLS} rows={res.data.items} rowKey={(r) => r.id} href={(r) => `/creators/${r.id}`} caption="Creators"
            empty={list.get("q") ? `No creator handle contains “${list.get("q")}”.` : "No creators in this tier."} />
          <Pager shown={res.data.items.length} total={tier ? c[tier] : c.all} cursors={list.cursors} next={res.data.next_cursor} onPage={list.setCursors} noun="creators" />
        </Card>
      )}
    </>
  );
}
