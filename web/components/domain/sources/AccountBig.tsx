"use client";

import Link from "next/link";
import { ChevronRight, Pause } from "lucide-react";

import { Ring } from "@/components/kit/charts";
import { Hours } from "@/components/kit/data";
import { Badge, Card, KV, Plat, platformName } from "@/components/kit/primitives";
import { accountState } from "@/components/domain/AccountCard";
import type { AccountSummary } from "@/lib/api/types";
import { fmtAgo, fmtTime } from "@/lib/format";
import { accountLabel } from "@/lib/use-accounts";

export function hourCells(a: AccountSummary) {
  const allowed = a.hourly_hours_utc;
  const hourlyOn = a.enabled && a.lanes.includes("hourly");
  return Array.from({ length: 24 }, (_, h) => {
    const ok = hourlyOn && (!allowed || allowed.includes(h));
    return { cls: ok ? "on" : "off", title: `${String(h).padStart(2, "0")}:00 UTC — ${ok ? "hourly slot allowed" : "no hourly slot"}` };
  });
}

export function pauseNote(a: AccountSummary): string | null {
  const st = accountState(a);
  if (st === "paused") return `Paused until ${fmtTime(a.paused_until)} UTC${a.pause_reason ? ` · ${a.pause_reason}` : ""}. Other accounts keep posting; resumes on its own.`;
  if (st === "disabled") return `Disabled · ${a.lanes.join(" + ")} lane off${a.lanes.includes("spike") && a.platform === "tiktok" ? " (TikTok gets no spike clips)" : ""}`;
  return null;
}

export function AccountBig({ a }: { a: AccountSummary }) {
  const pct = a.cap_24h ? (a.used_24h / a.cap_24h) * 100 : 0;
  const st = accountState(a);
  const note = pauseNote(a);
  return (
    <Card as="article" aria-labelledby={`acc-${a.id}`}>
      <div className="row">
        <Plat p={a.platform} lg />
        <div style={{ minWidth: 0 }}><h2 id={`acc-${a.id}`} className="semi" style={{ fontSize: 15, margin: 0 }} title={a.id}>{accountLabel(a, a.id)}</h2><div className="muted xs">{platformName(a.platform)}{a.lanes.length === 1 && a.lanes[0] === "spike" ? " · spike lane" : ""}</div></div>
        <span className="right"><Badge status={st} /></span>
      </div>
      <div className="row" style={{ marginTop: 18, gap: 20, alignItems: "center" }}>
        <Ring pct={pct} size={92} color={pct > 90 ? "#E5484D" : "#5A4AF4"} text={`${a.used_24h}/${a.cap_24h}`} sub="24h cap" fs={16} label={`${a.used_24h} of ${a.cap_24h} posts used in the last 24 hours`} />
        <div className="grow">
          <KV rows={[
            ["Lanes", <span key="l" className="row" style={{ gap: 4, justifyContent: "flex-end" }}>{a.lanes.length ? a.lanes.map((l) => <Badge key={l} tone={l === "spike" ? "purple" : "blue"}>{l}</Badge>) : "—"}</span>],
            ["Spike reserve", a.lanes.includes("spike") ? `${a.spike_reserve_remaining} of ${a.spike_reserve} left` : "—"],
            ["Privacy", a.privacy ?? (a.platform === "instagram" ? "public (no private mode)" : "—")],
            ["Min gap", `${Math.round(a.min_gap_s / 60)} min`],
          ]} />
        </div>
      </div>
      <div className="flabel mt">Hourly hours (UTC)</div>
      <Hours cells={hourCells(a)} />
      {note && <div className={`banner ${st === "paused" ? "amber" : "blue"}`} style={{ margin: "14px 0 0", padding: "10px 12px", fontSize: 11.5 }}><Pause size={14} />{note}</div>}
      <div className="row mt" style={{ justifyContent: "space-between" }}>
        <span className="muted xs">Last post <b className="ink2">{a.last_post_at ? fmtAgo(a.last_post_at) : "never"}</b></span>
        <Link href={`/accounts/${a.id}`} className="pc semi small row" style={{ gap: 4 }}>Details <ChevronRight size={13} /></Link>
      </div>
    </Card>
  );
}
