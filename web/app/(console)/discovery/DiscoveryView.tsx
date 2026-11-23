"use client";

import { useState } from "react";
import { Compass, SlidersHorizontal, X } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { HBars, Ring } from "@/components/kit/charts";
import { DataTable, Pager, type Col } from "@/components/kit/data";
import { Drawer } from "@/components/kit/overlays";
import {
  Badge, Btn, Card, CardHead, ErrorState, Grid, KV, PageHead, Prog, RefreshBtn, Seg, SkeletonCards, SkeletonTable, Two, When, Who,
} from "@/components/kit/primitives";
import { BlockBanners, readBlocks } from "@/components/domain/sources/BlockBanner";
import { SourceCard } from "@/components/domain/sources/SourceCard";
import { POLL, useApi } from "@/lib/api/client";
import type { Candidate, Discovery, Event, Page } from "@/lib/api/types";
import { fmtAgo, fmtCompact, fmtNum, fmtTime, fmtUsd, humanize, zoneLabel } from "@/lib/format";
import { isRecord, num as numOf, numericEntries, str } from "@/lib/shape";
import { useListParams } from "@/lib/use-list";
import { useSettings } from "@/lib/use-settings";

const KEYS = ["source_type", "limit"];
const DEFAULTS = { limit: "12" };

export function DiscoveryView() {
  const d = useApi<Discovery>("admin/discovery", undefined, { interval: POLL.list });
  const list = useListParams(KEYS, DEFAULTS);
  const cands = useApi<Page<Candidate>>("admin/candidates", list.params, { interval: POLL.list });
  const [open, setOpen] = useState<Candidate | null>(null);
  const st = useSettings();

  if (d.error && !d.data) return <><PageHead title="Discovery" /><ErrorState error={d.error} retry={() => d.refetch()} /></>;
  const lastRun = d.data?.runs.find((r) => r.type === "discovery.run");
  // The VOD cutoff can be widened for a pass (vod_widened); the last run records the one it used.
  const cut: Cutoffs = {
    live: st.num("discovery.live_score_cutoff"),
    vod: numOf(lastRun?.data?.vod_cutoff) ?? st.num("discovery.vod_score_cutoff"),
  };
  const weights = { live: st.get("discovery.weights_live"), vod: st.get("discovery.weights_vod") };

  return (
    <>
      <PageHead
        title="Discovery"
        sub={<>Finds trending streams and videos every 10 min · last pass <b className="ink2">{lastRun ? fmtAgo(lastRun.created_at) : "—"}</b></>}
        actions={<><Btn href="/settings?q=discovery." icon={<SlidersHorizontal size={15} />}>discovery.* settings</Btn><RefreshBtn onClick={() => { d.refetch(); cands.refetch(); }} spinning={d.isFetching} /></>}
      />

      {!d.data ? <SkeletonCards n={4} h={120} /> : <Sources d={d.data} lastRun={lastRun} />}

      <div className="g mt2" style={{ gridTemplateColumns: "minmax(0,1fr)" }}>
        <Card>
          <CardHead title="Recent passes" hint="discovery.run events · one every 10 minutes" actions={<Btn sm href="/events?type_prefix=discovery.">All</Btn>} />
          {!d.data ? <SkeletonTable rows={5} /> : <Runs runs={d.data.runs} />}
        </Card>
        <Card>
          <CardHead title="Candidates" hint={`Trend score 0–100 · cutoff live ${cut.live ?? "—"} · VOD ${cut.vod ?? "—"} · click a row for the breakdown`}
            actions={<Seg label="Candidate type" value={list.get("source_type")} onChange={(v) => list.set("source_type", v)}
              items={[{ value: "", label: "All", count: cands.data?.counts?.all }, { value: "live", label: "Live", count: cands.data?.counts?.live }, { value: "vod", label: "VOD", count: cands.data?.counts?.vod }]} />} />
          {cands.error && !cands.data ? <ErrorState error={cands.error} retry={() => cands.refetch()} /> : !cands.data ? <SkeletonTable rows={6} /> : (
            <>
              <CandidateTable rows={cands.data.items} onOpen={setOpen} cut={cut} />
              <Pager shown={cands.data.items.length} total={list.get("source_type") ? cands.data.counts?.[list.get("source_type")] : cands.data.counts?.all}
                cursors={list.cursors} next={cands.data.next_cursor} onPage={list.setCursors} noun="candidates" />
            </>
          )}
        </Card>
      </div>

      <CandidateDrawer c={open} onClose={() => setOpen(null)} cut={cut} weights={weights} />
    </>
  );
}

type Cutoffs = { live: number | null; vod: number | null };

/** One card per source from the last pass (`data.sources[]`), with the quota meter that belongs to it. */
function Sources({ d, lastRun }: { d: Discovery; lastRun: Event | undefined }) {
  const q = d.quotas;
  const data = lastRun?.data ?? {};
  const sources = (Array.isArray(data.sources) ? data.sources : []).filter(isRecord);
  const notes = (Array.isArray(data.notes) ? data.notes : []).filter((n): n is string => typeof n === "string");
  const vodBlocks = readBlocks(d.source_health.vod);
  const liveBlocks = readBlocks(d.source_health.live);
  const meter = (k: string) => {
    if (k === "youtube_search_api") return { pct: (q.search_calls_today / Math.max(q.search_calls_limit, 1)) * 100, label: `${q.search_calls_today} / ${q.search_calls_limit} calls today (resets 00:00 Pacific)` };
    if (k === "apify") {
      const pct = (q.apify_today_usd / Math.max(q.apify_daily_budget_usd, 0.01)) * 100;
      return { pct, label: `${fmtUsd(q.apify_today_usd)} / ${fmtUsd(q.apify_daily_budget_usd)} today · month ${fmtUsd(q.apify_month_usd)} / ${fmtUsd(q.apify_monthly_budget_usd, 0)}`, tone: pct > 90 ? ("red" as const) : pct > 50 ? ("amber" as const) : undefined };
    }
    if (k === "youtube_api") return { pct: (q.youtube_units_today / Math.max(q.youtube_units_limit, 1)) * 100, label: `${fmtNum(q.youtube_units_today)} / ${fmtNum(q.youtube_units_limit)} quota units today (resets 00:00 Pacific)` };
    return undefined;
  };
  return (
    <>
      <BlockBanners blocks={vodBlocks} what="downloads" />
      <BlockBanners blocks={liveBlocks} what="live recordings" />
      {!sources.length ? (
        <Card><div className="muted small">No source report yet. It appears after the first discovery pass.</div></Card>
      ) : (
        <AnimatedContent distance={20} duration={0.5} stagger={0.06} trigger="mount">
          <Grid cols={4}>
            {sources.map((x) => {
              const k = str(x.name) ?? "source";
              return <SourceCard key={k} k={k} h={{ status: str(x.status) ?? "ok", found: numOf(x.found), detail: str(x.detail) }} meter={meter(k)} />;
            })}
          </Grid>
        </AnimatedContent>
      )}
      {notes.length > 0 && <div className="muted xs mt">{notes.map((n) => <div key={n}>· {n}</div>)}</div>}
    </>
  );
}

function num(v: unknown): string { return typeof v === "number" ? fmtNum(v) : "—"; }

const RUN_COLS: Col<Event>[] = [
  { key: "run", head: "Run", render: (r) => <span className="semi"><When iso={r.created_at} /></span> },
  { key: "dur", head: "Duration", mobile: "hide", render: (r) => { const x = (r.data ?? {}) as Record<string, unknown>; return typeof x.duration_s === "number" ? `${x.duration_s} s` : "—"; } },
  { key: "live", head: "Live seen", align: "right", render: (r) => runNum(r, "live_seen") },
  { key: "asg", head: "Assigned", align: "right", render: (r) => runNum(r, "sessions_assigned") },
  { key: "rel", head: "Released", align: "right", mobile: "hide", render: (r) => runNum(r, "sessions_released") },
  { key: "vid", head: "Videos", align: "right", render: (r) => runNum(r, "videos_queued") },
  { key: "tc", head: "Twitch clips", align: "right", mobile: "hide", render: (r) => { const tc = r.data?.twitch_clips; return r.type === "discovery.failed" ? "—" : num(isRecord(tc) ? tc.queued : undefined); } },
  { key: "new", head: "New creators", align: "right", render: (r) => runNum(r, "new_creators") },
  { key: "src", head: "Source", mobile: "hide", render: (r) => { const x = (r.data ?? {}) as Record<string, unknown>; return <Badge tone={x.source === "failover" ? "amber" : "gray"} dot={false}>{x.source === "failover" ? "failover" : "n8n WF01"}</Badge>; } },
  { key: "result", head: "Result", render: (r) => r.type === "discovery.failed" ? <span title={r.message ?? undefined}><Badge status="failed" /></span> : <Badge status="ok" /> },
];
function runNum(r: Event, k: string): string { return r.type === "discovery.failed" ? "—" : num(((r.data ?? {}) as Record<string, unknown>)[k]); }

function Runs({ runs }: { runs: Event[] }) {
  return <DataTable dense caption="Recent discovery passes" cols={RUN_COLS} rows={runs} rowKey={(r) => r.id} empty="No discovery passes recorded yet." />;
}

function cutoffOf(c: Candidate, cut: Cutoffs): number | null { return c.source_type === "live" ? cut.live : cut.vod; }

function candidateCols(cut: Cutoffs): Col<Candidate>[] {
  return [
    { key: "who", head: "Creator", render: (c) => <Who name={c.creator.handle} avatar={c.creator.avatar_url} p={c.platform} size={28} /> },
    { key: "type", head: "Type", render: (c) => <Badge tone={c.source_type === "live" ? "purple" : "blue"}>{c.source_type === "live" ? "live" : "VOD"}</Badge> },
    {
      key: "title", head: "Title", render: (c) => {
        const metric = c.source_type === "live" ? `${fmtCompact(c.viewers)} viewers` : `${fmtCompact(c.views)} views · ${fmtCompact(c.views_per_hour)}/h`;
        return <div style={{ maxWidth: 240 }}><Two a={<span className="ellipsis" style={{ display: "block" }}>{c.title ?? "—"}</span>} b={metric} /></div>;
      },
    },
    {
      key: "trend", head: "Trend", render: (c) => {
        const cutoff = cutoffOf(c, cut);
        return (
          <div className="row" style={{ gap: 10 }}>
            <b className="num" style={{ width: 22 }}>{Math.round(c.trend_score)}</b>
            <Prog pct={c.trend_score} tone={cutoff === null || c.trend_score >= cutoff ? undefined : "amber"} thin width={80} label={`Trend score ${Math.round(c.trend_score)} of 100`} />
          </div>
        );
      },
    },
    {
      key: "result", head: "Result", render: (c) => {
        const cutoff = cutoffOf(c, cut);
        return c.picked ? <Badge status="picked" tone="green" />
          : cutoff === null || c.trend_score >= cutoff ? <Badge tone={c.creator.tier === "blocked" ? "red" : "amber"}>skipped · {c.creator.tier}</Badge>
          : <Badge tone="gray">below cutoff</Badge>;
      },
    },
  ];
}

function CandidateTable({ rows, onOpen, cut }: { rows: Candidate[]; onOpen: (c: Candidate) => void; cut: Cutoffs }) {
  return <DataTable dense caption="Candidates from the latest passes (tap for the score breakdown)" cols={candidateCols(cut)} rows={rows} rowKey={(c) => c.id} onRow={onOpen} empty="No candidates in this view." />;
}

function CandidateDrawer({ c, onClose, cut, weights }: { c: Candidate | null; onClose: () => void; cut: Cutoffs; weights: { live: unknown; vod: unknown } }) {
  // Weighted parts are 0–1; the weights come from discovery.weights_live / weights_vod. Other numbers are raw metrics.
  const w = Object.fromEntries(numericEntries(c?.source_type === "live" ? weights.live : weights.vod));
  const all = numericEntries(c?.score_parts);
  const parts = all.filter(([k]) => k in w);
  const extra = all.filter(([k]) => !(k in w));
  const cutoff = c ? cutoffOf(c, cut) : null;
  return (
    <Drawer open={!!c} onClose={onClose} title="Candidate" sub={c ? `${c.source_type === "live" ? "Live observation" : "Video"} · checked ${fmtTime(c.checked_at)} ${zoneLabel()}` : undefined} width={520}>
      {c && (
        <>
          <div className="row">
            <Who name={c.creator.handle} avatar={c.creator.avatar_url} p={c.platform} size={52} sub={c.title ? `“${c.title}”` : undefined} />
            <div className="right"><Ring pct={c.trend_score} size={74} text={String(Math.round(c.trend_score))} fs={18} label={`Trend score ${Math.round(c.trend_score)}`} /></div>
          </div>
          <div className="flabel mt2">Score parts</div>
          {parts.length ? (
            <HBars labelWidth={150} items={parts.map(([k, v]) => ({ label: <>{humanize(k)}<div className="muted xs">weight {w[k]}</div></>, value: Math.round(v * 100) / 100 }))} />
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
              ? [["Viewers", fmtNum(c.viewers)], ["Started", c.started_at ? `${fmtTime(c.started_at)} ${zoneLabel()}` : "—"]] as [string, string][]
              : [["Views", fmtNum(c.views)], ["Views per hour", fmtNum(c.views_per_hour)], ["Likes", fmtNum(c.likes)], ["Comments", fmtNum(c.comments)], ["Published", c.published_at ? fmtAgo(c.published_at) : "—"]] as [string, string][]),
            ...extra.map(([k, v]) => [humanize(k), fmtNum(v)] as [string, string]),
            ...(typeof c.score_parts?.origin === "string" ? [["Observed by", humanize(c.score_parts.origin)] as [string, string]] : []),
            ...(c.score_parts?.chat_measured === false ? [["Chat rate", "estimated (chat not measured)"] as [string, string]] : []),
            ["Language", c.language ?? "—"],
            ["Tier", <Badge key="t" status={c.creator.tier} />],
            ["Result", c.picked ? <Badge key="r" tone="green">picked</Badge> : <Badge key="r" tone="gray">{cutoff !== null && c.trend_score >= cutoff ? "not picked" : `below cutoff (${cutoff ?? "—"})`}</Badge>],
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
