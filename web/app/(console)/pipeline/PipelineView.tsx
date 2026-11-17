"use client";

import Link from "next/link";
import { Fragment, useState, type ReactNode } from "react";
import { Bolt, Check, ChevronRight, Compass, Film, Layers, Radio, Scissors, Send, Video } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import CountUp from "@/components/reactbits/CountUp";
import { Bars, HBars, Legend, Line, SegBar, Stacked } from "@/components/kit/charts";
import { Badge, Btn, Card, CardHead, ErrorState, PageHead, RefreshBtn, Seg, SkeletonCards } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Pipeline } from "@/lib/api/types";
import { fmtTime, humanize } from "@/lib/format";
import { splitReason } from "@/lib/shape";

const STAGE_COLORS = ["#C9C2FF", "#9F94FF", "#7C6EFF", "#5A4AF4", "#3A2CC7", "#12A150", "#0D7A3D", "#6b6b82"];
const WINDOWS = [{ value: "24h", label: "24h" }, { value: "7d", label: "7 days" }, { value: "30d", label: "30 days" }];

interface Node { key: string; icon: ReactNode; name: string; big: string; big2?: number; label: string; sub: string; href: string; warn?: ReactNode }

export function PipelineView() {
  const [win, setWin] = useState("24h");
  const q = useApi<Pipeline>("admin/pipeline", { window: win }, { interval: POLL.list });
  const head = (
    <PageHead title="Pipeline" sub={`Every stage from stream to post · counts for the last ${win === "24h" ? "24 hours" : win === "7d" ? "7 days" : "30 days"}`}
      actions={<><Seg label="Time window" items={WINDOWS} value={win} onChange={setWin} /><RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} /></>} />
  );
  if (q.error && !q.data) return <>{head}<ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <>{head}<SkeletonCards n={4} /><div className="mt2"><SkeletonCards n={4} h={220} /></div></>;
  const p = q.data;
  const n = p.nodes;
  const nodes: Node[] = [
    { key: "discovery", icon: <Compass size={15} />, name: "Discovery", big: "", big2: n.discovery.candidates, label: "candidates seen", sub: `${n.discovery.picked} picked`, href: "/discovery" },
    { key: "live", icon: <Radio size={15} />, name: "Live", big: `${n.live.active} / ${n.live.max_slots}`, label: "slots recording", sub: `${n.live.sessions} sessions`, href: "/live" },
    { key: "vod", icon: <Video size={15} />, name: "VOD", big: "", big2: n.vod.done, label: "videos done", sub: `${n.vod.processing} processing · ${n.vod.new} new`, href: "/videos",
      warn: n.vod.failed ? <Badge tone="red">{n.vod.failed} failed</Badge> : undefined },
    { key: "spikes", icon: <Bolt size={15} />, name: "Spikes", big: "", big2: n.spikes.detected, label: "detected", sub: `${n.spikes.confirmed} confirmed`, href: "/spikes" },
    { key: "render", icon: <Scissors size={15} />, name: "Render", big: "", big2: n.render.rendered, label: "clips rendered", sub: `${n.render.failed} failed`, href: "/clips",
      warn: n.render.failed ? <Badge tone="red">{n.render.failed} failed</Badge> : undefined },
    { key: "gate", icon: <Check size={15} />, name: "Gate", big: "", big2: n.gate.approved, label: "approved", sub: `${n.gate.rejected} rejected`, href: "/clips?status=approved" },
    { key: "queue", icon: <Layers size={15} />, name: "Queue", big: "", big2: n.queue.waiting, label: "waiting", sub: `${n.queue.expired} expired`, href: "/queue" },
    { key: "posts", icon: <Send size={15} />, name: "Posts", big: "", big2: n.posts.published, label: "published", sub: `${n.posts.failed} failed`, href: "/posts",
      warn: n.posts.failed ? <Badge tone="red">{n.posts.failed} failed</Badge> : undefined },
  ];
  const funnelCats = ["Detected", "Confirmed", "Clipped", "Posted"] as const;
  const tiers = p.funnel.filter((f) => f.tier !== "rejected");
  const rejected = p.funnel.find((f) => f.tier === "rejected");
  const stages = p.express_stages.filter((s): s is { stage: string; median_s: number } => typeof s.median_s === "number");
  const totalExpress = Math.round(stages.reduce((a, s) => a + s.median_s, 0));
  const hours = hourBuckets(p.approved_by_hour);
  const rejects = groupRejects(p.rejects);

  return (
    <>
      {head}
      <AnimatedContent distance={20} duration={0.5} trigger="mount">
        <nav aria-label="Pipeline stages" className="pipe-flow">
          {nodes.map((node, i) => (
            <Fragment key={node.key}>
              {i > 0 && <div className="pipe-arrow" aria-hidden><ChevronRight size={18} /></div>}
              <Link href={node.href} className="card pipe-node">
                <div className="row"><span className="cic">{node.icon}</span><span className="semi">{node.name}</span></div>
                <div className="num pipe-big">{node.big2 !== undefined ? <CountUp to={node.big2} separator="," duration={1} trigger="load" /> : node.big}</div>
                <div className="muted xs">{node.label}</div>
                <div className="small semi" style={{ marginTop: 10 }}>{node.sub}</div>
                <div style={{ marginTop: 8, minHeight: 22 }}>{node.warn}</div>
              </Link>
            </Fragment>
          ))}
        </nav>
      </AnimatedContent>

      <div className="g g2 mt2 collapse-md">
        <Card>
          <CardHead title="Spike funnel by tier" hint="Detected → confirmed by Claude → clipped → posted" />
          <Stacked label="Spike funnel: spikes per stage, stacked by tier"
            series={tiers.map((t) => funnelCats.map((c) => t[c.toLowerCase() as "detected" | "confirmed" | "clipped" | "posted"]))}
            colors={["#5A4AF4", "#9F94FF", "#DDD7FF"]} labels={[...funnelCats]} width={560} height={190} />
          <div className="mt">
            <Legend items={[{ color: "#5A4AF4", label: "Tier S" }, { color: "#9F94FF", label: "Tier A" }, { color: "#DDD7FF", label: "Tier B" }]} />
          </div>
          {rejected && <div className="muted xs" style={{ marginTop: 8 }}>{rejected.detected.toLocaleString("en-US")} more detected spikes were rejected as noise by Claude or merged.</div>}
        </Card>
        <Card>
          <CardHead title="Rejects by reason" hint={`${p.rejects.reduce((a, r) => a + r.n, 0)} clips · first failing gate check, or the writer's verdict before render`} actions={<Btn sm href="/clips?status=rejected" icon={<Film size={15} />}>Rejected clips</Btn>} />
          <HBars items={rejects.map((r) => ({ label: r.writer ? <span>Writer (Claude) said no</span> : <span className="mono">{r.reason}</span>, value: r.n, tone: r.reason.startsWith("spoiler") ? "red" as const : r.reason.startsWith("music") ? "amber" as const : undefined }))} />
          {!p.rejects.length && <div className="muted small">No rejects in this window.</div>}
        </Card>
        <Card>
          <CardHead title="Express path · median by stage" hint="Spike detected → post published"
            actions={stages.length ? <Badge tone={totalExpress <= 240 ? "green" : "amber"}>{totalExpress} s total</Badge> : undefined} />
          {stages.length ? (
            <SegBar parts={stages.map((s, i) => ({ label: humanize(s.stage), value: Math.round(s.median_s), color: STAGE_COLORS[i % STAGE_COLORS.length] }))} />
          ) : <div className="muted small">No express clips in this window: no S-tier spike went through the express path yet.</div>}
          <div className="mt">
            <Line label="Express latency per clip, seconds; dashed line is the 4 minute target" values={p.express_series.map((x) => x.s)} target={240} width={560} height={110}
              labels={p.express_series.map((x, i) => (i === 0 || i === p.express_series.length - 1 ? fmtTime(x.t) : ""))} />
          </div>
          <div className="muted xs">Dashed line = 4 min target (PRD). An alert fires after 3 clips over 6 min.</div>
        </Card>
        <Card>
          <CardHead title="Throughput" hint="Clips approved per hour, last 24 hours" />
          <Bars label="Clips approved per hour; dashed line is one clip per hour" values={hours.map((x) => x.n)} labels={hours.map((x, i) => (i % 3 === 0 ? String(x.h) : ""))} target={1} width={560} height={150} highlight={null} />
          <div className="muted xs">Dashed line = 1 clip/hour needed to keep every hourly slot filled.</div>
        </Card>
      </div>
    </>
  );
}

/** The API lists only hours that had approvals; spread them over the last 24 UTC hours (oldest first). */
function hourBuckets(rows: { t: string; n: number }[], now = Date.now()) {
  const top = Math.floor(now / 3_600_000) * 3_600_000;
  const byHour = new Map(rows.map((r) => [Math.floor(Date.parse(r.t) / 3_600_000) * 3_600_000, r.n]));
  return Array.from({ length: 24 }, (_, i) => {
    const t = top - (23 - i) * 3_600_000;
    return { h: new Date(t).getUTCHours(), n: byHour.get(t) ?? 0 };
  });
}

/** Writer verdicts are sentences, one per clip: count them as one reason. */
function groupRejects(rows: { reason: string; n: number }[]) {
  const writer = rows.filter((r) => splitReason(r.reason).kind === "writer").reduce((a, r) => a + r.n, 0);
  const codes = rows.filter((r) => splitReason(r.reason).kind !== "writer").map((r) => ({ ...r, writer: false }));
  return [...codes, ...(writer ? [{ reason: "writer", n: writer, writer: true }] : [])].sort((a, b) => b.n - a.n);
}
