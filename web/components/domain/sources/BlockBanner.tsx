"use client";

import { AlertTriangle } from "lucide-react";

import { Banner } from "@/components/kit/primitives";
import { fmtDay, fmtTime } from "@/lib/format";
import { useNow } from "@/lib/use-now";
import { isRecord, num, str } from "@/lib/shape";

/**
 * A platform that refuses this server, as written to `settings.vod.source_health` / `live.source_health`:
 * `{ <platform>: { since, error, access, blocks, last_blocked_at?, skip_until?, notified? } }`.
 */
export interface SourceBlock {
  platform: string; since: string | null; error: string | null; access: string | null; blocks: number | null;
  lastBlockedAt: string | null; skipUntil: string | null;
}

export function readBlocks(health: unknown): SourceBlock[] {
  if (!isRecord(health)) return [];
  return Object.entries(health).flatMap(([platform, h]) => {
    if (!isRecord(h) || !str(h.since)) return [];
    return [{
      platform, since: str(h.since), error: str(h.error), access: str(h.access), blocks: num(h.blocks),
      lastBlockedAt: str(h.last_blocked_at), skipUntil: str(h.skip_until),
    }];
  });
}

const NAMES: Record<string, string> = { youtube: "YouTube", twitch: "Twitch" };

/** One banner per blocked platform. `what` names the work that is blocked ("downloads", "live recording"). */
export function BlockBanners({ blocks, what }: { blocks: SourceBlock[]; what: string }) {
  const now = useNow();
  return (
    <>
      {blocks.map((b) => {
        const name = NAMES[b.platform] ?? b.platform;
        const retry = b.skipUntil && now && Date.parse(b.skipUntil) > now ? b.skipUntil : null;
        return (
          <Banner key={b.platform} tone="amber" icon={<AlertTriangle size={17} />}>
            <b>{name} {what} are blocked from this server</b> since {fmtDay(b.since)} {fmtTime(b.since)} UTC
            {b.blocks ? <> · {b.blocks} refused attempt{b.blocks === 1 ? "" : "s"}</> : null}
            {b.lastBlockedAt ? <> · last {fmtTime(b.lastBlockedAt)} UTC</> : null}
            {retry ? <> · next try {fmtTime(retry)} UTC</> : null}. The work waits instead of failing.
            {b.access && <div className="xs" style={{ marginTop: 4 }}>Access: {b.access}</div>}
            {b.error && <div className="xs ellipsis" style={{ marginTop: 2, opacity: 0.8 }} title={b.error}>Last error: {b.error}</div>}
          </Banner>
        );
      })}
    </>
  );
}
