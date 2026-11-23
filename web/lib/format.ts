/** Formatting helpers. Times are shown in the app zone (`app.timezone`, default Cairo); hover titles carry UTC. */

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
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/* ---------- display time zone ----------
 * The API sends UTC; the console shows every time in the app zone (`settings.app.timezone`, default
 * Africa/Cairo). Limits that are defined in UTC (hourly slots, budget resets) say so next to the time.
 * `TimeZoneSync` in the shell keeps this store in line with the setting. */

export const DEFAULT_ZONE = "Africa/Cairo";
let displayZone = DEFAULT_ZONE;
const zoneListeners = new Set<() => void>();

export function isValidZone(name: unknown): name is string {
  if (typeof name !== "string" || !name.trim()) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: name });
    return true;
  } catch {
    return false;
  }
}

export function getDisplayZone(): string {
  return displayZone;
}

export function setDisplayZone(zone: unknown): void {
  const next = isValidZone(zone) ? zone : DEFAULT_ZONE;
  if (next === displayZone) return;
  displayZone = next;
  zoneListeners.forEach((l) => l());
}

export function subscribeDisplayZone(cb: () => void): () => void {
  zoneListeners.add(cb);
  return () => { zoneListeners.delete(cb); };
}

/** "Cairo", "New York", "UTC". */
export function zoneLabel(zone = displayZone): string {
  if (zone.toUpperCase() === "UTC" || zone === "Etc/UTC") return "UTC";
  return zone.slice(zone.lastIndexOf("/") + 1).replace(/_/g, " ");
}

const partsCache = new Map<string, Intl.DateTimeFormat>();
function partsFormatter(zone: string): Intl.DateTimeFormat {
  let f = partsCache.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, hourCycle: "h23",
      year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric",
    });
    partsCache.set(zone, f);
  }
  return f;
}

export interface ZonedParts { year: number; month: number; day: number; hour: number; minute: number; second: number }

/** Wall-clock fields of an instant in `zone` (month is 1–12). */
export function zonedParts(at: string | number | Date, zone = displayZone): ZonedParts {
  const ms = typeof at === "string" ? Date.parse(at) : typeof at === "number" ? at : at.getTime();
  const out: Record<string, number> = {};
  for (const p of partsFormatter(zone).formatToParts(ms)) if (p.type !== "literal") out[p.type] = Number(p.value);
  return { year: out.year, month: out.month, day: out.day, hour: out.hour % 24, minute: out.minute, second: out.second };
}

/** The hour (0–23) of an instant in `zone`. */
export function zonedHour(at: string | number | Date, zone = displayZone): number {
  return zonedParts(at, zone).hour;
}

/** "UTC+2", "UTC-4:30", "UTC" for `zone` at instant `at`. */
export function zoneOffset(zone = displayZone, at: number = Date.now()): string {
  const p = zonedParts(at, zone);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  const min = Math.round((asUtc - Math.floor(at / 1000) * 1000) / 60000);
  if (min === 0) return "UTC";
  const a = Math.abs(min);
  return `UTC${min > 0 ? "+" : "-"}${Math.floor(a / 60)}${a % 60 ? `:${pad(a % 60)}` : ""}`;
}

/** "14:32" for today, "17 Nov 14:32" otherwise — in the display zone. */
export function fmtTime(iso: string | null | undefined, withSeconds = false, now: Date = new Date(), zone = displayZone): string {
  if (!iso) return "—";
  const d = zonedParts(iso, zone);
  const n = zonedParts(now, zone);
  const hm = `${pad(d.hour)}:${pad(d.minute)}${withSeconds ? `:${pad(d.second)}` : ""}`;
  const sameDay = d.year === n.year && d.month === n.month && d.day === n.day;
  return sameDay ? hm : `${d.day} ${MONTHS[d.month - 1]} ${hm}`;
}

/** "17 Nov 2026" in the display zone. */
export function fmtDay(iso: string | null | undefined, zone = displayZone): string {
  if (!iso) return "—";
  const d = zonedParts(iso, zone);
  return `${d.day} ${MONTHS[d.month - 1]} ${d.year}`;
}

/** "17 Nov" in the display zone. */
export function fmtDayShort(iso: string | null | undefined, zone = displayZone): string {
  if (!iso) return "—";
  const d = zonedParts(iso, zone);
  return `${d.day} ${MONTHS[d.month - 1]}`;
}

/** "17 Nov 2026 14:32 Cairo" — a full time with its zone, for detail pages and tooltips. */
export function fmtFull(iso: string | null | undefined, zone = displayZone): string {
  if (!iso) return "—";
  const d = zonedParts(iso, zone);
  return `${d.day} ${MONTHS[d.month - 1]} ${d.year} ${pad(d.hour)}:${pad(d.minute)} ${zoneLabel(zone)}`;
}

/** Hover text of every time: the exact UTC instant (limits and logs are in UTC). */
export function utcTitle(iso: string | null | undefined): string | undefined {
  if (!iso) return undefined;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return undefined;
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`;
}

/** UTC hour `h` (a slot or a reset defined in UTC) shown in the display zone today: "14:00 Cairo". */
export function utcHourInZone(h: number, zone = displayZone, now: number = Date.now()): string {
  const n = new Date(now);
  const at = Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate(), h, 0, 0);
  const p = zonedParts(at, zone);
  return `${pad(p.hour)}:${pad(p.minute)} ${zoneLabel(zone)}`;
}

export function series(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `#${String(n).padStart(4, "0")}`;
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "—";
}

export function humanize(code: string | null | undefined): string {
  return code ? code.replace(/_/g, " ") : "—";
}
