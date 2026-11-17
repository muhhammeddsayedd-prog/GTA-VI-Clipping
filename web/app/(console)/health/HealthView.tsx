"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Info, List, Server } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { DataTable, EventFeed, JsonBlock, type Col } from "@/components/kit/data";
import { Drawer } from "@/components/kit/overlays";
import { Badge, Banner, Btn, Card, CardHead, ErrorState, Grid, KV, PageHead, RefreshBtn, Seg, SkeletonCards, When } from "@/components/kit/primitives";
import { HBars } from "@/components/kit/charts";
import { POLL, useApi } from "@/lib/api/client";
import type { Event, Health, Heartbeat, Incident } from "@/lib/api/types";
import { fmtAgo, fmtDuration, fmtTime } from "@/lib/format";

type Selected = { key: string; title: string; opened_at: string; open: boolean; data: Record<string, unknown> | null };

export function HealthView() {
  const q = useApi<Health>("admin/health", undefined, { interval: POLL.status });
  const [tab, setTab] = useState("open");
  const [sel, setSel] = useState<Selected | null>(null);

  const groups = useMemo(() => {
    const g = new Map<string, Heartbeat[]>();
    // Group by service, not by the API's generic `kind`: worker-express-<host>-<pid> → worker-express.
    // n8n trigger heartbeats (trigger:*) have their own table below.
    for (const h of q.data?.heartbeats ?? []) {
      if (h.name.startsWith("trigger:")) continue;
      const svc = h.name.match(/^(worker-[a-z]+)/)?.[1] ?? h.name;
      g.set(svc, [...(g.get(svc) ?? []), h]);
    }
    return [...g.entries()];
  }, [q.data]);

  if (q.error && !q.data) return <><PageHead title="Health" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Health" sub="Loading…" /><SkeletonCards n={4} /></>;
  const h = q.data;
  const now = q.dataUpdatedAt;

  const recovered = new Set(h.incidents.filter((e) => e.type === "health.recovered").map((e) => e.entity_id));
  const closedToday = h.incidents.filter((e) => e.type === "health.incident" && recovered.has(e.entity_id) && now - Date.parse(e.created_at) < 86400e3);
  const panelItems: Selected[] = tab === "open"
    ? h.open_incidents.map((i: Incident) => ({ key: i.key, title: i.title, opened_at: i.opened_at, open: true, data: i.data }))
    : closedToday.map((e) => ({ key: e.entity_id ?? String(e.id), title: e.message ?? e.type, opened_at: e.created_at, open: false, data: e.data }));

  const histRows = h.incidents.filter((e) => e.type === "health.incident").map((e) => {
    const rec = h.incidents.find((r) => r.type === "health.recovered" && r.entity_id === e.entity_id && Date.parse(r.created_at) > Date.parse(e.created_at));
    return { e, rec };
  });
  const histCols: Col<{ e: Event; rec?: Event }>[] = [
    { key: "o", head: "Opened", render: (r) => <When iso={r.e.created_at} /> },
    { key: "i", head: "Incident", render: (r) => <span className="semi">{r.e.message}</span> },
    { key: "d", head: "Duration", render: (r) => fmtDuration(((r.rec ? Date.parse(r.rec.created_at) : now) - Date.parse(r.e.created_at)) / 1000) },
    { key: "s", head: "State", render: (r) => <Badge status={r.rec ? "recovered" : "open"} /> },
  ];

  return (
    <>
      <PageHead title="Health" sub="Heartbeats every 30 s · stale after 5 min · stuck jobs reset after 2 h"
        actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />

      <AnimatedContent distance={20} duration={0.5} stagger={0.06} trigger="mount">
        <Grid cols={5}>
          {groups.map(([kind, beats]) => {
            // Restarted workers leave old per-process rows that stay stale; the service is down only if none is fresh.
            const live = beats.filter((b) => !b.stale).length;
            const down = live === 0;
            const newest = beats.reduce((a, b) => (a.age_s < b.age_s ? a : b));
            const info = newest.info ?? {};
            const sub = kind === "live-manager" ? `${info.active ?? "?"} sessions · max ${info.max_slots ?? "?"}` : kind.startsWith("worker-") ? `${live} running` : kind === "api" ? "FastAPI · /v1/health" : "1 replica";
            const old = beats.length - live;
            return (
              <Card key={kind} as="div">
                <div className="row"><span className="cic" aria-hidden><Server size={15} /></span><span className="semi">{kind}</span>
                  <span className={`right sdot ${down ? "red" : "green"}`} role="img" aria-label={down ? "down" : "up"} /></div>
                <div className="muted xs" style={{ marginTop: 10 }}>{sub}{down ? <span className="down"> · no fresh heartbeat</span> : old > 0 && kind.startsWith("worker-") ? ` · ${old} old process rows` : null}</div>
                <div className="row" style={{ marginTop: 12 }}><span className="muted xs">last seen</span><span className="right semi">{fmtDuration(newest.age_s)} ago</span></div>
              </Card>
            );
          })}
        </Grid>
      </AnimatedContent>

      <div className="g mt2 collapse-md" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Card>
          <CardHead title="n8n triggers" hint="If n8n misses a trigger past its grace, the scheduler runs it itself" />
          <DataTable dense caption="n8n triggers" rowKey={(t) => t.name} rows={h.triggers} cols={[
            { key: "n", head: "Trigger", render: (t) => <span className="semi">{t.name}</span> },
            { key: "l", head: "Last fired", render: (t) => <span title={t.last_seen ?? undefined}>{t.last_seen ? fmtAgo(t.last_seen) : "never"}</span> },
            { key: "g", head: "Grace", render: (t) => fmtDuration(t.grace_s) },
            { key: "s", head: "Source", render: (t) => <Badge status={t.source === "failover" ? "failover" : "n/a"} dot={false}>{t.source ?? "—"}</Badge> },
            { key: "st", head: "State", render: (t) => <Badge status={t.stale ? "stale" : "ok"} /> },
          ]} />
        </Card>
        <section className="dpanel" aria-labelledby="inc-h">
          <div className="row" style={{ flexWrap: "wrap" }}>
            <h2 id="inc-h" style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Incidents</h2>
            <span className="right"><Seg dark label="Incidents" value={tab} onChange={setTab}
              items={[{ value: "open", label: "Open", count: h.open_incidents.length }, { value: "today", label: "Closed today", count: closedToday.length }]} /></span>
          </div>
          <ul className="dlist" style={{ listStyle: "none", padding: 0 }}>
            {panelItems.map((i, n) => (
              <li key={i.key + n}>
                <button type="button" className={`ditem link${n === 0 && i.open ? " on" : ""}`} onClick={() => setSel(i)}
                  style={{ width: "100%", border: 0, background: n === 0 && i.open ? undefined : "transparent", textAlign: "left", font: "inherit", color: "inherit" }}>
                  <span className={`sdot ${i.open ? "red" : "gray"}`} aria-hidden />
                  <div style={{ minWidth: 0 }}><div className="t1">{i.title}</div><div className="t2">opened {fmtTime(i.opened_at)}</div></div>
                  <span className="amt"><span className="dbadge">{i.open ? fmtDuration((now - Date.parse(i.opened_at)) / 1000) : "closed"}</span></span>
                </button>
              </li>
            ))}
            {!panelItems.length && <li style={{ color: "var(--dmute)", padding: 12 }}>{tab === "open" ? "No open incidents. Everything is healthy." : "Nothing closed today."}</li>}
          </ul>
        </section>
      </div>

      <Card style={{ marginTop: 16 }}>
        <CardHead title="Incident history" hint="health.incident / health.recovered events" actions={<Btn sm href="/events?type_prefix=health." icon={<List size={15} />}>Events</Btn>} />
        <DataTable caption="Incident history" rowKey={(r) => r.e.id} rows={histRows} cols={histCols} />
      </Card>

      <IncidentDrawer sel={sel} events={h.incidents} onClose={() => setSel(null)} />
    </>
  );
}

function IncidentDrawer({ sel, events, onClose }: { sel: Selected | null; events: Event[]; onClose: () => void }) {
  const data = sel?.data ?? {};
  const related = sel ? events.filter((e) => e.entity_id === sel.key) : [];
  const gb = (k: string) => (typeof data[k] === "number" ? (data[k] as number) : null);
  const usage = [["Live buffers", gb("live_buffers_gb")], ["VOD windows", gb("vod_windows_gb")], ["Rendered clips", gb("clips_gb")], ["Chat files", gb("chat_gb")]]
    .filter(([, v]) => v !== null) as [string, number][];
  return (
    <Drawer open={!!sel} onClose={onClose} title={sel?.title ?? ""} sub={sel && <>Incident · opened {fmtTime(sel.opened_at)} UTC · <Badge status={sel.open ? "open" : "recovered"} /></>}>
      {sel && (
        <>
          {typeof data.pct === "number" && (
            <Grid cols={2}>
              <Card as="div"><div className="muted xs">Now</div><div style={{ fontSize: 26, fontWeight: 600 }} className="down">{String(data.pct)}%</div><div className="muted xs">{String(data.path ?? "data volume")}</div></Card>
              <Card as="div"><div className="muted xs">Recovers when</div><div style={{ fontSize: 26, fontWeight: 600 }}>&lt; {String(data.warn_pct ?? 80)}%</div><div className="muted xs">guardian.disk_warn_pct</div></Card>
            </Grid>
          )}
          {usage.length > 0 && <><div className="flabel mt2">What is using it</div><HBars items={usage.map(([label, value]) => ({ label, value }))} unit=" GB" labelWidth={130} /></>}
          <div className="flabel mt2">Timeline</div>
          <EventFeed events={related} empty="No related events." />
          {!usage.length && <><div className="flabel mt2">Data</div><JsonBlock value={data} maxHeight={240} /></>}
          <KV rows={[["Incident key", <span key="k" className="mono">{sel.key}</span>], ["Opened", fmtAgo(sel.opened_at)]]} />
          {sel.key === "disk" && <Banner tone="blue" style={{ marginTop: 16 }} icon={<Info size={16} />}>Storage file retention is not built yet, so rendered clips are not cleaned automatically.</Banner>}
          <div className="row mt2"><Link className="btn sm" href={`/events?entity_type=incident&entity_id=${encodeURIComponent(sel.key)}`}>All events for this incident</Link></div>
        </>
      )}
    </Drawer>
  );
}
