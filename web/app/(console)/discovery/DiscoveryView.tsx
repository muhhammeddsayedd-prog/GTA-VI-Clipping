"use client";

import { useState } from "react";
import { Compass, SlidersHorizontal, X } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { HBars, Ring } from "@/components/kit/charts";
import { Pager } from "@/components/kit/data";
import { Drawer } from "@/components/kit/overlays";
import {
  Badge, Btn, Card, CardHead, ErrorState, Grid, KV, PageHead, Prog, RefreshBtn, Seg, SkeletonCards, SkeletonTable, Two, When, Who,
} from "@/components/kit/primitives";
import { SourceCard, type HealthRec } from "@/components/domain/sources/SourceCard";
import { POLL, useApi } from "@/lib/api/client";
import type { Candidate, Discovery, Event, Page } from "@/lib/api/types";
import { fmtAgo, fmtCompact, fmtNum, fmtTime, fmtUsd, humanize } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

const KEYS = ["source_type", "limit"];
const DEFAULTS = { limit: "12" };

export function DiscoveryView() {
  const d = useApi<Discovery>("admin/discovery", undefined, { interval: POLL.list });
  const list = useListParams(KEYS, DEFAULTS);
  const cands = useApi<Page<Candidate>>("admin/candidates", list.params, { interval: POLL.list });
  const [open, setOpen] = useState<Candidate | null>(null);

  if (d.error && !d.data) return <><PageHead title="Discovery" /><ErrorState error={d.error} retry={() => d.refetch()} /></>;
  const lastRun = d.data?.runs.find((r) => r.type === "discovery.run");

  return (
    <>
      <PageHead
        title="Discovery"
        sub={<>Finds trending streams and videos every 10 min · last pass <b className="ink2">{lastRun ? fmtAgo(lastRun.created_at) : "—"}</b></>}
        actions={<><Btn href="/settings?q=discovery." icon={<SlidersHorizontal size={15} />}>discovery.* settings</Btn><RefreshBtn onClick={() => { d.refetch(); cands.refetch(); }} spinning={d.isFetching} /></>}
      />

      {!d.data ? <SkeletonCards n={4} h={120} /> : <Sources d={d.data} />}

      <div className="g mt2" style={{ gridTemplateColumns: "minmax(0,1fr)" }}>
        <Card>
          <CardHead title="Recent passes" hint="discovery.run events · one every 10 minutes" actions={<Btn sm href="/events?type_prefix=discovery.">All</Btn>} />
          {!d.data ? <SkeletonTable rows={5} /> : <Runs runs={d.data.runs} />}
        </Card>
        <Card>
          <CardHead title="Candidates" hint="Trend score 0–100 · cutoff live 55 · VOD 50 · click a row for the breakdown"
            actions={<Seg label="Candidate type" value={list.get("source_type")} onChange={(v) => list.set("source_type", v)}
              items={[{ value: "", label: "All", count: cands.data?.counts?.all }, { value: "live", label: "Live", count: cands.data?.counts?.live }, { value: "vod", label: "VOD", count: cands.data?.counts?.vod }]} />} />
          {cands.error && !cands.data ? <ErrorState error={cands.error} retry={() => cands.refetch()} /> : !cands.data ? <SkeletonTable rows={6} /> : (
            <>
              <CandidateTable rows={cands.data.items} onOpen={setOpen} />
              <Pager shown={cands.data.items.length} total={list.get("source_type") ? cands.data.counts?.[list.get("source_type")] : cands.data.counts?.all}
                cursors={list.cursors} next={cands.data.next_cursor} onPage={list.setCursors} noun="candidates" />
            </>
          )}
        </Card>
      </div>

      <CandidateDrawer c={open} onClose={() => setOpen(null)} />
    </>
  );
}

function Sources({ d }: { d: Discovery }) {
  const q = d.quotas;
  const entries = Object.entries((d.source_health.discovery ?? {}) as Record<string, HealthRec>);
  const meter = (k: string) => {
    if (k === "data_api_search") return { pct: (q.search_calls_today / Math.max(q.search_calls_limit, 1)) * 100, label: `${q.search_calls_today} / ${q.search_calls_limit} calls today` };
    if (k === "apify") {
      const pct = (q.apify_today_usd / Math.max(q.apify_daily_budget_usd, 0.01)) * 100;
      return { pct, label: `${fmtUsd(q.apify_today_usd)} / ${fmtUsd(q.apify_daily_budget_usd)} today · month ${fmtUsd(q.apify_month_usd)} / ${fmtUsd(q.apify_monthly_budget_usd, 0)}`, tone: pct > 90 ? ("red" as const) : pct > 50 ? ("amber" as const) : undefined };
    }
    if (k === "youtube_rss") return { pct: (q.youtube_units_today / Math.max(q.youtube_units_limit, 1)) * 100, label: `YouTube API ${fmtNum(q.youtube_units_today)} / ${fmtNum(q.youtube_units_limit)} units` };
    return undefined;
  };
  if (!entries.length) return <Card><div className="muted small">No source health recorded yet. It appears after the first discovery pass.</div></Card>;
  return (
    <AnimatedContent distance={20} duration={0.5} stagger={0.06} trigger="mount">
      <Grid cols={entries.length >= 5 ? 5 : 4}>{entries.map(([k, h]) => <SourceCard key={k} k={k} h={h} meter={meter(k)} />)}</Grid>
    </AnimatedContent>
  );
}

function num(v: unknown): string { return typeof v === "number" ? fmtNum(v) : "—"; }

function Runs({ runs }: { runs: Event[] }) {
  if (!runs.length) return <div className="muted small" role="status">No discovery passes recorded yet.</div>;
  return (
    <div className="scroll-x">
      <table className="t dense">
        <caption className="sr-only">Recent discovery passes</caption>
        <thead><tr><th scope="col">Run</th><th scope="col">Duration</th><th scope="col" className="r">Live seen</th><th scope="col" className="r">Assigned</th><th scope="col" className="r">Released</th><th scope="col" className="r">Videos</th><th scope="col" className="r">New creators</th><th scope="col">Source</th><th scope="col">Result</th></tr></thead>
        <tbody>
          {runs.map((r) => {
            const x = (r.data ?? {}) as Record<string, unknown>;
            const failed = r.type === "discovery.failed";
            return (
              <tr key={r.id}>
                <td><When iso={r.created_at} /></td>
                <td>{typeof x.duration_s === "number" ? `${x.duration_s} s` : "—"}</td>
                <td className="r">{failed ? "—" : num(x.live_seen)}</td>
                <td className="r">{failed ? "—" : num(x.assigned)}</td>
                <td className="r">{failed ? "—" : num(x.released)}</td>
                <td className="r">{failed ? "—" : num(x.videos_queued)}</td>
                <td className="r">{failed ? "—" : num(x.new_creators)}</td>
                <td><Badge tone={x.source === "failover" ? "amber" : "gray"} dot={false}>{x.source === "failover" ? "failover" : "n8n WF01"}</Badge></td>
                <td>{failed ? <span title={r.message ?? undefined}><Badge status="failed" /></span> : <Badge status="ok" />}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CandidateTable({ rows, onOpen }: { rows: Candidate[]; onOpen: (c: Candidate) => void }) {
  if (!rows.length) return <div className="muted small" role="status" style={{ padding: "26px 4px", textAlign: "center" }}>No candidates in this view.</div>;
  return (
    <div className="scroll-x">
      <table className="t dense">
        <caption className="sr-only">Candidates from the latest passes</caption>
        <thead><tr><th scope="col">Creator</th><th scope="col">Type</th><th scope="col">Title</th><th scope="col">Trend</th><th scope="col">Result</th></tr></thead>
        <tbody>
          {rows.map((c) => {
            const cutoff = c.source_type === "live" ? 55 : 50;
            const metric = c.source_type === "live" ? `${fmtCompact(c.viewers)} viewers` : `${fmtCompact(c.views)} views · ${fmtCompact(c.views_per_hour)}/h`;
            return (
              <tr key={c.id} className="link" onClick={() => onOpen(c)}>
                <td>
                  <button type="button" onClick={(e) => { e.stopPropagation(); onOpen(c); }} aria-label={`Score breakdown for ${c.creator.handle}`} style={{ all: "unset", cursor: "pointer" }}>
                    <Who name={c.creator.handle} p={c.platform} size={28} />
                  </button>
                </td>
                <td><Badge tone={c.source_type === "live" ? "purple" : "blue"}>{c.source_type === "live" ? "live" : "VOD"}</Badge></td>
                <td style={{ maxWidth: 240 }}><Two a={<span className="ellipsis" style={{ display: "block" }}>{c.title ?? "—"}</span>} b={metric} /></td>
                <td>
                  <div className="row" style={{ gap: 10 }}>
                    <b className="num" style={{ width: 22 }}>{Math.round(c.trend_score)}</b>
                    <Prog pct={c.trend_score} tone={c.trend_score >= cutoff ? undefined : "amber"} thin width={100} label={`Trend score ${Math.round(c.trend_score)} of 100`} />
                  </div>
                </td>
                <td>{c.picked ? <Badge status="picked" tone="green" />
                  : c.trend_score >= cutoff ? <Badge tone={c.creator.tier === "blocked" ? "red" : "amber"}>skipped · {c.creator.tier}</Badge>
                  : <Badge tone="gray">below cutoff</Badge>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const PART_WEIGHTS: Record<string, number> = { viewers: 0.3, growth: 0.2, chat_rate: 0.2, global_reach: 0.15, history: 0.15, views_per_hour: 0.4, engagement: 0.25, size_ratio: 0.15 };

function CandidateDrawer({ c, onClose }: { c: Candidate | null; onClose: () => void }) {
  const parts = Object.entries(c?.score_parts ?? {});
  return (
    <Drawer open={!!c} onClose={onClose} title="Candidate" sub={c ? `${c.source_type === "live" ? "Live observation" : "Video"} · checked ${fmtTime(c.checked_at)} UTC` : undefined} width={520}>
      {c && (
        <>
          <div className="row">
            <Who name={c.creator.handle} p={c.platform} size={52} sub={c.title ? `“${c.title}”` : undefined} />
            <div className="right"><Ring pct={c.trend_score} size={74} text={String(Math.round(c.trend_score))} fs={18} label={`Trend score ${Math.round(c.trend_score)}`} /></div>
          </div>
          <div className="flabel mt2">Score parts</div>
          {parts.length ? (
            <HBars labelWidth={150} items={parts.map(([k, v]) => ({ label: <>{humanize(k)}{PART_WEIGHTS[k] ? <div className="muted xs">w {PART_WEIGHTS[k]}</div> : null}</>, value: v }))} />
          ) : <div className="muted small">No breakdown stored for this observation.</div>}
          <div className="flabel mt">Bonuses and penalties</div>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            {c.creator.tier === "preferred" && <span className="chip p">preferred +15</span>}
            <span className="chip">{c.language === "en" || !c.language ? "English ×1.00" : `${c.language} ×0.85`}</span>
            {(c.creator.tier === "restricted" || c.creator.tier === "blocked") && <span className="chip" style={{ color: "var(--red)" }}>creator {c.creator.tier}: never clipped</span>}
          </div>
          <div className="flabel mt2">Raw metrics</div>
          <KV rows={[
            ...(c.source_type === "live"
              ? [["Viewers", fmtNum(c.viewers)], ["Started", c.started_at ? `${fmtTime(c.started_at)} UTC` : "—"]] as [string, string][]
              : [["Views", fmtNum(c.views)], ["Views per hour", fmtNum(c.views_per_hour)], ["Likes", fmtNum(c.likes)], ["Comments", fmtNum(c.comments)], ["Published", c.published_at ? fmtAgo(c.published_at) : "—"]] as [string, string][]),
            ["Language", c.language ?? "—"],
            ["Tier", <Badge key="t" status={c.creator.tier} />],
            ["Result", c.picked ? <Badge key="r" tone="green">picked</Badge> : <Badge key="r" tone="gray">below cutoff ({c.source_type === "live" ? 55 : 50})</Badge>],
          ]} />
          <div className="row mt2" style={{ flexWrap: "wrap" }}>
            <Btn href={`/creators/${c.creator.id}`} icon={<Compass size={15} />}>Open creator</Btn>
            {c.url && <Btn kind="p" href={c.url} external>Open source</Btn>}
            <Btn kind="ghost" onClick={onClose} icon={<X size={15} />}>Close</Btn>
          </div>
        </>
      )}
    </Drawer>
  );
}
