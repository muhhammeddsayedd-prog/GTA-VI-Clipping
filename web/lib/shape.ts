/**
 * Helpers that read loosely shaped JSON columns (score parts, keywords, gate values, event data) safely.
 * The backend stores several of these as free-form JSON, so the console must never assume a shape.
 */
import type { Json } from "@/lib/api/types";

export function isRecord(v: unknown): v is Record<string, Json> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export function str(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

/** The numeric entries of an object, in order; nested objects, strings and booleans are skipped. */
export function numericEntries(v: unknown): [string, number][] {
  if (!isRecord(v)) return [];
  return Object.entries(v).flatMap(([k, x]) => (num(x) !== null ? [[k, x as number] as [string, number]] : []));
}

/**
 * Keyword set from `clips.keywords`. The writer stores `{ "<line>": ["word", …] }`; older rows and the
 * sample data use `[{ line, words }]`. Multi-word keywords add each word too.
 */
export function keywordSet(kw: unknown): Set<string> {
  const lists: unknown[] = Array.isArray(kw)
    ? kw.map((k) => (isRecord(k) ? k.words : null))
    : isRecord(kw) ? Object.values(kw) : [];
  const out = new Set<string>();
  for (const list of lists) {
    if (!Array.isArray(list)) continue;
    for (const phrase of list) {
      if (typeof phrase !== "string") continue;
      for (const w of phrase.toLowerCase().split(/\s+/)) {
        const clean = w.replace(/[^a-z0-9']/g, "");
        if (clean) out.add(clean);
      }
    }
  }
  return out;
}

/** One-line human text for any JSON value (gate check values and limits, event data). */
export function showValue(v: Json | undefined): string {
  if (v === undefined || v === null) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v;
  if (Array.isArray(v)) {
    if (!v.length) return "none";
    if (v.length === 2 && v.every((x) => typeof x === "number")) return `${showValue(v[0])}–${showValue(v[1])}`;
    return v.map((x) => showValue(x)).join(", ");
  }
  const parts = Object.entries(v).filter(([, x]) => x !== null && x !== undefined);
  if (!parts.length) return "—";
  return parts.map(([k, x]) => `${k.replace(/_/g, " ")} ${showValue(x)}`).join(" · ");
}

/** A reject reason is either a gate code (`music_over_limit`) or a writer sentence (`writer: …`). */
export function splitReason(reason: string | null | undefined): { kind: "writer" | "code" | null; text: string } {
  if (!reason) return { kind: null, text: "" };
  const m = /^writer:\s*([\s\S]*)$/i.exec(reason);
  return m ? { kind: "writer", text: m[1] } : { kind: "code", text: reason };
}
