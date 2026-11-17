"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Clock, Edit3, Lock, Power } from "lucide-react";

import FadeContent from "@/components/reactbits/FadeContent";
import { DataTable, EventFeed, type Col } from "@/components/kit/data";
import { Drawer } from "@/components/kit/overlays";
import { Badge, Btn, Card, CardHead, Chip, ErrorState, IconBtn, Mono, PageHead, SearchField, SkeletonTable, Toggle, When } from "@/components/kit/primitives";
import { SettingEditModal, type EditRequest } from "@/components/domain/ops/SettingEditModal";
import { useDrawerParam } from "@/components/domain/ops/useDrawerParam";
import { POLL, useApi } from "@/lib/api/client";
import type { Event, Json, Setting } from "@/lib/api/types";
import { useListParams } from "@/lib/use-list";
import { cn } from "@/lib/utils";

const SWITCHES: [string, string][] = [
  ["launch.dry_run", "Dry run"], ["precheck.enabled", "YouTube pre-check"], ["vod.fallback_enabled", "Transcript fallback"],
  ["guardian.failover_enabled", "Trigger failover"], ["live.ab_clips", "Live A/B clips"], ["posting.staging_allow_instagram", "Instagram from staging"],
];
const STOP_KEYS = new Set(["control.emergency_stop", "control.stop_reason"]);

function preview(v: Json): string {
  const t = JSON.stringify(v);
  return t.length > 96 ? `${t.slice(0, 95)}…` : t;
}

export function SettingsView() {
  const lp = useListParams(["group", "q"]);
  const keyParam = useDrawerParam("key");
  const q = useApi<{ items: Setting[] }>("admin/settings", undefined, { interval: POLL.slow });
  const [search, setSearch] = useState(lp.get("q"));
  const [edit, setEdit] = useState<EditRequest | null>(null);
  const [history, setHistory] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => { if (search !== lp.get("q")) lp.set("q", search.trim()); }, 250);
    return () => clearTimeout(t);
  }, [search, lp]);

  const all = useMemo(() => q.data?.items ?? [], [q.data]);
  const groups = useMemo(() => {
    const m = new Map<string, number>();
    for (const s of all) m.set(s.group, (m.get(s.group) ?? 0) + 1);
    return [...m.entries()];
  }, [all]);
  const byKey = useMemo(() => new Map(all.map((s) => [s.key, s])), [all]);

  // ?key=gate.hourly selects its group and opens the editor (derived, not stored)
  const deepKey = keyParam.value;
  const deepSetting = deepKey ? byKey.get(deepKey) : undefined;
  const deepEdit: EditRequest | null = deepSetting && !deepSetting.written_by_code && !STOP_KEYS.has(deepSetting.key) ? { setting: deepSetting } : null;
  const activeEdit = edit ?? deepEdit;

  const term = lp.get("q").toLowerCase();
  const group = lp.get("group") || deepSetting?.group || groups[0]?.[0] || "";
  const rows = term ? all.filter((s) => s.key.toLowerCase().includes(term) || (s.description ?? "").toLowerCase().includes(term)) : all.filter((s) => s.group === group);

  const cols: Col<Setting>[] = [
    { key: "k", head: "Key", render: (s) => <Mono className="semi">{s.key}</Mono> },
    { key: "v", head: "Value", render: (s) => <span className="mono xs" style={{ wordBreak: "break-all" }} title={JSON.stringify(s.value)}>{preview(s.value)}</span> },
    { key: "d", head: "Description", render: (s) => <span className="muted xs">{s.description ?? "—"}</span> },
    { key: "u", head: "Updated", render: (s) => <When iso={s.updated_at} mode="ago" /> },
    {
      key: "a", head: <span className="sr-only">Actions</span>, render: (s) => s.written_by_code ? <Chip><Lock size={11} /> written by code</Chip>
        : STOP_KEYS.has(s.key) ? <Chip><Power size={11} /> header button</Chip>
        : <IconBtn sm label={`Edit ${s.key}`} onClick={() => setEdit({ setting: s })}><Edit3 size={14} /></IconBtn>,
    },
  ];

  const closeEdit = () => { setEdit(null); if (deepKey) keyParam.set(""); };

  return (
    <>
      <PageHead title="Settings" sub="Every threshold lives in the settings table · changes apply within 30 s and are announced on Telegram"
        actions={<Btn icon={<Clock size={15} />} onClick={() => setHistory(true)}>Change history</Btn>} />
      {q.error && !q.data ? <ErrorState error={q.error} retry={() => q.refetch()} /> : !q.data ? <SkeletonTable /> : (
        <div className="g collapse-md" style={{ gridTemplateColumns: "260px 1fr" }}>
          <nav className="card settings-side" aria-label="Setting groups" style={{ padding: 10, alignSelf: "start" }}>
            <div className="ph" style={{ fontSize: 10.5, fontWeight: 700, color: "var(--mute)", letterSpacing: ".06em", padding: "8px 12px" }}>GROUPS · {all.length} KEYS</div>
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {groups.map(([g, n]) => (
                <li key={g}>
                  <button type="button" className={cn("side-item", !term && g === group && "on")} aria-current={!term && g === group ? "true" : undefined}
                    onClick={() => { setSearch(""); lp.set("q", ""); lp.set("group", g); }}>
                    <span className="semi">{g}</span><span className="chip">{n}</span>
                  </button>
                </li>
              ))}
            </ul>
          </nav>

          <div className="col">
            <section aria-labelledby="sw-h">
              <div className="flabel" id="sw-h">Switches</div>
              <div className="g g3">
                <Card as="div" style={{ padding: 16 }}>
                  <div className="row"><div style={{ minWidth: 0 }}><div className="semi">Emergency stop</div><div className="mono muted xs" style={{ marginTop: 3 }}>control.emergency_stop</div></div>
                    <span className="right"><Toggle red on={byKey.get("control.emergency_stop")?.value === true} label="Emergency stop" /></span></div>
                  <div className="muted xs" style={{ marginTop: 8 }}>Use the <b>Stop posting / Resume</b> button in the header.</div>
                </Card>
                {SWITCHES.filter(([k]) => byKey.has(k)).map(([k, label]) => {
                  const s = byKey.get(k)!;
                  const on = s.value === true;
                  return (
                    <Card key={k} as="div" style={{ padding: 16 }}>
                      <div className="row"><div style={{ minWidth: 0 }}><div className="semi">{label}</div><div className="mono muted xs" style={{ marginTop: 3 }}>{k}</div></div>
                        <span className="right"><Toggle on={on} label={label} onChange={(v) => setEdit({ setting: s, proposed: v })} /></span></div>
                      {s.description && <div className="muted xs" style={{ marginTop: 8 }}>{s.description}</div>}
                    </Card>
                  );
                })}
              </div>
            </section>

            <Card>
              <CardHead title={term ? <>Search results <Badge tone="gray" dot={false}>{rows.length}</Badge></> : group}
                hint={term ? `Keys or descriptions containing “${term}”` : `${rows.length} keys · changes apply within 30 s · each change sends a Telegram alert`}
                actions={<SearchField label="Search settings" placeholder="Search keys" value={search} onChange={setSearch} width={220} />} />
              <FadeContent duration={0.3} trigger="mount" key={term || group}>
                <DataTable caption="Settings" cols={cols} rows={rows} rowKey={(s) => s.key} selected={deepKey || null} empty="No settings match." />
              </FadeContent>
            </Card>
            <p className="muted xs">The full settings reference is in docs/16-settings-reference.md. <Link className="pc" href="/events?type_prefix=setting.">Every change is in Events.</Link></p>
          </div>
        </div>
      )}
      <SettingEditModal req={activeEdit} onClose={closeEdit} />
      <HistoryDrawer open={history} onClose={() => setHistory(false)} />
    </>
  );
}

function HistoryDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const q = useApi<{ items: Event[] }>(open ? "admin/settings/history" : null, undefined, { interval: false });
  return (
    <Drawer open={open} onClose={onClose} title="Change history" sub="setting.changed and emergency stop events">
      {q.error ? <ErrorState error={q.error} /> : !q.data ? <div className="muted">Loading…</div> : (
        <EventFeed events={q.data.items} empty="No setting has been changed yet." />
      )}
    </Drawer>
  );
}
