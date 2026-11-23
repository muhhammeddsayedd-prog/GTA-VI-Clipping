"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { Clock, Globe, Monitor, Settings2 } from "lucide-react";

import { Popover } from "@/components/kit/overlays";
import { fmtDay, setDisplayZone, zoneLabel, zoneOffset, zonedParts } from "@/lib/format";
import { useSettings } from "@/lib/use-settings";
import { useDisplayZone } from "@/lib/use-zone";

/** Keeps the display-zone store in line with `settings.app.timezone`. Renders nothing. */
export function TimeZoneSync() {
  const s = useSettings();
  const zone = s.str("app.timezone");
  useEffect(() => {
    if (s.ready) setDisplayZone(zone);
  }, [s.ready, zone]);
  return null;
}

/* A one-second ticker shared by every clock on the page; server snapshot 0 (renders nothing). */
const listeners = new Set<() => void>();
let tick = 0;
let timer: ReturnType<typeof setInterval> | null = null;
function subscribe(cb: () => void) {
  listeners.add(cb);
  if (!timer) {
    tick = Date.now();
    timer = setInterval(() => { tick = Date.now(); listeners.forEach((l) => l()); }, 1000);
  }
  return () => {
    listeners.delete(cb);
    if (!listeners.size && timer) { clearInterval(timer); timer = null; }
  };
}
export function useSecondClock(): number {
  return useSyncExternalStore(subscribe, () => tick || Date.now(), () => 0);
}

const pad = (n: number) => String(n).padStart(2, "0");
function hms(now: number, zone: string, seconds = true): string {
  const p = zonedParts(now, zone);
  return `${pad(p.hour)}:${pad(p.minute)}${seconds ? `:${pad(p.second)}` : ""}`;
}

function browserZone(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; }
}

/** Live clock in the header: app zone with UTC beside it; click for the zones side by side. */
export function HeaderClock() {
  const zone = useDisplayZone();
  const now = useSecondClock();
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  if (!now) return null;
  const local = browserZone();
  const iso = new Date(now).toISOString();
  const rows = [
    { key: "app", icon: <Clock size={16} />, title: `${zoneLabel(zone)} · app time`, zone },
    { key: "utc", icon: <Globe size={16} />, title: "UTC · limits, caps and budgets", zone: "UTC" },
    ...(local !== zone && local !== "UTC" && local !== "Etc/UTC"
      ? [{ key: "local", icon: <Monitor size={16} />, title: `${zoneLabel(local)} · this browser`, zone: local }]
      : []),
  ];
  return (
    <div style={{ position: "relative" }}>
      <button type="button" data-pop-trigger className="hclock" aria-label={`App time ${hms(now, zone, false)} ${zoneLabel(zone)}, ${hms(now, "UTC", false)} UTC`}
        aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="hclock-main num">{hms(now, zone)}</span>
        <span className="hclock-zone">{zoneLabel(zone)}</span>
        {zone !== "UTC" && <span className="hclock-utc num hide-sm">{hms(now, "UTC", false)} UTC</span>}
      </button>
      <Popover open={open} onClose={close} label="Clocks" style={{ position: "absolute", right: 0, top: "calc(100% + 12px)", width: 300 }}>
        <div className="ph">Clocks</div>
        {rows.map((r) => (
          <div key={r.key} className="pi">
            <span className="pic">{r.icon}</span>
            <div style={{ minWidth: 0 }}>
              <div className="semi">{r.title}</div>
              <div className="t2">{fmtDay(iso, r.zone)} · {zoneOffset(r.zone, now)}</div>
            </div>
            <span className="right semi num">{hms(now, r.zone, false)}</span>
          </div>
        ))}
        <div className="sep" />
        <Link href="/settings#timezone" className="pi link" onClick={close}>
          <span className="pic"><Settings2 size={16} /></span>
          <div><div className="semi">Change app time zone</div><div className="t2">Settings → Time zone</div></div>
        </Link>
      </Popover>
    </div>
  );
}
