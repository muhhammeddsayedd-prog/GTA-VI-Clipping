/** Formatting helpers. Everything is shown in UTC; hover titles carry local time. */

export function fmtNum(n: number | null | undefined, opts?: Intl.NumberFormatOptions): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", opts).format(n);
}

export function fmtCompact(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function fmtUsd(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined) return "—";
  return `$${n.toFixed(digits)}`;
}

export function fmtPct(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function fmtDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return "—";
  const s = Math.round(Math.abs(sec));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60 && m < 10 ? ` ${s % 60}s` : ""}`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** Clip length like 0:34. */
export function fmtClock(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return "—";
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
}

export function ageSeconds(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  return Math.round((now - Date.parse(iso)) / 1000);
}

/** "9 min ago", "in 28 min". */
export function fmtAgo(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const d = Math.round((now - Date.parse(iso)) / 1000);
  const abs = Math.abs(d);
  let t: string;
  if (abs < 45) t = `${abs} s`;
  else if (abs < 3600) t = `${Math.round(abs / 60)} min`;
  else if (abs < 86400) t = `${Math.floor(abs / 3600)}h ${Math.round((abs % 3600) / 60)}m`;
  else t = `${Math.round(abs / 86400)}d`;
  return d >= 0 ? `${t} ago` : `in ${t}`;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** "14:32" for today, "17 Nov 14:32" otherwise (UTC). */
export function fmtTime(iso: string | null | undefined, withSeconds = false, now = new Date()): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${withSeconds ? `:${pad(d.getUTCSeconds())}` : ""}`;
  const sameDay = d.getUTCFullYear() === now.getUTCFullYear() && d.getUTCMonth() === now.getUTCMonth() && d.getUTCDate() === now.getUTCDate();
  return sameDay ? hm : `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${hm}`;
}

export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

export function localTitle(iso: string | null | undefined): string | undefined {
  if (!iso) return undefined;
  return `${new Date(iso).toLocaleString()} (your time)`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function series(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `#${String(n).padStart(4, "0")}`;
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "—";
}

export function humanize(code: string | null | undefined): string {
  return code ? code.replace(/_/g, " ") : "—";
}
