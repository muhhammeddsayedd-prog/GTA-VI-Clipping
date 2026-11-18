"use client";

import Link from "next/link";

import { Badge, Card, Plat, Prog, platformName } from "@/components/kit/primitives";
import type { AccountSummary } from "@/lib/api/types";
import { fmtAgo, fmtTime } from "@/lib/format";
import { accountLabel } from "@/lib/use-accounts";

export function accountState(a: AccountSummary): "enabled" | "paused" | "disabled" {
  if (!a.enabled) return "disabled";
  if (a.paused_until && Date.parse(a.paused_until) > Date.now()) return "paused";
  return "enabled";
}

/** Compact account card used on the overview. */
export function AccountCard({ a }: { a: AccountSummary }) {
  const pct = a.cap_24h ? (a.used_24h / a.cap_24h) * 100 : 0;
  const st = accountState(a);
  return (
    <Link href={`/accounts/${a.id}`} aria-label={`${accountLabel(a, a.id)}: ${a.used_24h} of ${a.cap_24h} posts in 24 hours, ${st}`}>
      <Card as="div" className="hover-lift">
        <div className="row">
          <Plat p={a.platform} lg />
          <div style={{ minWidth: 0 }}><div className="semi ellipsis" title={a.id}>{accountLabel(a, a.id)}</div><div className="muted xs ellipsis">{platformName(a.platform)}{a.lanes.length === 1 && a.lanes[0] === "spike" ? " · spike lane" : ""}</div></div>
          <span className="right"><Badge status={st} /></span>
        </div>
        <div className="row" style={{ marginTop: 16, alignItems: "baseline" }}>
          <span className="num" style={{ fontSize: 24, fontWeight: 600 }}>{a.used_24h}</span>
          <span className="muted">/ {a.cap_24h} in 24h</span>
          <span className="right muted xs">{Math.round(pct)}%</span>
        </div>
        <div style={{ marginTop: 8 }}><Prog pct={pct} tone={pct > 85 ? "amber" : undefined} label={`${accountLabel(a, a.id)} cap used`} /></div>
        <div className="row muted xs" style={{ marginTop: 12, justifyContent: "space-between", gap: 8 }}>
          <span>Last <b className="ink2">{a.last_post_at ? fmtAgo(a.last_post_at) : "—"}</b></span>
          {a.spike_reserve > 0 && a.lanes.includes("spike") && <span>Reserve <b className="ink2">{a.spike_reserve_remaining}/{a.spike_reserve}</b></span>}
          <span>{st === "paused" ? <>Until <b className="warn">{fmtTime(a.paused_until)}</b></> : st === "disabled" ? <b className="ink2">lane off</b> : <>Gap <b className="ink2">{Math.round(a.min_gap_s / 60)}m</b></>}</span>
        </div>
      </Card>
    </Link>
  );
}
