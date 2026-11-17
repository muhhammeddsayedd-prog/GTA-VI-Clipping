"use client";

import Link from "next/link";
import { useState } from "react";
import { List } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { Legend } from "@/components/kit/charts";
import { DataTable, Hours, type Col } from "@/components/kit/data";
import { Badge, Btn, Card, CardHead, ErrorState, PageHead, Plat, Prog, RefreshBtn, Seg, SkeletonCards, When } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { CapRow, Event, QueueView as Q } from "@/lib/api/types";
import { useNow } from "@/components/domain/clips/useNow";
import { fmtAgo, fmtDuration, fmtTime, series } from "@/lib/format";

const SLOT_CLS: Record<string, string> = { posted: "done", planned: "on", skipped: "skip", failed: "fail", not_allowed: "off", empty: "skip" };

const CAP_COLS: Col<CapRow>[] = [
  { key: "a", head: "Account", render: (r) => <span className="semi">{r.account_id}</span> },
  { key: "u", head: "Used 24h", align: "right", render: (r) => <span className="num">{r.used_24h} / {r.cap_24h}</span> },
  { key: "f", head: "Free", align: "right", render: (r) => <span className="num">{r.free}</span> },
  { key: "r", head: "Reserve", align: "right", render: (r) => <span className="num">{r.spike_reserve_remaining}</span> },
  { key: "g", head: "Gap ok", render: (r) => <Badge tone={r.gap_ok ? "green" : "amber"}>{r.gap_ok ? "yes" : "no"}</Badge> },
  {
    key: "al", head: "Allowed", mobile: "aside", render: (r) => r.paused_until && Date.parse(r.paused_until) > Date.now()
      ? <Badge tone="amber">paused until {fmtTime(r.paused_until)}</Badge>
      : r.hourly_allowed && r.spike_allowed ? <Badge tone="purple">both</Badge> : r.hourly_allowed ? <Badge tone="blue">hourly</Badge> : r.spike_allowed ? <Badge tone="amber">spike only</Badge> : <Badge tone="red">none</Badge>,
  },
];

const DEC_COLS: Col<Event>[] = [
  { key: "t", head: "Slot", render: (e) => <When iso={(e.data?.slot_at as string | undefined) ?? e.created_at} /> },
  { key: "a", head: "Account", mobile: "primary", render: (e) => String(e.data?.account_id ?? e.entity_id ?? "—") },
  { key: "d", head: "Decision", mobile: "aside", render: (e) => <Badge status={e.type === "posting.slot_empty" ? "empty" : e.type.startsWith("post.failed") ? "failed" : "skipped"}>{e.type === "posting.slot_empty" ? "empty" : e.type.startsWith("post.failed") ? "failed" : "skipped"}</Badge> },
  { key: "r", head: "Rule", render: (e) => <span className="mono">{String(e.data?.rule ?? "—")}</span> },
  { key: "w", head: "Why", render: (e) => e.message ?? "—" },
];

export function QueueView() {
  const q = useApi<Q>("admin/queue", undefined, { interval: POLL.list });
  const [lane, setLane] = useState("hourly");
  const now = useNow();
  if (q.error && !q.data) return <><PageHead title="Posting queue" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Posting queue" sub="Loading…" /><SkeletonCards n={2} h={420} /></>;
  const d = q.data;
  const ranked = lane === "spike" ? d.ranked.spike : d.ranked.hourly;
  const nextIn = d.next_slot_at && now ? (Date.parse(d.next_slot_at) - now) / 1000 : null;
  const nowH = now ? new Date(now).getUTCHours() : -1;

  return (
    <>
      <PageHead title="Posting queue"
        sub={<>What will post next, where, and why slots were skipped{d.next_slot_at ? <> · next hourly slot <b className="ink2">{fmtTime(d.next_slot_at)} UTC</b>{nextIn !== null && nextIn > 0 ? ` in ${fmtDuration(nextIn)}` : ""}</> : null}</>}
        actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />

      <div className="g collapse-md" style={{ gridTemplateColumns: "480px 1fr" }}>
        <section className="dpanel" aria-labelledby="ranked-h">
          <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
            <h2 id="ranked-h" style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>{lane === "spike" ? "Spike lane" : "Hourly lane"} · ranked</h2>
            <span className="right"><Seg dark label="Lane" value={lane} onChange={setLane} items={[{ value: "hourly", label: "Hourly", count: d.ranked.hourly.length }, { value: "spike", label: "Spike", count: d.ranked.spike.length }]} /></span>
          </div>
          <div className="xs" style={{ color: "var(--dmute)", marginTop: 6 }}>rank = score × freshness × spike bonus × preferred · bar = time left before the clip expires</div>
          {!ranked.length ? (
            <div style={{ padding: "30px 6px", color: "var(--dmute)" }}>Nothing approved is waiting in this lane.</div>
          ) : (
            <AnimatedContent distance={16} duration={0.45} stagger={0.05} trigger="mount">
              <ol className="dlist" style={{ listStyle: "none", padding: 0 }}>
                {ranked.map((it, i) => {
                  const life = it.expires_at && now ? Math.max(0, (Date.parse(it.expires_at) - now) / (72 * 3600e3)) * 100 : 100;
                  return (
                    <li key={it.clip_id}>
                      <Link href={`/clips/${it.clip_id}`} className={`ditem link${i === 0 ? " on" : ""}`}>
                        <span className="dbadge" style={{ minWidth: 30, textAlign: "center" }}>{i + 1}</span>
                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div className="t1 ellipsis">{series(it.series_no)} · {it.title ?? it.hook}</div>
                          <div className="t2">{it.creator.handle} · {fmtDuration(it.age_s)} old{it.expires_at ? ` · expires ${fmtAgo(it.expires_at)}` : ""}</div>
                          <div style={{ marginTop: 6, width: 140 }}><Prog pct={life} thin label="Freshness left" /></div>
                        </div>
                        <span className="amt">{it.rank_score?.toFixed(2) ?? "—"}<div className="t2">rank</div></span>
                      </Link>
                    </li>
                  );
                })}
              </ol>
            </AnimatedContent>
          )}
        </section>

        <div className="col">
          <Card>
            <CardHead title="Hourly slots · today (UTC)" hint="Each cell is one slot · jitter 0–5 min"
              actions={<Legend items={[{ color: "var(--p)", label: "posted" }, { color: "var(--ps2)", label: "planned" }, { color: "#F6D7A4", label: "skipped / empty" }, { color: "var(--red)", label: "failed" }, { color: "#E6E6EE", label: "not allowed" }]} />} />
            {d.slots.map((s, i) => (
              <div key={s.account_id} className="row slot-row" style={{ gap: 14, padding: "10px 0", borderBottom: i < d.slots.length - 1 ? "1px solid var(--line)" : 0 }}>
                <Link href={`/accounts/${s.account_id}`} className="row slot-label" style={{ width: 160, flex: "none" }}><Plat p={s.platform} /><span className="semi">{s.account_id}</span></Link>
                <div className="grow" style={{ minWidth: 0 }}>
                  <Hours labels={i === d.slots.length - 1} cells={s.hours.map((h) => ({
                    cls: `${SLOT_CLS[h.state] ?? ""}${h.hour === nowH ? " now" : ""}`,
                    title: `${String(h.hour).padStart(2, "0")}:00 — ${h.state.replace("_", " ")}`,
                  }))} />
                </div>
              </div>
            ))}
          </Card>
          <Card>
            <CardHead title={`Cap math${d.next_slot_at ? ` · next slot ${fmtTime(d.next_slot_at)}` : ""}`} hint="The same numbers the poster uses · recomputed every slot" />
            <DataTable caption="Cap math per account" cols={CAP_COLS} rows={d.caps} rowKey={(r) => r.account_id} href={(r) => `/accounts/${r.account_id}`} dense />
          </Card>
        </div>
      </div>

      <Card style={{ marginTop: 16 }}>
        <CardHead title="Slot decisions" hint="posting.slot_skipped · posting.slot_empty · precedence rules R1–R11"
          actions={<Btn sm href="/events?type_prefix=posting." icon={<List size={15} />}>Events</Btn>} />
        <DataTable caption="Slot decisions" cols={DEC_COLS} rows={d.decisions} rowKey={(e) => e.id} dense empty="No skipped or empty slots today." />
      </Card>
    </>
  );
}
