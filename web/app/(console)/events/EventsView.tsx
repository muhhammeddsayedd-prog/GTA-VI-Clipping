"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { X } from "lucide-react";

import { DataTable, EventFeed, JsonBlock, LevelBadge, Pager, type Col } from "@/components/kit/data";
import { Drawer } from "@/components/kit/overlays";
import { Btn, Card, ErrorState, KV, Mono, PageHead, RefreshBtn, SearchField, Seg, SelectField, SkeletonTable, When } from "@/components/kit/primitives";
import { useDrawerParam } from "@/components/domain/ops/useDrawerParam";
import { entityHref } from "@/components/domain/ops/entity";
import { POLL, useApi } from "@/lib/api/client";
import type { Event, Page } from "@/lib/api/types";
import { fmtTime } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

const KEYS = ["type_prefix", "level", "service", "entity_type", "entity_id"];
const SERVICES = ["api", "scheduler", "live-manager", "worker-express", "worker-normal", "posting"];
const ENTITIES = ["clip", "spike", "post", "video", "live_session", "creator", "account", "job", "incident", "takedown", "setting"];

export function EventsView() {
  const lp = useListParams(KEYS);
  const drawer = useDrawerParam("event");
  const q = useApi<Page<Event>>("admin/events", lp.params, { interval: POLL.list });
  const counts = q.data?.counts ?? {};
  const [prefix, setPrefix] = useState(lp.get("type_prefix"));
  useEffect(() => {
    const t = setTimeout(() => { if (prefix !== lp.get("type_prefix")) lp.set("type_prefix", prefix.trim()); }, 350);
    return () => clearTimeout(t);
  }, [prefix, lp]);
  const entityId = lp.get("entity_id");

  const cols: Col<Event>[] = [
    { key: "t", head: "Time", render: (e) => <span className="mono"><When iso={e.created_at} seconds /></span> },
    { key: "l", head: "Level", render: (e) => <LevelBadge level={e.level} /> },
    { key: "ty", head: "Type", render: (e) => <Mono className="semi">{e.type}</Mono> },
    { key: "s", head: "Service", render: (e) => e.service ?? "—" },
    { key: "en", head: "Entity", render: (e) => e.entity_type ? <span>{e.entity_type}{e.entity_id ? <span className="muted"> · {e.entity_id.slice(0, 12)}</span> : null}</span> : <span className="muted">—</span> },
    { key: "m", head: "Message", render: (e) => <span className="ellipsis" style={{ display: "inline-block", maxWidth: 420 }} title={e.message ?? undefined}>{e.message ?? "—"}</span> },
  ];

  return (
    <>
      <PageHead title="Events" sub="Full audit log · every state change writes a row · kept 30 days" actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />
      <div className="filters">
        <span className="lab">Filters</span>
        <SearchField label="Type starts with" placeholder="Type starts with… e.g. post." value={prefix} onChange={setPrefix} width={230} />
        <SelectField label="Service" value={lp.get("service")} onChange={(v) => lp.set("service", v)} width={170} options={[{ value: "", label: "All services" }, ...SERVICES.map((s) => ({ value: s, label: s }))]} />
        <SelectField label="Entity" value={lp.get("entity_type")} onChange={(v) => lp.set("entity_type", v)} width={160} options={[{ value: "", label: "All entities" }, ...ENTITIES.map((s) => ({ value: s, label: s }))]} />
        <span className="right">
          <Seg label="Level" value={lp.get("level")} onChange={(v) => lp.set("level", v)} items={[
            { value: "", label: "All", count: counts.all }, { value: "info", label: "Info", count: counts.info ?? 0 }, { value: "warn", label: "Warn", count: counts.warn ?? 0 },
            { value: "error", label: "Error", count: counts.error ?? 0 }, { value: "critical", label: "Critical", count: counts.critical ?? 0 },
          ]} />
        </span>
      </div>
      {entityId && (
        <div className="banner purple" role="status">
          <span className="grow">Showing events for <b>{lp.get("entity_type") || "entity"}</b> <span className="mono">{entityId}</span></span>
          <Btn sm icon={<X size={13} />} onClick={() => lp.set("entity_id", "")}>Clear</Btn>
        </div>
      )}
      {q.error && !q.data ? <ErrorState error={q.error} retry={() => q.refetch()} /> : !q.data ? <SkeletonTable rows={10} /> : (
        <Card>
          <DataTable dense caption="Events" cols={cols} rows={q.data.items} rowKey={(e) => e.id} selected={drawer.value ? Number(drawer.value) : null}
            href={(e) => drawer.hrefFor(String(e.id))} empty="No events match these filters." />
          <Pager shown={q.data.items.length} total={counts.all} cursors={lp.cursors} next={q.data.next_cursor} onPage={lp.setCursors} noun="events" />
        </Card>
      )}
      <EventDrawer id={drawer.value} onClose={() => drawer.set("")} onOpen={(id) => drawer.set(String(id))} />
    </>
  );
}

function EventDrawer({ id, onClose, onOpen }: { id: string; onClose: () => void; onOpen: (id: number) => void }) {
  const q = useApi<Event & { related: Event[] }>(id ? `admin/events/${encodeURIComponent(id)}` : null, undefined, { interval: false });
  const e = q.data;
  const link = e ? entityHref(e.entity_type, e.entity_id) : null;
  return (
    <Drawer open={!!id} onClose={onClose} title={e ? <span className="mono" style={{ fontSize: 18 }}>{e.type}</span> : "Event"}
      sub={e && <>Event #{e.id.toLocaleString("en-US")} · {fmtTime(e.created_at, true)} UTC · <LevelBadge level={e.level} /></>}>
      {q.error ? <ErrorState error={q.error} /> : !e ? <div className="muted">Loading…</div> : (
        <>
          <KV rows={[["Service", e.service ?? "—"],
            ["Entity", e.entity_type ? <span key="e">{e.entity_type}{e.entity_id ? <> · {link ? <Link className="pc" href={link}>{e.entity_id}</Link> : <span className="mono">{e.entity_id}</span>}</> : null}</span> : "—"],
            ["Message", e.message ?? "—"]]} />
          <div className="flabel mt2">data</div>
          <JsonBlock value={e.data} maxHeight={320} label="Event data" />
          <div className="flabel mt2">Related · same entity, close in time</div>
          <EventFeed events={e.related} onOpen={(r) => onOpen(r.id)} empty="No related events." />
          <div className="row mt2" style={{ flexWrap: "wrap" }}>
            {link && <Btn kind="p" href={link}>Open {e.entity_type}</Btn>}
            {e.entity_id && <Btn href={`/events?entity_type=${encodeURIComponent(e.entity_type ?? "")}&entity_id=${encodeURIComponent(e.entity_id)}`}>All events for this {e.entity_type}</Btn>}
          </div>
        </>
      )}
    </Drawer>
  );
}
