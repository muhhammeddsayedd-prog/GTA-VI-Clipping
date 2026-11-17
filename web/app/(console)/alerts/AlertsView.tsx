"use client";

import { AlertTriangle, Bell, CheckCircle2, Power } from "lucide-react";

import FadeContent from "@/components/reactbits/FadeContent";
import { Pager } from "@/components/kit/data";
import { Badge, Card, ErrorState, PageHead, RefreshBtn, Seg, SelectField, SkeletonTable, When } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Alert, Page } from "@/lib/api/types";
import { SEVERITY_TONE } from "@/lib/status";
import { useListParams } from "@/lib/use-list";
import { cn } from "@/lib/utils";

const KEYS = ["severity", "delivered"];

function look(a: Alert) {
  if (a.title.startsWith("Recovered") || a.title.startsWith("Emergency stop OFF")) return { tone: "green", icon: a.title.startsWith("Emergency") ? <Power size={16} /> : <CheckCircle2 size={16} /> };
  if (a.severity === "critical") return { tone: "red", icon: <AlertTriangle size={16} /> };
  if (a.severity === "warn") return { tone: "amber", icon: <AlertTriangle size={16} /> };
  return { tone: "blue", icon: <Bell size={16} /> };
}

export function AlertsView() {
  const lp = useListParams(KEYS);
  const q = useApi<Page<Alert>>("admin/alerts", lp.params, { interval: POLL.list });
  const counts = q.data?.counts ?? {};
  const delivered = lp.get("delivered");

  return (
    <>
      <PageHead title="Alerts" sub="Every message sent to Telegram · warn alerts are limited to 1 per title per 30 min · info is batched into the daily report"
        actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />
      <div className="filters">
        <span className="lab">Filters</span>
        <SelectField label="Delivery" value={delivered} onChange={(v) => lp.set("delivered", v)} width={170}
          options={[{ value: "", label: "Any delivery" }, { value: "true", label: "Delivered" }, { value: "false", label: "Not delivered" }]} />
        <span className="right">
          <Seg label="Severity" value={lp.get("severity")} onChange={(v) => lp.set("severity", v)} items={[
            { value: "", label: "All", count: counts.all }, { value: "critical", label: "Critical", count: counts.critical ?? 0 },
            { value: "warn", label: "Warn", count: counts.warn ?? 0 }, { value: "info", label: "Info", count: counts.info ?? 0 },
          ]} />
        </span>
      </div>
      {q.error && !q.data ? <ErrorState error={q.error} retry={() => q.refetch()} /> : !q.data ? <SkeletonTable /> : (
        <Card>
          {!q.data.items.length ? <div className="muted small" role="status" style={{ padding: 24, textAlign: "center" }}>No alerts match these filters.</div> : (
            <FadeContent duration={0.4} trigger="mount">
              <ul className="feed" style={{ listStyle: "none", margin: 0, padding: 0 }}>
                {q.data.items.map((a) => {
                  const l = look(a);
                  return (
                    <li key={a.id} className="fi" style={{ padding: "14px 4px" }}>
                      <span className={cn("ficon", l.tone)} aria-hidden>{l.icon}</span>
                      <div className="grow" style={{ minWidth: 0 }}>
                        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}><span className="t1">{a.title}</span><Badge tone={SEVERITY_TONE[a.severity]}>{a.severity}</Badge></div>
                        {a.body && <div className="t2">{a.body}</div>}
                      </div>
                      <div style={{ textAlign: "right", flex: "none" }}>
                        <div className="time"><When iso={a.created_at} /></div>
                        <div style={{ marginTop: 6 }}>
                          {a.sent_at ? <Badge tone="green" dot={false}>delivered</Badge>
                            : <Badge tone="red" dot={false}>not delivered · {a.attempts} tries</Badge>}
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </FadeContent>
          )}
          <Pager shown={q.data.items.length} total={counts.all} cursors={lp.cursors} next={q.data.next_cursor} onPage={lp.setCursors} noun="alerts" />
        </Card>
      )}
    </>
  );
}
