"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Activity, AlertTriangle, Bell, ChevronDown, Clock, Database, LayoutGrid, LogOut, Menu, Play, Power, Search, Server, Shield, Film, X } from "lucide-react";

import ShinyText from "@/components/reactbits/ShinyText";
import { Popover } from "@/components/kit/overlays";
import { Avatar, Badge, Btn } from "@/components/kit/primitives";
import { useApi, POLL } from "@/lib/api/client";
import type { Alert, Health, Status } from "@/lib/api/types";
import { fmtAgo, fmtDuration } from "@/lib/format";
import { SEVERITY_TONE } from "@/lib/status";
import { cn } from "@/lib/utils";
import { useNow } from "@/lib/use-now";
import { CommandPalette } from "./CommandPalette";
import { Logo } from "./Logo";
import { NAV, activeOf } from "./nav";
import { ResumeModal, StopModal } from "./StopControls";

export interface ShellProps {
  env: "production" | "staging" | "dev";
  email: string;
  mock: boolean;
  signOut: () => Promise<void>;
  children: ReactNode;
}

export function Shell({ env, email, mock, signOut, children }: ShellProps) {
  const pathname = usePathname();
  const { group, item } = activeOf(pathname);
  const [openGroup, setOpenGroup] = useState<string | null>(null);
  const [pop, setPop] = useState<null | "alerts" | "profile">(null);
  const [palette, setPalette] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const [resumeOpen, setResumeOpen] = useState(false);
  const [sheet, setSheet] = useState(false);

  const status = useApi<Status>("status", undefined, { interval: POLL.status });
  const health = useApi<Health>("admin/health", undefined, { interval: POLL.list });
  const alerts = useApi<{ items: Alert[] }>(pop === "alerts" ? "admin/alerts" : null, { limit: 6 }, { interval: POLL.list });

  const mode = status.data?.posting_mode ?? null;
  // Workers leave one heartbeat row per process, so old rows of restarted workers stay stale forever.
  // Count a service as down only when none of its heartbeats is fresh.
  const stale = (() => {
    const fresh = new Map<string, boolean>();
    for (const h of health.data?.heartbeats ?? []) {
      if (h.name.startsWith("trigger:")) continue;
      const svc = h.name.match(/^(worker-[a-z]+)/)?.[1] ?? h.name;
      fresh.set(svc, (fresh.get(svc) ?? false) || !h.stale);
    }
    return [...fresh.values()].filter((ok) => !ok).length;
  })();
  const incidents = (health.data?.open_incidents.length ?? 0) + stale;

  // Close menus when the page changes (state adjusted during render, not in an effect).
  const [lastPath, setLastPath] = useState(pathname);
  if (pathname !== lastPath) {
    setLastPath(pathname);
    setOpenGroup(null);
    setPop(null);
    setSheet(false);
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((p) => !p); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const closePop = useCallback(() => setPop(null), []);
  const closeGroup = useCallback(() => setOpenGroup(null), []);

  const envChip = env === "production" ? <span className="envchip">PROD</span> : env === "staging" ? <span className="envchip stg">STAGING</span> : <span className="envchip" style={{ color: "var(--ink2)" }}>DEV</span>;

  return (
    <>
      <a href="#main" className="sr-only" style={{ position: "absolute" }}>Skip to content</a>
      <div className="app">
        {env === "staging" && (
          <div className="band stg" role="note"><AlertTriangle size={15} style={{ flex: "none" }} /><span>STAGING · GTA V · posts go to the real accounts as <b>private</b> · Instagram posting is off · Telegram commands are production-only</span></div>
        )}
        {mock && (
          <div className="band mock" role="note"><Database size={15} style={{ flex: "none" }} /><span>SAMPLE DATA · no control API is configured, so this console shows generated data. Nothing here is real.</span></div>
        )}

        <header className="top">
          <button type="button" className="ibtn mobile-only" aria-label="Open menu" onClick={() => setSheet(true)}><Menu size={18} /></button>
          <Link href="/" className="brand" aria-label="6 Stars Clips console home">
            <Logo />
            <div><b>6 STARS</b><span>Clips control console</span></div>
          </Link>
          {envChip}
          <nav className="navpill" aria-label="Main">
            {NAV.map((g) => {
              const on = group?.key === g.key;
              const Icon = g.icon;
              if (g.href) {
                return <Link key={g.key} href={g.href} className={cn(on && "on")} aria-current={on ? "page" : undefined}>{on && <Icon size={14} className="nav-ic" />}{g.label}</Link>;
              }
              const label = on && item ? item.label : g.label;
              return (
                <div key={g.key} style={{ position: "relative" }}>
                  <button type="button" data-pop-trigger className={cn(on && "on", openGroup === g.key && !on && "open")} aria-expanded={openGroup === g.key} aria-haspopup="menu"
                    onClick={() => setOpenGroup((o) => (o === g.key ? null : g.key))}>
                    {on && <Icon size={14} className="nav-ic" />}{label}<ChevronDown size={13} />
                  </button>
                  <Popover open={openGroup === g.key} onClose={closeGroup} label={g.label} style={{ position: "absolute", top: "calc(100% + 12px)", left: -8, width: 320 }}>
                    <div className="ph">{g.label}</div>
                    <ul role="menu" style={{ listStyle: "none", margin: 0, padding: 0 }}>
                      {g.items!.map((it) => {
                        const I = it.icon;
                        const cur = item?.href === it.href;
                        return (
                          <li key={it.href} role="none">
                            <Link role="menuitem" href={it.href} className={cn("pi link", cur && "on")} aria-current={cur ? "page" : undefined}>
                              <span className="pic"><I size={16} /></span>
                              <div><div className="semi">{it.label}</div><div className="t2">{it.hint}</div></div>
                            </Link>
                          </li>
                        );
                      })}
                    </ul>
                  </Popover>
                </div>
              );
            })}
          </nav>

          <div className="tright">
            <ModePill mode={mode} />
            <button type="button" className="ibtn hide-sm" aria-label="Search (Ctrl K)" title="Search (Ctrl K)" onClick={() => setPalette(true)}><Search size={17} /></button>
            <Link href="/health" className="ibtn hide-sm" aria-label={`Health: ${incidents} open incident${incidents === 1 ? "" : "s"}`} title="Health">
              <Activity size={17} />{incidents > 0 && <em>{incidents}</em>}
            </Link>
            <div style={{ position: "relative" }}>
              <button type="button" data-pop-trigger className="ibtn" aria-label="Recent alerts" aria-expanded={pop === "alerts"} onClick={() => setPop((p) => (p === "alerts" ? null : "alerts"))}><Bell size={17} /></button>
              <Popover open={pop === "alerts"} onClose={closePop} label="Recent alerts" style={{ position: "absolute", right: 0, top: "calc(100% + 12px)", width: "min(420px, calc(100vw - 24px))", padding: 16 }}>
                <div className="row" style={{ padding: "0 4px 8px" }}><h3 style={{ margin: 0, fontSize: 15 }}>Alerts</h3><span className="right muted xs">same messages as Telegram</span></div>
                <ul className="feed" style={{ listStyle: "none", margin: 0, padding: 0 }}>
                  {(alerts.data?.items ?? []).map((a) => (
                    <li key={a.id} className="fi" style={{ padding: 12 }}>
                      <span className={cn("ficon", a.severity === "critical" ? "red" : a.severity === "warn" ? "amber" : "blue")}>{a.severity === "info" ? <Bell size={15} /> : <AlertTriangle size={15} />}</span>
                      <div style={{ minWidth: 0 }}><div className="t1">{a.title}</div>{a.body && <div className="t2 clamp2">{a.body}</div>}
                        <div style={{ marginTop: 6 }}><Badge tone={SEVERITY_TONE[a.severity]}>{a.severity}</Badge> <Badge tone={a.sent_at ? "green" : "red"} dot={false}>{a.sent_at ? "sent to Telegram" : "not delivered"}</Badge></div></div>
                      <span className="time">{fmtAgo(a.created_at)}</span>
                    </li>
                  ))}
                  {alerts.isLoading && <li className="muted small" style={{ padding: 12 }}>Loading…</li>}
                  {alerts.data && !alerts.data.items.length && <li className="muted small" style={{ padding: 12 }}>No alerts yet.</li>}
                </ul>
                <div className="row" style={{ padding: "10px 4px 0" }}><span className="muted xs">Nothing waits for you here</span><Link href="/alerts" className="right pc semi small">All alerts →</Link></div>
              </Popover>
            </div>
            {mode === "stopped" ? (
              <button type="button" className="stopbtn resume" onClick={() => setResumeOpen(true)}><Play size={13} /><span className="stop-text">Resume</span></button>
            ) : (
              <button type="button" className="stopbtn" onClick={() => setStopOpen(true)}><Power size={14} strokeWidth={2.2} /><span className="stop-text">Stop posting</span></button>
            )}
            <div style={{ position: "relative" }}>
              <button type="button" data-pop-trigger aria-label="Account menu" aria-expanded={pop === "profile"} onClick={() => setPop((p) => (p === "profile" ? null : "profile"))} style={{ border: 0, padding: 0, background: "none", borderRadius: "50%" }}>
                <Avatar name="Operator" size={40} i={0} />
              </button>
              <Popover open={pop === "profile"} onClose={closePop} label="Account" style={{ position: "absolute", right: 0, top: "calc(100% + 12px)", width: 300 }}>
                <div className="row" style={{ padding: 12 }}><Avatar name="Operator" size={44} i={0} /><div style={{ minWidth: 0 }}><div className="semi">Operator</div><div className="muted xs ellipsis">{email}</div></div></div>
                <div className="sep" />
                <div className="pi"><span className="pic"><Server size={16} /></span><div><div className="semi">Environment</div><div className="t2">{env === "production" ? "Production" : env === "staging" ? "Staging" : "Local development"}</div></div><span className="right">{envChip}</span></div>
                <div className="pi"><span className="pic"><Clock size={16} /></span><div><div className="semi">Times shown in UTC</div><div className="t2">Hover any time for your local time</div></div></div>
                <div className="sep" />
                <form action={signOut}>
                  <button type="submit" className="pi link" style={{ width: "100%", border: 0, background: "none", color: "var(--red)", textAlign: "left" }}>
                    <span className="pic" style={{ background: "var(--rs)", color: "var(--red)" }}><LogOut size={16} /></span><span className="semi">Sign out</span>
                  </button>
                </form>
              </Popover>
            </div>
          </div>
        </header>

        <ModeBanner status={status.data} onResume={() => setResumeOpen(true)} />

        <main id="main" tabIndex={-1} style={{ outline: "none" }}>{children}</main>
      </div>

      <nav className="mbar mobile-bar" aria-label="Quick">
        {[{ href: "/", icon: LayoutGrid, label: "Overview" }, { href: "/clips", icon: Film, label: "Clips" }, { href: "/guardian", icon: Shield, label: "Guardian" }, { href: "/health", icon: Activity, label: "Health" }].map((l) => {
          const I = l.icon;
          const on = l.href === "/" ? pathname === "/" : pathname.startsWith(l.href);
          return <Link key={l.href} href={l.href} className={on ? "on" : ""} aria-label={l.label} aria-current={on ? "page" : undefined}><I size={19} /></Link>;
        })}
        <a href="#menu" onClick={(e) => { e.preventDefault(); setSheet(true); }} aria-label="All pages"><Menu size={19} /></a>
      </nav>

      {sheet && <MobileSheet onClose={() => setSheet(false)} onSearch={() => { setSheet(false); setPalette(true); }} signOut={signOut} />}
      <CommandPalette open={palette} onClose={() => setPalette(false)} />
      <StopModal open={stopOpen} onClose={() => setStopOpen(false)} />
      <ResumeModal open={resumeOpen} onClose={() => setResumeOpen(false)} reason={status.data?.stop_reason ?? null} />
    </>
  );
}

function ModePill({ mode }: { mode: Status["posting_mode"] | null }) {
  if (!mode) return <div className="mode" aria-label="Posting mode loading"><i style={{ background: "var(--mute2)", boxShadow: "none" }} /><span className="mode-text">…</span></div>;
  const map = { live: ["live", "LIVE"], dry_run: ["dry", "DRY RUN"], stopped: ["stopped", "STOPPED"] } as const;
  const [cls, label] = map[mode];
  return (
    <div className={cn("mode", cls)} role="status" aria-label={`Posting mode: ${label}`}>
      <i />
      <span className="mode-text">{mode === "live" ? <ShinyText text={label} color="#12A150" shineColor="#7CF0AE" speed={3} /> : label}</span>
    </div>
  );
}

function ModeBanner({ status, onResume }: { status: Status | undefined; onResume: () => void }) {
  const now = useNow();
  if (!status) return null;
  if (status.posting_mode === "stopped") {
    return (
      <div className="banner red" role="alert" style={{ marginTop: 18 }}>
        <Power size={18} strokeWidth={2.2} />
        <div className="grow"><b>Posting is stopped.</b> Reason: <b>{status.stop_reason ?? "none given"}</b> · discovery, recording and rendering keep running.</div>
        <Btn sm href="/guardian">See strikes</Btn>
        <Btn sm kind="green" icon={<Play size={12} />} onClick={onResume}>Resume posting</Btn>
      </div>
    );
  }
  if (status.posting_mode === "dry_run") {
    const left = status.launch_at && now ? (Date.parse(status.launch_at) - now) / 1000 : null;
    return (
      <div className="banner amber" role="status" style={{ marginTop: 18 }}>
        <Clock size={18} />
        <div className="grow"><b>Dry run.</b> Clips are picked and logged as <b>skipped</b>; nothing is uploaded.{left && left > 0 ? <> Launch in <b>{fmtDuration(left)}</b>.</> : null}</div>
        <Btn sm href="/settings?q=launch.">launch.* settings</Btn>
      </div>
    );
  }
  return null;
}

function MobileSheet({ onClose, onSearch, signOut }: { onClose: () => void; onSearch: () => void; signOut: () => Promise<void> }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <>
      <div className="overlay fixed" onClick={onClose} aria-hidden />
      <div role="dialog" aria-modal="true" aria-label="All pages" className="sheet">
        <div style={{ width: 44, height: 5, borderRadius: 5, background: "var(--line2)", margin: "0 auto 12px" }} />
        <div className="row"><h2 style={{ margin: 0, fontSize: 20 }}>All pages</h2><button type="button" className="right ibtn sm" aria-label="Close" onClick={onClose}><X size={15} /></button></div>
        <button type="button" className="field" style={{ width: "100%", marginTop: 12, cursor: "pointer" }} onClick={onSearch}><Search size={15} /><span className="ph" style={{ color: "var(--mute)" }}>Search or paste an id</span></button>
        {[
          { key: "general", label: "General", items: NAV.filter((g) => g.href).map((g) => ({ href: g.href!, label: g.label, icon: g.icon })) },
          ...NAV.filter((g) => g.items).map((g) => ({ key: g.key, label: g.label, items: g.items! })),
        ].map((g) => (
          <div key={g.key}>
            <div className="sheet-h">{g.label}</div>
            <div className="sheet-grid">
              {g.items.map((it) => {
                const I = it.icon;
                return (
                  <Link key={it.href} href={it.href} className="sheet-tile" onClick={onClose}>
                    <span className="pic"><I size={17} /></span><span className="semi">{it.label}</span>
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
        <form action={signOut}><button type="submit" className="row" style={{ marginTop: 18, color: "var(--red)", gap: 8, border: 0, background: "none" }}><LogOut size={16} /><span className="semi">Sign out</span></button></form>
        <div style={{ height: 20 }} />
      </div>
    </>
  );
}
