"use client";

import Link from "next/link";
import { useState, type ComponentProps, type CSSProperties, type ReactNode } from "react";
import { AlertTriangle, ArrowLeft, Info, RefreshCw } from "lucide-react";

import { cn } from "@/lib/utils";
import { toneOf, type Tone } from "@/lib/status";
import { fmtAgo, fmtTime, humanize, localTitle } from "@/lib/format";
import type { PostPlatform, Platform } from "@/lib/api/types";

/* ---------- badges & chips ---------- */
export function Badge({ children, status, tone, dot = true, className }: { children?: ReactNode; status?: string | null; tone?: Tone; dot?: boolean; className?: string }) {
  const t = tone ?? toneOf(status);
  return <span className={cn("b", t, !dot && "nodot", className)}>{children ?? humanize(status)}</span>;
}

export function StatusBadge({ status }: { status: string | null | undefined }) {
  return <Badge status={status ?? "n/a"} />;
}

export function TierChip({ tier }: { tier: string | null | undefined }) {
  const t = !tier || tier === "rejected" ? "R" : tier;
  return <span className={cn("tier", t)} title={tier ? `Tier ${tier}` : "No tier"}>{t === "R" ? "–" : t}</span>;
}

export function Chip({ children, accent, className }: { children: ReactNode; accent?: boolean; className?: string }) {
  return <span className={cn("chip", accent && "p", className)}>{children}</span>;
}

const GLYPH: Record<string, string> = { tiktok: "♪", instagram: "◎", youtube: "▶", twitch: "◆" };
const PLAT_NAME: Record<string, string> = { tiktok: "TikTok", instagram: "Instagram", youtube: "YouTube", twitch: "Twitch" };
/** Official platform marks live in /public/brand; unknown platforms fall back to a coloured glyph. */
const LOGO: Record<string, string> = { tiktok: "/brand/tiktok.png", instagram: "/brand/instagram.png", youtube: "/brand/youtube.png", twitch: "/brand/twitch.svg" };
export function Plat({ p, lg }: { p: Platform | PostPlatform | string; lg?: boolean }) {
  const logo = LOGO[p];
  return (
    <span className={cn("plat", p, lg && "lg", logo && "logo")} role="img" aria-label={PLAT_NAME[p] ?? p} title={PLAT_NAME[p] ?? p}>
      {logo ? <img src={logo} alt="" width={lg ? 38 : 22} height={lg ? 38 : 22} loading="lazy" decoding="async" /> : GLYPH[p] ?? "•"}
    </span>
  );
}
export const platformName = (p: string) => PLAT_NAME[p] ?? p;

const AV = ["#7C6EFF,#4B3BDF", "#FF8A65,#E5484D", "#34D399,#059669", "#FBBF24,#D97706", "#60A5FA,#2563EB", "#F472B6,#DB2777", "#A78BFA,#7C3AED", "#2DD4BF,#0D9488", "#94A3B8,#475569"];
/** Creator photo when the platform gave us one; initials on a gradient otherwise (or if the image fails). */
export function Avatar({ name, size = 36, i, src }: { name: string; size?: number; i?: number; src?: string | null }) {
  const [broken, setBroken] = useState<string | null>(null);
  if (src && broken !== src) {
    return (
      <span aria-hidden className="avatar photo" style={{ width: size, height: size }}>
        <img src={src} alt="" width={size} height={size} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setBroken(src)} />
      </span>
    );
  }
  const idx = i ?? [...name].reduce((a, c) => a + c.charCodeAt(0), 0) % AV.length;
  const [a, b] = AV[idx].split(",");
  const ini = name.replace(/[_.@]/g, " ").trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
  return (
    <span aria-hidden className="avatar" style={{ width: size, height: size, fontSize: Math.max(10, Math.floor(size / 3)), background: `linear-gradient(135deg,${a},${b})` }}>
      {ini}
    </span>
  );
}

export function Who({ name, sub, p, size = 32, href, avatar }: { name: string; sub?: ReactNode; p?: string; size?: number; href?: string; avatar?: string | null }) {
  const inner = (
    <div className="row">
      <Avatar name={name} size={size} src={avatar} />
      <div style={{ minWidth: 0 }}>
        <div className="semi row" style={{ gap: 6 }}><span className="ellipsis">{name}</span>{p && <Plat p={p} />}</div>
        {sub && <div className="sub">{sub}</div>}
      </div>
    </div>
  );
  return href ? <Link href={href} onClick={(e) => e.stopPropagation()}>{inner}</Link> : inner;
}

export function Two({ a, b }: { a: ReactNode; b?: ReactNode }) {
  return (<><div className="t1">{a}</div>{b !== undefined && <div className="sub">{b}</div>}</>);
}

/* ---------- time ---------- */
export function When({ iso, mode = "time", seconds }: { iso: string | null | undefined; mode?: "time" | "ago"; seconds?: boolean }) {
  if (!iso) return <span className="muted">—</span>;
  return <time dateTime={iso} title={localTitle(iso)} className="num">{mode === "ago" ? fmtAgo(iso) : fmtTime(iso, seconds)}</time>;
}

/* ---------- layout ---------- */
export function Card({ children, className, style, as: As = "section", id, "aria-labelledby": labelledBy }: {
  children: ReactNode; className?: string; style?: CSSProperties; as?: "section" | "div" | "article"; id?: string; "aria-labelledby"?: string;
}) {
  return <As className={cn("card", className)} style={style} id={id} aria-labelledby={labelledBy}>{children}</As>;
}

export function CardHead({ title, hint, actions, id }: { title: ReactNode; hint?: ReactNode; actions?: ReactNode; id?: string }) {
  return (
    <div className="chead">
      <div><h2 id={id} style={{ margin: 0, fontSize: 14, fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>{title}</h2>{hint && <div className="hint">{hint}</div>}</div>
      {actions && <div className="acts">{actions}</div>}
    </div>
  );
}

export function PageHead({ title, sub, actions, back }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; back?: string | true }) {
  return (
    <div className="phead">
      {back && (back === true
        ? <button type="button" className="back" aria-label="Back" onClick={() => history.back()}><ArrowLeft size={18} /></button>
        : <Link className="back" aria-label="Back" href={back}><ArrowLeft size={18} /></Link>)}
      <div style={{ minWidth: 0 }}>
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {actions && <div className="acts">{actions}</div>}
    </div>
  );
}

export function Grid({ cols, children, className, style }: { cols?: 2 | 3 | 4 | 5 | 6; children: ReactNode; className?: string; style?: CSSProperties }) {
  return <div className={cn("g", cols && `g${cols}`, className)} style={style}>{children}</div>;
}

/* ---------- buttons ---------- */
type BtnKind = "" | "p" | "dark" | "red" | "green" | "w" | "ghost";
export function Btn({ children, kind = "", sm, icon, href, external, className, ...rest }: {
  children: ReactNode; kind?: BtnKind; sm?: boolean; icon?: ReactNode; href?: string; external?: boolean; className?: string;
} & Omit<ComponentProps<"button">, "children">) {
  const cls = cn("btn", kind, sm && "sm", className);
  if (href) {
    return external
      ? <a className={cls} href={href} target="_blank" rel="noopener noreferrer">{icon}{children}</a>
      : <Link className={cls} href={href}>{icon}{children}</Link>;
  }
  return <button type="button" className={cls} {...rest}>{icon}{children}</button>;
}

export function IconBtn({ label, children, sm, dark, href, className, ...rest }: { label: string; children: ReactNode; sm?: boolean; dark?: boolean; href?: string; className?: string } & Omit<ComponentProps<"button">, "children">) {
  const cls = cn("ibtn", sm && "sm", dark && "dark", className);
  if (href) return <Link className={cls} href={href} aria-label={label} title={label}>{children}</Link>;
  return <button type="button" className={cls} aria-label={label} title={label} {...rest}>{children}</button>;
}

export function RefreshBtn({ onClick, spinning }: { onClick: () => void; spinning?: boolean }) {
  return (
    <IconBtn label="Refresh" onClick={onClick}>
      <RefreshCw size={17} className={spinning ? "animate-spin" : undefined} />
    </IconBtn>
  );
}

/* ---------- segmented filter ---------- */
export interface SegItem { value: string; label: string; count?: number | null }
export function Seg({ items, value, onChange, dark, label }: { items: SegItem[]; value: string; onChange: (v: string) => void; dark?: boolean; label: string }) {
  return (
    <div className={cn("seg", dark && "dark")} role="radiogroup" aria-label={label}>
      {items.map((it) => (
        <button key={it.value} type="button" role="radio" aria-checked={it.value === value} className={it.value === value ? "on" : ""} onClick={() => onChange(it.value)}>
          {it.label}{it.count !== undefined && it.count !== null && <i>{it.count}</i>}
        </button>
      ))}
    </div>
  );
}

/* ---------- inputs ---------- */
export function SearchField({ value, onChange, placeholder, width = 230, label }: { value: string; onChange: (v: string) => void; placeholder: string; width?: number; label: string }) {
  return (
    <label className="field" style={{ width, minWidth: Math.min(width, 160) }}>
      <span className="sr-only">{label}</span>
      <input className="field-input" type="search" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

export function SelectField({ value, onChange, options, label, width = 170 }: { value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; label: string; width?: number }) {
  return (
    <label className="field" style={{ width, minWidth: Math.min(width, 140), paddingRight: 10 }}>
      <span className="sr-only">{label}</span>
      <select className="field-input" value={value} onChange={(e) => onChange(e.target.value)} style={{ cursor: "pointer" }}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

export function Toggle({ on, red, label, onChange, disabled }: { on: boolean; red?: boolean; label: string; onChange?: (v: boolean) => void; disabled?: boolean }) {
  if (!onChange) return <span className={cn("toggle", red && "red", on && "on")} role="img" aria-label={`${label}: ${on ? "on" : "off"}`} />;
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled} onClick={() => onChange(!on)}
      className={cn("toggle", red && "red", on && "on")} style={{ border: 0, padding: 0, cursor: disabled ? "not-allowed" : "pointer" }} />
  );
}

/* ---------- banners & states ---------- */
export function Banner({ tone = "amber", icon, children, actions, style }: { tone?: "red" | "amber" | "blue" | "purple"; icon?: ReactNode; children: ReactNode; actions?: ReactNode; style?: CSSProperties }) {
  return (
    <div className={cn("banner", tone)} role={tone === "red" ? "alert" : "status"} style={style}>
      {icon ?? (tone === "blue" || tone === "purple" ? <Info size={17} /> : <AlertTriangle size={17} />)}
      <div className="grow">{children}</div>
      {actions}
    </div>
  );
}

export function Empty({ icon, title, children, actions, tone }: { icon: ReactNode; title: string; children?: ReactNode; actions?: ReactNode; tone?: "amber" | "red" }) {
  const style = tone === "amber" ? { background: "var(--as)", color: "var(--amber)" } : tone === "red" ? { background: "var(--rs)", color: "var(--red)" } : undefined;
  return (
    <div className="empty" role="status">
      <div>
        <div className="eic" style={style}>{icon}</div>
        <h2>{title}</h2>
        {children && <p>{children}</p>}
        {actions && <div className="row" style={{ justifyContent: "center", flexWrap: "wrap" }}>{actions}</div>}
      </div>
    </div>
  );
}

export function ErrorState({ error, retry }: { error: { message: string; status?: number } | null; retry?: () => void }) {
  const unreachable = error?.status === 502 || error?.status === 504;
  return (
    <Card>
      <Empty icon={<AlertTriangle size={30} />} tone={unreachable ? "amber" : "red"} title={unreachable ? "Can’t reach the pipeline server" : error?.status === 404 ? "Not found" : "Something went wrong"}
        actions={retry && <Btn kind="p" icon={<RefreshCw size={15} />} onClick={retry}>Try again</Btn>}>
        {unreachable
          ? "The console is up, but the control API did not answer. The system may still be running — Telegram alerts come straight from the server."
          : error?.message}
      </Empty>
    </Card>
  );
}

export function Skel({ w = "100%", h = 14, r = 10, style }: { w?: number | string; h?: number; r?: number; style?: CSSProperties }) {
  return <div className="skel" style={{ width: w, height: h, borderRadius: r, ...style }} />;
}

export function SkeletonCards({ n = 4, h = 150 }: { n?: number; h?: number }) {
  return (
    <div className={cn("g", n >= 4 ? "g4" : n === 3 ? "g3" : "g2")} role="status" aria-busy="true" aria-label="Loading">
      {Array.from({ length: n }).map((_, i) => (
        <div key={i} className="card"><Skel w="50%" /><div style={{ height: 18 }} /><Skel w="40%" h={30} /><div style={{ height: 16 }} /><Skel h={h - 90} r={12} /></div>
      ))}
    </div>
  );
}

export function SkeletonTable({ rows = 8 }: { rows?: number }) {
  return (
    <div className="card" role="status" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="row" style={{ padding: "12px 0", borderBottom: "1px solid var(--line)" }}>
          <Skel w={32} h={32} r={16} /><Skel w="30%" h={12} /><span className="grow" /><Skel w="12%" h={12} /><Skel w="8%" h={12} />
        </div>
      ))}
    </div>
  );
}

/* ---------- data bits ---------- */
export function Prog({ pct, tone, thin, width, label }: { pct: number; tone?: "red" | "amber" | "green"; thin?: boolean; width?: number | string; label?: string }) {
  const v = Math.max(0, Math.min(100, pct));
  return (
    <div className={cn("prog", tone, thin && "thin")} style={{ width }} role="progressbar" aria-valuenow={Math.round(v)} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <i style={{ width: `${v}%` }} />
    </div>
  );
}

export function KV({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv2" style={{ margin: 0 }}>
      {rows.map(([k, v], i) => (
        <div className="r" key={i}><dt style={{ color: "var(--mute)" }}>{k}</dt><dd style={{ margin: 0, fontWeight: 600, textAlign: "right" }}>{v}</dd></div>
      ))}
    </dl>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("mono", className)}>{children}</span>;
}

export function Dash() { return <span className="muted">—</span>; }
