"use client";

import { LayoutGrid, List } from "lucide-react";

import FadeContent from "@/components/reactbits/FadeContent";
import { DataTable, Pager, Thumb, type Col } from "@/components/kit/data";
import { Badge, Card, ErrorState, IconBtn, PageHead, RefreshBtn, SearchField, Seg, SelectField, SkeletonTable, Two, When, Who } from "@/components/kit/primitives";
import { ClipCard, laneTone } from "@/components/domain/clips/ClipBits";
import { useApi } from "@/lib/api/client";
import type { Clip, Page } from "@/lib/api/types";
import { fmtClock, humanize, series } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

const KEYS = ["status", "lane", "q", "creator_id"];
const DEFAULTS = {};
const STATUSES = ["rendering", "gating", "precheck_pending", "approved", "posted", "rejected", "expired", "removed"];

const COLS: Col<Clip>[] = [
  { key: "clip", head: "Clip", render: (c) => <div className="row"><div style={{ width: 34, flex: "none" }}><Thumb seed={c.id} /></div><span className="semi pc">{series(c.series_no)}</span></div> },
  { key: "hook", head: "Hook / topic", render: (c) => <Two a={<span className="ellipsis" style={{ maxWidth: 320, display: "block" }}>{c.hook ?? c.title ?? "—"}</span>} b={c.topic ? <span className="mono">{c.topic}</span> : "—"} /> },
  { key: "creator", head: "Creator", render: (c) => <Who name={c.creator.handle} p={c.source_platform} size={26} /> },
  { key: "lane", head: "Lane", render: (c) => <Badge tone={laneTone(c.lane)}>{c.lane}</Badge> },
  { key: "status", head: "Status", render: (c) => <Badge status={c.status} /> },
  { key: "score", head: "Score", align: "right", render: (c) => <b className="num">{c.score ?? "—"}</b> },
  { key: "len", head: "Length", align: "right", render: (c) => <span className="num">{fmtClock(c.duration_s)}</span> },
  { key: "reason", head: "Reject reason", render: (c) => (c.reject_reason ? <span className="down semi">{humanize(c.reject_reason)}</span> : <span className="muted">—</span>) },
  { key: "created", head: "Created", render: (c) => <When iso={c.created_at} /> },
];

export function ClipsView({ view }: { view: "grid" | "table" }) {
  const lp = useListParams(KEYS, DEFAULTS);
  const q = useApi<Page<Clip>>("admin/clips", { ...lp.params, limit: view === "grid" ? 24 : 50 });
  const counts = q.data?.counts ?? {};
  const setView = (v: "grid" | "table") => lp.set("view", v === "grid" ? "" : v);

  return (
    <>
      <PageHead title="Clips" sub="Every rendered clip · 1080×1920, subtitles burned in, approved or rejected by the quality gate"
        actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />
      <div className="filters" style={{ flexWrap: "wrap" }}>
        <SelectField label="Lane" value={lp.get("lane")} onChange={(v) => lp.set("lane", v)} width={150}
          options={[{ value: "", label: "All lanes" }, { value: "hourly", label: "Hourly lane" }, { value: "spike", label: "Spike lane" }]} />
        <SearchField label="Search clips" value={lp.get("q")} onChange={(v) => lp.set("q", v)} placeholder="Search hook, topic, #series" />
        {lp.get("creator_id") && <button type="button" className="chip p" onClick={() => lp.set("creator_id", "")}>One creator ✕</button>}
        <span className="right row" style={{ gap: 6 }} role="group" aria-label="View">
          <IconBtn label="Grid view" sm dark={view === "grid"} aria-pressed={view === "grid"} onClick={() => setView("grid")}><LayoutGrid size={15} /></IconBtn>
          <IconBtn label="Table view" sm dark={view === "table"} aria-pressed={view === "table"} onClick={() => setView("table")}><List size={15} /></IconBtn>
        </span>
      </div>
      <div className="mb">
        <Seg label="Clip status" value={lp.get("status")} onChange={(v) => lp.set("status", v)}
          items={[{ value: "", label: "All", count: counts.all ?? null }, ...STATUSES.filter((s) => counts[s] || s === lp.get("status")).map((s) => ({ value: s, label: humanize(s).replace(/^\w/, (x) => x.toUpperCase()), count: counts[s] ?? 0 }))]} />
      </div>

      {q.error && !q.data ? <ErrorState error={q.error} retry={() => q.refetch()} />
        : !q.data ? (view === "grid" ? <div className="g g6" role="status" aria-busy="true">{Array.from({ length: 12 }).map((_, i) => <div key={i} className="clipcard"><div className="skel" style={{ aspectRatio: "9/16", borderRadius: 16 }} /></div>)}</div> : <SkeletonTable />)
        : view === "grid" ? (
          <>
            {q.data.items.length ? (
              <FadeContent blur duration={500} trigger="mount" stagger={0.03}>
                <div className="g g6">{q.data.items.map((c) => <ClipCard key={c.id} c={c} />)}</div>
              </FadeContent>
            ) : <Card><div className="muted small" style={{ textAlign: "center", padding: 30 }}>No clips match these filters.</div></Card>}
            <Pager shown={q.data.items.length} total={lp.get("status") ? counts[lp.get("status")] : counts.all} cursors={lp.cursors} next={q.data.next_cursor} onPage={lp.setCursors} noun="clips" />
          </>
        ) : (
          <Card>
            <DataTable caption="Clips" cols={COLS} rows={q.data.items} rowKey={(c) => c.id} href={(c) => `/clips/${c.id}`} empty="No clips match these filters." />
            <Pager shown={q.data.items.length} total={lp.get("status") ? counts[lp.get("status")] : counts.all} cursors={lp.cursors} next={q.data.next_cursor} onPage={lp.setCursors} noun="clips" />
          </Card>
        )}
    </>
  );
}
