/**
 * `takedowns.actions` as the guardian writes it (src/sixstars/guardian/takedowns.py):
 * `{ state, kind?, summary?, why?, strike?, matched?: { creators, posts, how? }, items?: [ … ] }`.
 * Items are `block_creator` (one per creator) and `remove_post` (one per post, outcome removed | already_gone | manual | failed).
 * An older array form (the items alone) is read too.
 */
import type { Json } from "@/lib/api/types";
import { isRecord } from "@/lib/shape";

export interface TakedownAction {
  action?: string; outcome?: string; detail?: string | null; platform?: string; post_id?: string; post_url?: string | null;
  account_id?: string; creator?: string; newly_blocked?: boolean; clips_dropped?: number; pending_posts_cancelled?: number; at?: string;
}

export interface TakedownActions {
  state: string | null; kind: string | null; summary: string | null; why: string | null; strike: boolean;
  matched: { creators: string[]; posts: number; how: string | null } | null; items: TakedownAction[];
}

export function readTakedownActions(raw: Json): TakedownActions {
  if (Array.isArray(raw)) {
    const items = raw.filter(isRecord) as TakedownAction[];
    return { state: null, kind: null, summary: null, why: null, strike: items.some((a) => a.action === "strike"), matched: null, items };
  }
  if (!isRecord(raw)) return { state: null, kind: null, summary: null, why: null, strike: false, matched: null, items: [] };
  const m = isRecord(raw.matched) ? raw.matched : null;
  return {
    state: typeof raw.state === "string" ? raw.state : null,
    kind: typeof raw.kind === "string" ? raw.kind : null,
    summary: typeof raw.summary === "string" ? raw.summary : null,
    why: typeof raw.why === "string" ? raw.why : null,
    strike: raw.strike === true,
    matched: m ? {
      creators: Array.isArray(m.creators) ? m.creators.filter((c): c is string => typeof c === "string") : [],
      posts: typeof m.posts === "number" ? m.posts : 0,
      how: typeof m.how === "string" ? m.how : null,
    } : null,
    items: Array.isArray(raw.items) ? (raw.items.filter(isRecord) as TakedownAction[]) : [],
  };
}

/** One-line summary for the takedown list. */
export function takedownSummary(status: string, raw: Json): string {
  if (status === "ignored") return "not a takedown";
  const a = readTakedownActions(raw);
  const removed = a.items.filter((x) => x.action === "remove_post" && (x.outcome === "removed" || x.outcome === "already_gone")).length;
  const manual = a.items.filter((x) => x.action === "remove_post" && (x.outcome === "manual" || x.outcome === "failed")).length;
  const parts = [
    a.items.some((x) => x.action === "block_creator") ? "blocked" : null,
    removed ? `${removed} removed` : null,
    manual ? `${manual} by hand` : null,
    a.strike ? "strike" : null,
  ].filter(Boolean);
  if (status === "open") return parts.length ? `needs you · ${parts.join(" · ")}` : a.state === "unclassified" ? "needs you · not classified" : "needs you";
  return parts.join(" · ") || "—";
}
