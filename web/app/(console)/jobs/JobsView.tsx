"use client";

import { Bolt, Layers, RefreshCw, X } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { DataTable, JsonBlock, Kpi, Pager, Steps, type Col } from "@/components/kit/data";
import { Drawer } from "@/components/kit/overlays";
import { Badge, Card, ErrorState, Grid, KV, Mono, PageHead, RefreshBtn, Seg, SelectField, SkeletonTable, When } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Job, Page } from "@/lib/api/types";
import { fmtAgo, fmtTime } from "@/lib/format";
import { useListParams } from "@/lib/use-list";
import { useDrawerParam } from "@/components/domain/ops/useDrawerParam";

const KEYS = ["status", "queue", "type"];
const TYPES = ["express.render", "clip.render", "clip.gate", "clip.precheck", "clip.precheck_result", "vod.process", "replay.find", "replay.scan", "vod.cleanup", "creator.permission", "post.hourly", "post.retry"];

export function JobsView() {
  const lp = useListParams(KEYS);
  const drawer = useDrawerParam("job");
  const q = useApi<Page<Job> & { meta?: { by_queue?: Record<string, Record<string, number>> } }>("admin/jobs", lp.params, { interval: POLL.list });
  const counts = q.data?.counts ?? {};
  const byQ = q.data?.meta?.by_queue ?? {};
  const n = (qn: string, st: string) => byQ[qn]?.[st] ?? 0;
  const openId = drawer.value;

  const cols: Col<Job>[] = [
    { key: "id", head: "Job", render: (j) => <span className="mono semi">#{j.id}</span> },
    { key: "t", head: "Type", render: (j) => <Badge tone={j.queue === "express" ? "purple" : "blue"} dot={false}>{j.type}</Badge> },
    { key: "q", head: "Queue", render: (j) => j.queue },
    { key: "p", head: "Priority", align: "right", render: (j) => <span className="num">{j.priority}</span> },
    { key: "s", head: "Status", render: (j) => <Badge status={j.status} /> },
    { key: "a", head: "Attempts", render: (j) => <span className="num">{j.attempts} / {j.max_attempts}</span> },
    { key: "w", head: "Worker", render: (j) => j.locked_by ? <Mono>{j.locked_by}</Mono> : j.status === "queued" && j.run_after && Date.parse(j.run_after) > Date.now() ? <span className="muted">runs {fmtAgo(j.run_after)}</span> : <span className="muted">—</span> },
    { key: "e", head: "Last error", render: (j) => j.last_error ? <span className="down xs ellipsis" style={{ display: "inline-block", maxWidth: 280 }} title={j.last_error}>{j.last_error.split("\n")[0]}</span> : <span className="muted">—</span> },
    { key: "c", head: "Created", render: (j) => <When iso={j.created_at} /> },
  ];

  return (
    <>
      <PageHead title="Jobs" sub="Postgres queue · FOR UPDATE SKIP LOCKED · higher priority runs first" actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />
      <AnimatedContent distance={20} duration={0.5} stagger={0.06} trigger="mount">
        <Grid cols={4}>
          <Kpi label="Express queue" value={n("express", "queued")} unit="queued" icon={<Bolt size={15} />} delta={<span>{n("express", "running")} running · renders and posting</span>} />
          <Kpi label="Normal queue" value={n("normal", "queued")} unit="queued" icon={<Layers size={15} />} delta={<span>{n("normal", "running")} running · VOD, gate, permission</span>} />
          <Kpi label="Failed" value={n("express", "failed") + n("normal", "failed")} icon={<X size={15} />} iconTone="red" delta={<span>after 3 attempts (30 s · 2 min · 10 min)</span>} />
          <Kpi label="Cancelled" value={n("express", "cancelled") + n("normal", "cancelled")} icon={<RefreshCw size={15} />} iconTone="amber" delta={<span>stuck and dead-worker resets show in Health</span>} />
        </Grid>
      </AnimatedContent>

      <div className="filters mt2">
        <span className="lab">Filters</span>
        <SelectField label="Type" value={lp.get("type")} onChange={(v) => lp.set("type", v)} width={190} options={[{ value: "", label: "All types" }, ...TYPES.map((t) => ({ value: t, label: t }))]} />
        <SelectField label="Queue" value={lp.get("queue")} onChange={(v) => lp.set("queue", v)} width={150} options={[{ value: "", label: "All queues" }, { value: "express", label: "express" }, { value: "normal", label: "normal" }]} />
        <span className="right">
          <Seg label="Status" value={lp.get("status")} onChange={(v) => lp.set("status", v)} items={[
            { value: "", label: "All", count: counts.all }, { value: "queued", label: "Queued", count: counts.queued ?? 0 }, { value: "running", label: "Running", count: counts.running ?? 0 },
            { value: "failed", label: "Failed", count: counts.failed ?? 0 }, { value: "done", label: "Done", count: counts.done ?? 0 }, { value: "cancelled", label: "Cancelled", count: counts.cancelled ?? 0 },
          ]} />
        </span>
      </div>

      {q.error && !q.data ? <ErrorState error={q.error} retry={() => q.refetch()} /> : !q.data ? <SkeletonTable /> : (
        <Card>
          <DataTable caption="Jobs" cols={cols} rows={q.data.items} rowKey={(j) => j.id} selected={openId ? Number(openId) : null}
            href={(j) => drawer.hrefFor(String(j.id))} empty="No jobs match these filters." />
          <Pager shown={q.data.items.length} total={counts.all} cursors={lp.cursors} next={q.data.next_cursor} onPage={lp.setCursors} noun="jobs" />
        </Card>
      )}
      <JobDrawer id={openId} onClose={() => drawer.set("")} />
    </>
  );
}

function JobDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useApi<Job>(id ? `admin/jobs/${encodeURIComponent(id)}` : null, undefined, { interval: POLL.detail });
  const j = q.data;
  const backoff = ["retry in 30 s", "retry in 2 min", "retry in 10 min"];
  const tries = j ? Array.from({ length: Math.max(j.attempts, 0) }, (_, i) => {
    const last = i === j.attempts - 1;
    const fail = j.status === "failed" || !last;
    return {
      state: (last && j.status === "running" ? "run" : last && j.status === "done" ? "done" : fail ? "fail" : "done") as "run" | "done" | "fail",
      title: `Attempt ${i + 1}`,
      sub: last ? (j.status === "running" ? `running on ${j.locked_by}` : j.status === "done" ? "succeeded" : "failed · no more retries") : `failed · ${backoff[i] ?? "retried"}`,
      time: last ? fmtTime(j.updated_at) : undefined,
    };
  }) : [];
  return (
    <Drawer open={!!id} onClose={onClose} title={j ? `Job #${j.id}` : "Job"} sub={j && <>{j.type} · {j.queue} · <Badge status={j.status} /></>} width={520}>
      {q.error ? <ErrorState error={q.error} /> : !j ? <div className="muted">Loading…</div> : (
        <>
          <KV rows={[["Priority", j.priority], ["Dedupe key", j.dedupe_key ? <Mono key="d">{j.dedupe_key}</Mono> : "—"], ["Created", <When key="c" iso={j.created_at} />],
            ["Run after", <When key="r" iso={j.run_after} />], ["Attempts", `${j.attempts} of ${j.max_attempts}`], ["Locked by", j.locked_by ?? "—"]]} />
          {tries.length > 0 && <><div className="flabel mt2">Attempts</div><Steps steps={tries} /></>}
          {j.last_error && <><div className="flabel mt2">Last error</div><pre className="code wrap" style={{ color: "#FFB4B6", margin: 0 }}>{j.last_error}</pre></>}
          <div className="flabel mt">Payload</div>
          <JsonBlock value={j.payload ?? null} maxHeight={260} label="Job payload" />
          <p className="muted xs" style={{ marginTop: 16 }}>Retry and cancel come in a later phase; today jobs retry on their own.</p>
        </>
      )}
    </Drawer>
  );
}
