"use client";

import { ChevronLeft, ChevronRight, Copy, DollarSign, Eye, Film, Send, Trash2, AlertTriangle, Users } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { HBars, Ring } from "@/components/kit/charts";
import { DataTable, Delta, ExtLink, Kpi, Thumb } from "@/components/kit/data";
import { useToast } from "@/components/kit/overlays";
import { Badge, Btn, Card, CardHead, ErrorState, Grid, IconBtn, KV, PageHead, Plat, SkeletonCards } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { PostPlatform } from "@/lib/api/types";
import { fmtCompact, fmtDuration, fmtNum, fmtUsd, humanize } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

/** Mirrors `sixstars.stats.report.build()`. */
interface Report {
  day: string; timezone: string; generated_at: string; env: string;
  accounts: { account_id: string; platform: PostPlatform; handle: string | null; enabled: boolean; paused_until: string | null; followers: number | null; followers_change: number | null; views_total: number | null; views_change: number | null; published: number; failed: number; dry_run: number }[];
  views: { total: number | null; change: number | null; median_by_platform: Record<string, number> };
  top_clips: { title: string; views: number; platform: string; post_url: string | null }[];
  ypp: { subscribers: number | null; subscribers_target: number | null; shorts_views_90d: number; shorts_views_target: number | null };
  posts: { hourly_published: number; spike_published: number; failed: number; dry_run: number; slots_used: number; slots_skipped_events: number };
  spikes: { detected: number; S: number; A: number; B: number; rejected: number; median_spike_to_post_s: number | null };
  clips: { rendered: number; approved: number; rejected: number; rejected_by_reason: Record<string, number>; expired: number; queue: { spike: number; hourly: number; low_watermark: number | null } };
  discovery: { videos_processed: number; candidates_checked: number; new_creators: number; new_by_tier: Record<string, number> };
  guardian: { claims: number; takedowns: number; emails_ignored: number; emails_open: number; strikes: number; strike_limit: number; emergency_stop: boolean; stop_reason: string | null; manual_removals: { platform: string; post_url: string | null }[] };
  spend: { provider: string; spent_usd: number; budget_usd: number | null }[];
  issues: string[];
}

const iso = (d: Date) => d.toISOString().slice(0, 10);
const shift = (day: string, n: number) => { const d = new Date(`${day}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + n); return iso(d); };
const yesterday = () => shift(iso(new Date()), -1);
const pretty = (day: string) => new Date(`${day}T00:00:00Z`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

function asText(r: Report): string {
  const lines = [`VI Stars Clippy daily report — ${r.day} (${r.timezone})`, "",
    ...r.accounts.map((a) => `${a.handle ?? a.account_id}: ${fmtNum(a.followers)} followers (${a.followers_change ?? "—"}), ${a.published} posts, ${a.failed} failed`),
    `Views: ${fmtNum(r.views.total)} (${r.views.change ?? "—"})`,
    `Posts: ${r.posts.hourly_published} hourly, ${r.posts.spike_published} spike, ${r.posts.failed} failed`,
    `Spikes: ${r.spikes.detected} detected (S ${r.spikes.S} · A ${r.spikes.A} · B ${r.spikes.B})`,
    `Clips: ${r.clips.rendered} rendered, ${r.clips.approved} approved, ${r.clips.rejected} rejected`,
    `Guardian: ${r.guardian.claims} claims, ${r.guardian.takedowns} takedowns, ${r.guardian.strikes}/${r.guardian.strike_limit} strikes`,
    `Spend: ${r.spend.map((s) => `${s.provider} ${fmtUsd(s.spent_usd)}`).join(", ")}`,
    ...(r.issues.length ? ["", "Issues:", ...r.issues.map((i) => `- ${i}`)] : [])];
  return lines.join("\n");
}

export function ReportView() {
  const lp = useListParams(["day"]);
  const day = lp.get("day") || yesterday();
  const q = useApi<Report>("reports/daily", { format: "json", day }, { interval: POLL.slow });
  const toast = useToast();
  const today = iso(new Date());

  const head = (
    <PageHead title="Daily report" sub={<>Same numbers as the 00:00 UTC Telegram report · day of <b className="ink2">{pretty(day)}</b></>}
      actions={<>
        <label className="field" style={{ width: 170, minWidth: 150 }}>
          <span className="sr-only">Report day</span>
          <input type="date" className="field-input" value={day} max={today} onChange={(e) => e.target.value && lp.set("day", e.target.value)} />
        </label>
        <IconBtn label="Previous day" onClick={() => lp.set("day", shift(day, -1))}><ChevronLeft size={17} /></IconBtn>
        <IconBtn label="Next day" disabled={day >= today} onClick={() => lp.set("day", shift(day, 1))}><ChevronRight size={17} /></IconBtn>
        <Btn icon={<Copy size={15} />} disabled={!q.data} onClick={() => q.data && navigator.clipboard.writeText(asText(q.data)).then(() => toast("Report copied as text."), () => toast("Could not copy.", true))}>Copy as text</Btn>
      </>} />
  );
  if (q.error && !q.data) return <>{head}<ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <>{head}<SkeletonCards n={4} h={110} /></>;
  const r = q.data;
  const followers = r.accounts.reduce((a, x) => a + (x.followers_change ?? 0), 0);
  const posts = r.posts.hourly_published + r.posts.spike_published;
  const spent = r.spend.reduce((a, s) => a + s.spent_usd, 0);
  const over = r.spend.some((s) => s.budget_usd !== null && s.spent_usd > s.budget_usd);

  return (
    <>
      {head}
      <AnimatedContent distance={20} duration={0.5} stagger={0.05} trigger="mount" key={day}>
        <Grid cols={5}>
          <Kpi label="Total views" value={r.views.total} icon={<Eye size={15} />} delta={r.views.change !== null ? <><Delta v={r.views.change} /> <span>vs previous day</span></> : undefined} />
          <Kpi label="New followers" value={followers} icon={<Users size={15} />} delta={<span>across {r.accounts.filter((a) => a.enabled).length} accounts</span>} />
          <Kpi label="Posts" value={posts} icon={<Send size={15} />} delta={<span>{r.posts.hourly_published} hourly · {r.posts.spike_published} spike · {r.posts.failed} failed</span>} />
          <Kpi label="Clips approved" value={r.clips.approved} icon={<Film size={15} />} delta={<span>{r.clips.rejected} rejected · {r.clips.expired} expired</span>} />
          <Kpi label="Spend" value={spent} prefix="$" decimals={2} icon={<DollarSign size={15} />} iconTone={over ? "red" : undefined} delta={<span className={over ? "down" : undefined}>{over ? "over a budget" : "under all budgets"}</span>} />
        </Grid>
      </AnimatedContent>

      <div className="g mt2 collapse-md" style={{ gridTemplateColumns: "1.3fr 1fr" }}>
        <Card>
          <CardHead title="Accounts" />
          <DataTable dense caption="Accounts" rowKey={(a) => a.account_id} rows={r.accounts} cols={[
            { key: "a", head: "Account", render: (a) => <span className="row"><Plat p={a.platform} /> <span className="semi" title={a.account_id}>{a.handle ?? a.account_id}</span>{!a.enabled && <Badge status="disabled" />}</span> },
            { key: "f", head: "Followers", align: "right", render: (a) => fmtNum(a.followers) },
            { key: "c", head: "Change", align: "right", render: (a) => a.followers_change === null ? "—" : <Delta v={a.followers_change} /> },
            { key: "v", head: "Views", align: "right", render: (a) => fmtCompact(a.views_change) },
            { key: "m", head: "Median / post", align: "right", render: (a) => (a.enabled ? fmtCompact(r.views.median_by_platform[a.platform]) : "—") },
            { key: "p", head: "Posts", align: "right", render: (a) => <span>{a.published}{a.failed ? <span className="down"> · {a.failed} failed</span> : null}</span> },
          ]} />
        </Card>
        <Card>
          <CardHead title="Top clips" hint="By views over the last week" />
          {r.top_clips.length ? r.top_clips.map((c, i) => (
            <div key={i} className="row" style={{ gap: 12, padding: "8px 0" }}>
              <div style={{ width: 52, flex: "none" }}><Thumb seed={c.title} /></div>
              <div className="grow" style={{ minWidth: 0 }}><div className="semi ellipsis">{c.title || "Untitled"}</div><div className="muted xs row" style={{ gap: 6 }}><Plat p={c.platform} />{c.post_url && <ExtLink href={c.post_url}>open</ExtLink>}</div></div>
              <div style={{ textAlign: "right" }}><div className="semi num">{fmtCompact(c.views)}</div><div className="muted xs">views</div></div>
            </div>
          )) : <div className="muted small">No published posts with views yet.</div>}
        </Card>
      </div>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
        <Card>
          <CardHead title="YouTube Partner Program" hint="reports.ypp_targets" />
          <div className="row" style={{ gap: 16, justifyContent: "center", flexWrap: "wrap" }}>
            <Ring size={110} fs={20} label="Subscribers toward target" pct={r.ypp.subscribers !== null && r.ypp.subscribers_target ? (r.ypp.subscribers / r.ypp.subscribers_target) * 100 : 0}
              text={fmtNum(r.ypp.subscribers)} sub={`of ${fmtNum(r.ypp.subscribers_target)} subs`} />
            <Ring size={110} fs={20} color="#12A150" track="#E5F7EC" label="Shorts views in 90 days toward target" pct={r.ypp.shorts_views_target ? (r.ypp.shorts_views_90d / r.ypp.shorts_views_target) * 100 : 0}
              text={fmtCompact(r.ypp.shorts_views_90d)} sub={`of ${fmtCompact(r.ypp.shorts_views_target)} / 90d`} />
          </div>
        </Card>
        <Card>
          <CardHead title="Spike funnel" hint={`median spike → post ${fmtDuration(r.spikes.median_spike_to_post_s)}`} />
          <HBars labelWidth={110} items={[{ label: "Detected", value: r.spikes.detected }, { label: "Tier S", value: r.spikes.S }, { label: "Tier A", value: r.spikes.A }, { label: "Tier B", value: r.spikes.B }, { label: "Rejected", value: r.spikes.rejected, tone: "amber" }]} />
          <div className="flabel mt">Gate rejects</div>
          <HBars labelWidth={150} items={Object.entries(r.clips.rejected_by_reason).map(([k, v]) => ({ label: humanize(k), value: v }))} />
        </Card>
        <Card>
          <CardHead title="Guardian & issues" />
          <KV rows={[["Claims", r.guardian.claims], ["Takedowns", `${r.guardian.takedowns} actioned · ${r.guardian.emails_open} open`],
            ["Strikes", <span key="s" className={r.guardian.strikes >= r.guardian.strike_limit ? "down" : undefined}>{r.guardian.strikes} of {r.guardian.strike_limit}</span>],
            ["Emergency stop", r.guardian.emergency_stop ? <Badge key="e" status="stopped" /> : <Badge key="e" tone="green">off</Badge>],
            ["Discovery", `${fmtNum(r.discovery.candidates_checked)} checked · ${r.discovery.new_creators} new creators`]]} />
          <ul className="feed mt" style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {r.guardian.manual_removals.map((m, i) => (
              <li key={`m${i}`} className="fi"><span className="ficon red"><Trash2 size={15} /></span><div><div className="t1">Still needs a manual delete</div><div className="t2">{m.platform}{m.post_url && <> · <ExtLink href={m.post_url}>open post</ExtLink></>}</div></div></li>
            ))}
            {r.issues.map((s, i) => (
              <li key={i} className="fi"><span className="ficon amber"><AlertTriangle size={15} /></span><div><div className="t2" style={{ marginTop: 0, color: "var(--ink2)" }}>{s}</div></div></li>
            ))}
            {!r.issues.length && !r.guardian.manual_removals.length && <li className="muted small">No issues that day.</li>}
          </ul>
        </Card>
      </div>
    </>
  );
}
