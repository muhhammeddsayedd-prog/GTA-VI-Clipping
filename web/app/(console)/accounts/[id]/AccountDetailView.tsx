"use client";

import { Eye, List, Pause, Send, Star, Users } from "lucide-react";

import { Bars, Line } from "@/components/kit/charts";
import { CheckRow, DataTable, EventFeed, Hours, Kpi, type Col } from "@/components/kit/data";
import { Badge, Banner, Btn, Card, CardHead, ErrorState, Grid, PageHead, Plat, SkeletonCards, When } from "@/components/kit/primitives";
import { accountState } from "@/components/domain/AccountCard";
import { hourCells, pauseNote } from "@/components/domain/sources/AccountBig";
import { POLL, useApi } from "@/lib/api/client";
import type { AccountDetail, Post } from "@/lib/api/types";
import { ageSeconds, fmtAgo, fmtCompact, fmtDay, fmtNum, fmtTime, series } from "@/lib/format";
import { accountLabel } from "@/lib/use-accounts";

const POST_COLS: Col<Post>[] = [
  { key: "clip", head: "Clip", render: (p) => <span className="semi pc">{series(p.clip_series_no)}</span> },
  { key: "hook", head: "Hook", render: (p) => <span className="ellipsis" style={{ display: "block", maxWidth: 260 }}>{p.clip_hook ?? "—"}</span> },
  { key: "lane", head: "Lane", render: (p) => <Badge tone={p.lane === "spike" ? "purple" : "blue"}>{p.lane}</Badge> },
  { key: "status", head: "Status", render: (p) => <Badge status={p.status} /> },
  { key: "pub", head: "Published", render: (p) => <When iso={p.published_at} /> },
  { key: "views", head: "Views", align: "right", render: (p) => <span className="num">{fmtNum(p.views)}</span> },
];

const PLAT_LABEL: Record<string, string> = { tiktok: "TikTok", instagram: "Instagram Reels", youtube: "YouTube Shorts" };

export function AccountDetailView({ id }: { id: string }) {
  const q = useApi<AccountDetail>(`admin/accounts/${encodeURIComponent(id)}`, undefined, { interval: POLL.list });
  if (q.error && !q.data) return <><PageHead title={id} back="/accounts" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title={id} back="/accounts" sub="Loading…" /><SkeletonCards n={4} /></>;
  const a = q.data;
  const st = accountState(a);
  const note = pauseNote(a);
  // Only snapshots that carry a follower count; a real 0 is shown as 0, "no snapshot" as —.
  const followers = a.stats.map((s) => s.followers).filter((f): f is number => typeof f === "number");
  const first = followers[0] ?? 0, last = followers.length ? followers[followers.length - 1] : null;
  const views7 = a.views_by_day.reduce((s, d) => s + d.views, 0);
  const statLabels = a.stats.map((s, i) => (i === 0 || i === a.stats.length - 1 || i === Math.floor(a.stats.length / 2) ? fmtDayShort(s.captured_at) : ""));
  const k = a.caps;
  const gapMin = minutesSince(k.last_post_at);
  const paused = st === "paused";

  return (
    <>
      <PageHead back="/accounts"
        title={<span className="row" style={{ gap: 14 }}><Plat p={a.platform} lg /><span title={a.id}>{accountLabel(a, a.id)}</span></span>}
        sub={<>{a.handle} · {PLAT_LABEL[a.platform] ?? a.platform} · <Badge status={st} /></>}
        actions={<Btn kind="p" href={`/events?entity_type=account&entity_id=${a.id}`} icon={<List size={15} />}>Account events</Btn>} />

      {note && <Banner tone={paused ? "amber" : "blue"} icon={<Pause size={17} />}><b>{paused ? `Paused until ${fmtTime(a.paused_until)} UTC.` : "Disabled."}</b> {note.replace(/^(Paused until [^.]+\. |Disabled · )/, "")}</Banner>}

      <Grid cols={4}>
        <Kpi label={a.platform === "youtube" ? "Subscribers" : "Followers"} value={last} icon={<Users size={15} />}
          delta={followers.length > 1 && last !== null ? <span><b className={last >= first ? "up" : "down"}>{last >= first ? "↑" : "↓"} {fmtNum(Math.abs(last - first))}</b> since {fmtDay(a.stats.find((s) => typeof s.followers === "number")?.captured_at)}</span> : undefined} />
        <Kpi label="Views · 7d" value={views7} icon={<Eye size={15} />} valueText={fmtCompact(views7)} />
        <Kpi label="Posts · 24h" value={a.used_24h} unit={`/ ${a.cap_24h}`} icon={<Send size={15} />} iconTone={a.used_24h >= a.cap_24h ? "red" : undefined} delta={<span>last {a.last_post_at ? fmtAgo(a.last_post_at) : "never"}</span>} />
        <Kpi label="Spike reserve" value={a.lanes.includes("spike") ? a.spike_reserve_remaining : null} unit={a.lanes.includes("spike") ? `/ ${a.spike_reserve}` : undefined} icon={<Star size={15} />} delta={<span>{a.lanes.includes("spike") ? "kept free for spike clips" : "spike lane off"}</span>} />
      </Grid>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1fr 1fr 400px" }}>
        <Card>
          <CardHead title={a.platform === "youtube" ? "Subscribers" : "Followers"} hint="account_stats · snapshot at most every 30 min" />
          {followers.length > 1 ? <Line label={`Followers over ${a.stats.length / 2} days`} values={followers} labels={statLabels} width={440} height={250} />
            : <div className="muted small">No snapshots yet. Stats are collected every 2 hours.</div>}
        </Card>
        <Card>
          <CardHead title="Views per day" hint="Sum over this account's posts" />
          {a.views_by_day.length ? <Bars label="Views per day, last 7 days" values={a.views_by_day.map((d) => d.views)} labels={a.views_by_day.map((d) => d.day.slice(8, 10))} width={440} height={250} />
            : <div className="muted small">No views recorded yet.</div>}
        </Card>
        <Card>
          <CardHead title="Cap math · right now" hint="The same numbers the poster uses" />
          <CheckRow state="ok" name="used_24h" value={k.used_24h} limit={`cap ${k.cap_24h}`} />
          <CheckRow state={k.free > 0 ? "ok" : "no"} name="free" value={k.free} limit="cap − used" />
          <CheckRow state={a.lanes.includes("spike") ? (k.spike_reserve_remaining > 0 ? "ok" : "no") : "na"} name="spike_reserve_remaining" value={k.spike_reserve_remaining} limit={`of ${a.spike_reserve}`} />
          <CheckRow state={a.lanes.includes("hourly") ? (k.hourly_allowed ? "ok" : "no") : "na"} name="hourly_allowed" value={k.hourly_allowed ? "yes" : "no"} limit={a.lanes.includes("hourly") ? "free > reserve" : "lane off"} />
          <CheckRow state={a.lanes.includes("spike") ? (k.spike_allowed ? "ok" : "no") : "na"} name="spike_allowed" value={k.spike_allowed ? "yes" : "no"} limit={a.lanes.includes("spike") ? "≤ 2 per hour" : "lane off"} />
          <CheckRow state={k.gap_ok ? "ok" : "no"} name="gap_ok" value={k.gap_ok ? "yes" : "no"} limit={gapMin !== null ? `${gapMin} min ≥ ${Math.round(a.min_gap_s / 60)}` : "no posts yet"} />
          <CheckRow state={paused ? "no" : "ok"} name="paused_until" value={paused ? fmtTime(k.paused_until) : "—"} limit={paused ? "rate limit" : "not paused"} />
        </Card>
      </div>

      <Card style={{ marginTop: 16 }}>
        <CardHead title="Hourly hours (UTC)" hint="Hours this account takes an hourly-lane post" />
        <Hours cells={hourCells(a)} />
      </Card>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1.4fr 1fr" }}>
        <Card>
          <CardHead title="Recent posts" hint="Last 20 on this account" actions={<Btn sm href={`/posts?account_id=${a.id}`}>All posts</Btn>} />
          <DataTable cols={POST_COLS} rows={a.posts} rowKey={(p) => p.id} href={(p) => `/posts/${p.id}`} dense caption="Recent posts" empty="No posts on this account yet." />
        </Card>
        <Card>
          <CardHead title="Account events" />
          <EventFeed events={a.events} empty="No events for this account." />
        </Card>
      </div>
    </>
  );
}

function minutesSince(iso: string | null): number | null {
  const s = ageSeconds(iso);
  return s === null ? null : Math.round(s / 60);
}

function fmtDayShort(iso: string): string {
  const d = new Date(iso);
  return `${d.getUTCDate()} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()]}`;
}
