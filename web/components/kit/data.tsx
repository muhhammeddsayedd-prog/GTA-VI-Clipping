"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Fragment, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle, ArrowUpRight, Bolt, Check, ChevronLeft, ChevronRight, CircleDot, Compass, Film, Info, Radio, Send, Settings2, ShieldAlert, User, Video, X,
} from "lucide-react";

import CountUp from "@/components/reactbits/CountUp";
import { cn } from "@/lib/utils";
import { LEVEL_TONE } from "@/lib/status";
import { fmtTime, humanize, localTitle } from "@/lib/format";
import type { Event, Json, Trace as TraceT } from "@/lib/api/types";
import { Badge, Card } from "./primitives";

/* ---------- table ---------- */
export interface Col<T> {
  key: string;
  head: ReactNode;
  render: (row: T) => ReactNode;
  align?: "right";
  width?: number | string;
  /**
   * Phone layout (rows become cards): "primary" is the card title (default: first column), "lead" sits before it
   * (e.g. a tier chip or thumbnail), "aside" goes top-right (default: a status-like column), "hide" is left out.
   * Everything else is shown as label/value pairs.
   */
  /** "meta" forces the column into the card's details (they are capped at MOBILE_META_MAX otherwise). */
  mobile?: "primary" | "lead" | "aside" | "meta" | "hide";
}

const ASIDE_KEYS = /^(status|state|st|decision|result|verdict|severity|sev|level)$/i;
const MOBILE_META_MAX = 4;

function mobileRoles<T>(cols: Col<T>[]) {
  const primary = cols.find((c) => c.mobile === "primary") ?? cols.find((c) => c.mobile !== "lead" && c.mobile !== "hide") ?? cols[0];
  const lead = cols.filter((c) => c.mobile === "lead");
  const aside = cols.find((c) => c.mobile === "aside") ?? cols.find((c) => c !== primary && !c.mobile && ASIDE_KEYS.test(c.key));
  const rest = cols.filter((c) => c !== primary && c !== aside && !lead.includes(c) && c.mobile !== "hide");
  const forced = rest.filter((c) => c.mobile === "meta");
  const fill = rest.filter((c) => c.mobile !== "meta").slice(0, Math.max(0, MOBILE_META_MAX - forced.length));
  const meta = rest.filter((c) => forced.includes(c) || fill.includes(c)); // keep column order
  return { primary, lead, aside, meta };
}

export function DataTable<T>({ cols, rows, rowKey, href, onRow, selected, dense, caption, empty = "Nothing here yet." }: {
  cols: Col<T>[]; rows: T[]; rowKey: (r: T) => string | number; href?: (r: T) => string | null; selected?: string | number | null;
  /** Row click handler for rows that open a drawer instead of navigating. */
  onRow?: (r: T) => void;
  dense?: boolean; caption?: string; empty?: ReactNode;
}) {
  const router = useRouter();
  /** Click handler for a row (or undefined when rows are not interactive). Clicks on inner links/buttons pass through. */
  const clickFor = (r: T, to: string | null) => (to || onRow)
    ? (e: React.MouseEvent) => {
      if ((e.target as HTMLElement).closest("a,button")) return;
      if (onRow) onRow(r); else router.push(to!);
    }
    : undefined;
  if (!rows.length) return <div className="muted small" style={{ padding: "26px 4px", textAlign: "center" }} role="status">{empty}</div>;
  const m = mobileRoles(cols);
  return (
    <>
    <ul className="mcards" aria-label={caption}>
      {rows.map((r) => {
        const k = rowKey(r);
        const to = href?.(r) ?? null;
        return (
          <li key={k} className={cn("mcard", (to || onRow) && "link", selected === k && "sel")} onClick={clickFor(r, to)}>
            <div className="mc-head">
              {m.lead.map((c) => <div key={c.key} className="mc-lead">{c.render(r)}</div>)}
              <div className="mc-primary">
                {to && <Link href={to} className="sr-only">Open</Link>}
                {m.primary.render(r)}
              </div>
              {m.aside && <div className="mc-aside">{m.aside.render(r)}</div>}
              {to && <ChevronRight size={16} className="mc-chev" aria-hidden />}
            </div>
            {m.meta.length > 0 && (
              <dl className="mc-meta">
                {m.meta.map((c) => (
                  <div key={c.key} className={c.align === "right" ? "num" : undefined}>
                    <dt>{c.head}</dt><dd>{c.render(r)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </li>
        );
      })}
    </ul>
    <div className="scroll-x dt-table">
      <table className={cn("t", dense && "dense")}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>{cols.map((c) => <th key={c.key} scope="col" className={c.align === "right" ? "r" : undefined} style={{ width: c.width }}>{c.head}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const k = rowKey(r);
            const to = href?.(r) ?? null;
            return (
              <tr key={k} className={cn((to || onRow) && "link", selected === k && "sel")} onClick={clickFor(r, to)}>
                {cols.map((c, i) => (
                  <td key={c.key} className={c.align === "right" ? "r" : undefined}>
                    {i === 0 && to && <Link href={to} className="sr-only">Open</Link>}
                    {c.render(r)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
    </>
  );
}

/** Cursor pager. `cursors` is the stack of cursors used so far (page 1 = null). */
export function Pager({ shown, total, cursors, next, onPage, noun = "rows" }: {
  shown: number; total?: number; cursors: (string | null)[]; next: string | null; onPage: (stack: (string | null)[]) => void; noun?: string;
}) {
  const page = cursors.length;
  return (
    <div className="tfoot">
      <span>Showing {shown}{total !== undefined ? ` of ${total.toLocaleString("en-US")}` : ""} {noun}</span>
      <div className="pager" role="navigation" aria-label="Pages">
        <button type="button" className="ibtn sm" aria-label="Previous page" disabled={page <= 1} onClick={() => onPage(cursors.slice(0, -1))} style={{ opacity: page <= 1 ? 0.4 : 1 }}><ChevronLeft size={14} /></button>
        <span className="on" aria-current="page">{page}</span>
        <button type="button" className="ibtn sm" aria-label="Next page" disabled={!next} onClick={() => next && onPage([...cursors, next])} style={{ opacity: next ? 1 : 0.4 }}><ChevronRight size={14} /></button>
      </div>
    </div>
  );
}

/* ---------- KPI tile ---------- */
export function Kpi({ label, value, decimals = 0, prefix, suffix, unit, delta, icon, iconTone, chart, valueText }: {
  label: string; value?: number | null; decimals?: number; prefix?: string; suffix?: string; unit?: string; delta?: ReactNode;
  icon: ReactNode; iconTone?: "red" | "green" | "amber"; chart?: ReactNode; valueText?: ReactNode;
}) {
  return (
    <Card as="div">
      <div className="kpi">
        <div className="lbl">{label}<span className={cn("cic", iconTone)} aria-hidden>{icon}</span></div>
        <div className="val num">
          {valueText ?? (value === null || value === undefined ? <span className="muted">—</span> : (
            <>
              {prefix && <small>{prefix}</small>}
              <CountUp to={value} decimals={decimals} separator="," duration={1.1} trigger="load" />
              {suffix}
            </>
          ))}
          {unit && <sub>{unit}</sub>}
        </div>
        {delta && <div className="delta">{delta}</div>}
        {chart && <div className="chart">{chart}</div>}
      </div>
    </Card>
  );
}

export function Delta({ v, good = true, suffix = "" }: { v: number; good?: boolean; suffix?: string }) {
  const up = v >= 0;
  const ok = up === good;
  return <b className={ok ? "up" : "down"}>{up ? "↑" : "↓"} {Math.abs(v).toLocaleString("en-US")}{suffix}</b>;
}

/* ---------- trace bar ---------- */
export function TraceBar({ trace, on }: { trace: TraceT; on: "creator" | "live_session" | "video" | "spike" | "clip" | "posts" }) {
  const steps: { key: typeof on; icon: ReactNode; k: string; v: string; href: string | null }[] = [
    { key: "creator", icon: <User size={15} />, k: "Creator", v: trace.creator?.label ?? "—", href: trace.creator ? `/creators/${trace.creator.id}` : null },
    trace.live_session
      ? { key: "live_session", icon: <Radio size={15} />, k: "Live session", v: trace.live_session.label, href: `/live/${trace.live_session.id}` }
      : { key: "video", icon: <Video size={15} />, k: "Video", v: trace.video?.label ?? "—", href: trace.video ? `/videos/${trace.video.id}` : null },
    { key: "spike", icon: <Bolt size={15} />, k: "Spike", v: trace.spike?.label ?? "—", href: trace.spike ? `/spikes/${trace.spike.id}` : null },
    { key: "clip", icon: <Film size={15} />, k: "Clip", v: trace.clip?.label ?? "—", href: trace.clip ? `/clips/${trace.clip.id}` : null },
    {
      key: "posts", icon: <Send size={15} />, k: "Posts",
      v: trace.posts.length ? summarize(trace.posts.map((p) => p.status ?? "")) : "none",
      href: trace.posts.length === 1 ? `/posts/${trace.posts[0].id}` : trace.clip ? `/posts?clip_id=${trace.clip.id}` : null,
    },
  ];
  return (
    <nav className="trace" aria-label="Where this came from and where it went">
      {steps.map((s, i) => (
        <Fragment key={s.key + i}>
          {i > 0 && <span className="arr" aria-hidden><ChevronRight size={16} /></span>}
          {s.href && s.key !== on ? (
            <Link href={s.href} className="st"><span className="ico">{s.icon}</span><div style={{ minWidth: 0 }}><div className="k">{s.k}</div><div className="v">{s.v}</div></div></Link>
          ) : (
            <div className={cn("st", s.key === on && "on")} aria-current={s.key === on ? "page" : undefined}>
              <span className="ico">{s.icon}</span><div style={{ minWidth: 0 }}><div className="k">{s.k}</div><div className="v">{s.v}</div></div>
            </div>
          )}
        </Fragment>
      ))}
    </nav>
  );
}
function summarize(statuses: string[]): string {
  const c: Record<string, number> = {};
  for (const s of statuses) c[s] = (c[s] ?? 0) + 1;
  return Object.entries(c).map(([k, n]) => `${n} ${humanize(k)}`).join(" · ");
}

/* ---------- event feed ---------- */
const TYPE_ICON: [RegExp, ReactNode, string][] = [
  [/^post\.(failed|remove)/, <X key="x" size={15} />, "red"],
  [/^post\./, <Send key="s" size={15} />, "green"],
  [/^(spike|live)\./, <Bolt key="b" size={15} />, "purple"],
  [/^clip\.rejected|render_failed/, <X key="x2" size={15} />, "red"],
  [/^clip\./, <Film key="f" size={15} />, "purple"],
  [/^discovery\./, <Compass key="c" size={15} />, "blue"],
  [/^health\.recovered/, <Check key="ok" size={15} />, "green"],
  [/^(health|account)\./, <AlertTriangle key="a" size={15} />, "amber"],
  [/^(takedown|guardian)\./, <ShieldAlert key="g" size={15} />, "red"],
  [/^(setting|control)\./, <Settings2 key="st" size={15} />, "gray"],
];
export function eventLook(e: Event): { icon: ReactNode; tone: string } {
  for (const [re, icon, tone] of TYPE_ICON) if (re.test(e.type)) return { icon, tone: e.level === "critical" || e.level === "error" ? "red" : tone };
  return { icon: e.level === "info" ? <Info size={15} /> : <CircleDot size={15} />, tone: LEVEL_TONE[e.level] ?? "gray" };
}

export function EventFeed({ events, onOpen, empty = "No events yet." }: { events: Event[]; onOpen?: (e: Event) => void; empty?: string }) {
  if (!events.length) return <div className="muted small" role="status">{empty}</div>;
  return (
    <ul className="feed" style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {events.map((e) => {
        const { icon, tone } = eventLook(e);
        const body = (
          <>
            <span className={cn("ficon", tone)} aria-hidden>{icon}</span>
            <div style={{ minWidth: 0 }}>
              <div className="t1">{e.message || humanize(e.type)}</div>
              <div className="t2"><span className="mono">{e.type}</span>{e.service ? ` · ${e.service}` : ""}</div>
            </div>
            <time className="time" dateTime={e.created_at} title={localTitle(e.created_at)}>{fmtTime(e.created_at)}</time>
          </>
        );
        return (
          <li key={e.id} className="fi">
            {onOpen ? <button type="button" onClick={() => onOpen(e)} style={{ all: "unset", display: "flex", gap: 12, width: "100%", cursor: "pointer" }}>{body}</button> : body}
          </li>
        );
      })}
    </ul>
  );
}

/* ---------- steps ---------- */
export function Steps({ steps }: { steps: { state: "done" | "run" | "fail" | ""; title: ReactNode; sub?: ReactNode; time?: ReactNode; icon?: ReactNode }[] }) {
  return (
    <ol className="steps" style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {steps.map((s, i) => (
        <li key={i} className={cn("step", s.state)}>
          <span className="sc" aria-hidden>{s.icon ?? (s.state === "fail" ? <X size={14} strokeWidth={2.4} /> : s.state === "done" ? <Check size={14} strokeWidth={2.4} /> : <CircleDot size={14} />)}</span>
          <div className="grow">
            <div className="row"><div className="t1">{s.title}</div>{s.time && <span className="right muted xs">{s.time}</span>}</div>
            {s.sub && <div className="t2">{s.sub}</div>}
          </div>
        </li>
      ))}
    </ol>
  );
}

/* ---------- JSON ---------- */
function esc(s: string) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
export function highlightJson(value: unknown): string {
  const text = JSON.stringify(value, null, 2) ?? "null";
  return esc(text).replace(/("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi, (m, str, colon) => {
    if (str) return colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`;
    return `<span class="n">${m}</span>`;
  });
}
export function JsonBlock({ value, maxHeight = 420, label = "JSON" }: { value: unknown; maxHeight?: number; label?: string }) {
  const html = useMemo(() => highlightJson(value), [value]);
  return <pre className="code" style={{ maxHeight, margin: 0 }} aria-label={label} dangerouslySetInnerHTML={{ __html: html }} />;
}

/* ---------- hours strip ---------- */
export function Hours({ cells, labels = true }: { cells: { cls: string; title: string }[]; labels?: boolean }) {
  return (
    <div>
      <div className="hours">{cells.map((c, i) => <i key={i} className={c.cls} title={c.title} />)}</div>
      {labels && <div className="hl" aria-hidden>{cells.map((_, h) => <span key={h} className={h % 3 ? "hl-minor" : undefined}>{String(h).padStart(2, "0")}</span>)}</div>}
    </div>
  );
}

/* ---------- gate/cap check row ---------- */
export function CheckRow({ state, name, value, limit }: { state: "ok" | "no" | "na"; name: ReactNode; value: ReactNode; limit?: ReactNode }) {
  return (
    <div className={cn("check", state === "no" && "fail")}>
      <span className={state} aria-label={state === "ok" ? "passed" : state === "no" ? "failed" : "not checked"}>
        {state === "ok" ? <Check size={13} strokeWidth={2.6} /> : state === "no" ? <X size={13} strokeWidth={2.6} /> : <Info size={13} />}
      </span>
      <span>{name}</span><span className="vv">{value}</span><span className="lim">{limit}</span>
    </div>
  );
}

/* ---------- thumbnails (generated scene, no third-party imagery) ---------- */
const SCENES = [["#FF7E5F", "#7B2FF7", "#1A1033"], ["#00C9FF", "#3F2B96", "#0B0F2A"], ["#F7971E", "#E5484D", "#2A0F1A"], ["#43E97B", "#2B5876", "#081A1F"],
  ["#FC5C7D", "#6A82FB", "#14122E"], ["#FDC830", "#F37335", "#2B1406"], ["#8E2DE2", "#4A00E0", "#0E0624"], ["#11998E", "#38EF7D", "#062019"]];
export function seedOf(s: string): number { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h; }
export function Scene({ seed }: { seed: number }) {
  const [a, b, c] = SCENES[seed % SCENES.length];
  return (
    <div className="scene" style={{ background: `linear-gradient(180deg,${a} 0%,${b} 48%,${c} 100%)` }}>
      <svg viewBox="0 0 90 160" preserveAspectRatio="none" style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }} aria-hidden>
        <circle cx={30 + ((seed * 13) % 35)} cy={52} r={15} fill="#fff" opacity={0.28} />
        <path d="M0 98 L0 78 L8 78 L8 66 L15 66 L15 82 L22 82 L22 58 L30 58 L30 74 L38 74 L38 86 L46 86 L46 62 L52 56 L58 62 L58 80 L66 80 L66 70 L74 70 L74 84 L82 84 L82 72 L90 72 L90 98 Z" fill={c} opacity={0.85} />
        <path d="M0 160 L36 98 L54 98 L90 160 Z" fill="#000" opacity={0.35} />
        <path d="M45 104 L45 112 M45 120 L45 132 M45 140 L45 158" stroke="#fff" strokeWidth={1.4} opacity={0.5} />
      </svg>
    </div>
  );
}
/**
 * 9:16 clip tile. `src` is a real image: a frame of the rendered clip ("frame") or the 16:9 thumbnail of the
 * source video ("source", shown whole over a blurred copy of itself). Without one, or if it fails to load,
 * a generated scene stands in.
 */
export function Thumb({ seed, hook, dur, live, width = "100%", children, src, kind = "frame" }: {
  seed: string; hook?: string | null; dur?: string; live?: boolean; width?: number | string; children?: ReactNode;
  src?: string | null; kind?: "frame" | "source" | null;
}) {
  const [broken, setBroken] = useState<string | null>(null);
  const img = src && broken !== src ? src : null;
  return (
    <div className={`thumb${img ? " has-img" : ""}`} style={{ width }}>
      {img ? (
        kind === "source" ? (
          <>
            <img className="thumb-bg" src={img} alt="" aria-hidden loading="lazy" decoding="async" referrerPolicy="no-referrer" />
            <img className="thumb-src" src={img} alt="" loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setBroken(img)} />
          </>
        ) : (
          <img className="thumb-img" src={img} alt="" loading="lazy" decoding="async" onError={() => setBroken(img)} />
        )
      ) : <Scene seed={seedOf(seed)} />}
      <div className="tl">{live && <span className="livebadge">LIVE · JUST HAPPENED</span>}</div>
      {dur && <div className="tr"><span className="dur">{dur}</span></div>}
      {hook && <div className="ov"><div className="hook">{hook}</div></div>}
      {children}
    </div>
  );
}

/* ---------- misc ---------- */
export function LevelBadge({ level }: { level: string }) { return <Badge tone={LEVEL_TONE[level] ?? "gray"}>{level}</Badge>; }

export function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return <a href={href} target="_blank" rel="noopener noreferrer" className="pc semi row" style={{ gap: 4, display: "inline-flex" }}>{children}<ArrowUpRight size={12} /></a>;
}

export function isJsonObject(v: Json | unknown): v is Record<string, unknown> { return !!v && typeof v === "object" && !Array.isArray(v); }
