"use client";

import Link from "next/link";
import { useState } from "react";
import { Activity, ArrowUpRight, Bolt, Check, FileText, Layers, Link2, List, Play, Send, Users } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import SpotlightCard from "@/components/reactbits/SpotlightCard";
import CountUp from "@/components/reactbits/CountUp";
import { Bars, Line, Ring } from "@/components/kit/charts";
import { Delta, EventFeed, Kpi, Thumb } from "@/components/kit/data";
import { AccountCard } from "@/components/domain/AccountCard";
import { Avatar, Badge, Btn, Card, CardHead, ErrorState, Grid, KV, PageHead, Plat, RefreshBtn, Seg, SkeletonCards, Dash } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Overview, QueueItem } from "@/lib/api/types";
import { fmtClock, fmtDay, fmtDuration, fmtNum, fmtTime, fmtUsd, series } from "@/lib/format";

const PROVIDER: Record<string, string> = { anthropic: "Anthropic", transcription: "Transcribe", openai: "Transcribe", groq: "Transcribe", apify: "Apify", youtube: "YouTube", upload_post: "Upload-Post" };

export function OverviewView() {
  const q = useApi<Overview>("admin/overview", undefined, { interval: POLL.status });
  if (q.error && !q.data) return <><PageHead title="Overview" sub="Live status of the 6 Stars pipeline" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Overview" sub="Loading live status…" /><SkeletonCards n={4} /><div className="mt2"><SkeletonCards n={4} h={120} /></div></>;
  const o = q.data;
  const hourLabels = o.posts_24h.by_hour.map((_, i) => {
    const h = (new Date(o.now).getUTCHours() - 23 + i + 24) % 24;
    return h % 3 === 0 ? String(h) : "";
  });
  const queueTotal = o.queue.hourly + o.queue.spike;
  const top = o.spend.by_provider.filter((p) => p.budget_usd !== null).slice(0, 3);

  return (
    <>
      <PageHead
        title="Overview"
        sub={<>Live status of the 6 Stars pipeline · {fmtDay(o.now)} · {fmtTime(o.now)} UTC</>}
        actions={<><RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} /><Btn kind="p" href="/reports" icon={<FileText size={15} />}>Today’s report</Btn></>}
      />

      <AnimatedContent distance={24} duration={0.6} stagger={0.08} trigger="mount">
        <Grid cols={4}>
          <Kpi label="Posts published · 24h" value={o.posts_24h.published} icon={<Send size={15} />}
            delta={<><span>{o.posts_24h.failed} failed</span></>}
            chart={<Bars label="Posts per hour, last 24 hours" values={o.posts_24h.by_hour} labels={hourLabels} />} />
          <Kpi label="Approved queue" value={queueTotal} unit="clips" icon={<Layers size={15} />} iconTone={queueTotal < o.queue.low_watermark ? "amber" : undefined}
            delta={<span>Hourly {o.queue.hourly} · Spike {o.queue.spike} · low mark {o.queue.low_watermark}</span>}
            chart={<QueueGauge total={queueTotal} low={o.queue.low_watermark} />} />
          <Kpi label="Spike → post · median" value={o.express.p50_s} unit="s" icon={<Bolt size={15} />}
            delta={<span>p90 {o.express.p90_s !== null ? `${o.express.p90_s} s` : "—"} · target under 240 s</span>}
            chart={<Line label="Express latency over the last day" values={o.express.series.map((p) => p.s)} target={240} />} />
          <SpendCard total={o.spend.total_usd} budget={o.spend.budget_usd} providers={top} />
        </Grid>
      </AnimatedContent>

      <div className="filters mt2">
        <span className="lab">Accounts <span className="cnt">{o.accounts.length}</span></span>
        <span className="muted small hide-sm">Caps are rolling 24h windows · min gap between posts per account</span>
        <span className="right"><Btn sm href="/accounts" icon={<Users size={15} />}>All accounts</Btn></span>
      </div>
      <AnimatedContent distance={20} duration={0.5} stagger={0.06} trigger="mount" delay={0.15}>
        <Grid cols={4}>{o.accounts.map((a) => <AccountCard key={a.id} a={a} />)}</Grid>
      </AnimatedContent>

      <div className="mt2"><PostingPanel items={o.up_next} /></div>

      <div className="g mt2 collapse-md" style={{ gridTemplateColumns: "1.25fr 1fr 1fr" }}>
        <Card>
          <CardHead title="What just happened" hint="Notable events · all services" actions={<Btn sm href="/events" icon={<List size={15} />}>Events</Btn>} />
          <EventFeed events={o.recent_events} />
        </Card>
        <Card>
          <CardHead title="System health" hint="Heartbeats, triggers and incidents"
            actions={o.health.open_incidents.length ? <Badge tone="red">{o.health.open_incidents.length} incident{o.health.open_incidents.length > 1 ? "s" : ""} open</Badge> : <Badge tone="green">all clear</Badge>} />
          <KV rows={[
            ["Services", <span key="s" className={o.health.services_ok < o.health.services_total ? "down" : "up"}>{o.health.services_ok} / {o.health.services_total} up</span>],
            ["n8n triggers", <span key="t" className={o.health.triggers_ok < o.health.triggers_total ? "down" : "up"}>{o.health.triggers_ok} / {o.health.triggers_total} on time</span>],
            ["Live slots", `${o.health.live.active} of ${o.health.live.max_slots} recording`],
            ["Jobs", `${o.health.jobs.queued} queued · ${o.health.jobs.running} running · ${o.health.jobs.failed_24h} failed 24h`],
            ...o.health.open_incidents.slice(0, 2).map((i) => [<span key={i.key} className="down">Open incident</span>, <Link key={`l${i.key}`} href="/health" className="down">{i.title} · {fmtDuration((Date.parse(o.now) - Date.parse(i.opened_at)) / 1000)}</Link>] as [React.ReactNode, React.ReactNode]),
          ]} />
          <div className="mt"><Btn sm href="/health" icon={<Activity size={15} />}>Health</Btn></div>
        </Card>
        <Card>
          <CardHead title="Growth" hint="Followers and YouTube Partner progress" actions={<Btn sm href="/reports" icon={<FileText size={15} />}>Reports</Btn>} />
          <div className="row" style={{ gap: 18, flexWrap: "wrap" }}>
            <Ring label="YouTube subscribers toward 1,000" size={96} fs={17}
              pct={o.growth.ypp.subscribers !== null ? (o.growth.ypp.subscribers / o.growth.ypp.subscribers_target) * 100 : 0}
              sub={`${fmtNum(o.growth.ypp.subscribers_target)} subs`} />
            <Ring label="Shorts views in 90 days toward 10 million" size={96} fs={17} color="#12A150" track="#E5F7EC"
              pct={o.growth.ypp.shorts_views_90d !== null ? (o.growth.ypp.shorts_views_90d / o.growth.ypp.views_target) * 100 : 0}
              sub="10M views/90d" />
            <div className="grow" style={{ minWidth: 160 }}>
              <KV rows={o.growth.accounts.map((g) => [
                <span key={g.account_id} className="row" style={{ gap: 6 }}><Plat p={g.platform} />{g.account_id}</span>,
                <span key={`v${g.account_id}`}>{fmtNum(g.followers)} {g.delta_24h !== null && <Delta v={g.delta_24h} />}</span>,
              ])} />
            </div>
          </div>
        </Card>
      </div>
    </>
  );
}

function QueueGauge({ total, low }: { total: number; low: number }) {
  const max = Math.max(total, low * 2, 1);
  return (
    <div aria-label={`${total} clips approved; low mark ${low}`} role="img">
      <div style={{ position: "relative", height: 74, display: "flex", alignItems: "flex-end", gap: 4 }}>
        {Array.from({ length: 24 }).map((_, i) => {
          const filled = i < Math.round((total / max) * 24);
          return <div key={i} style={{ flex: 1, height: `${30 + (i % 4) * 8}%`, borderRadius: 4, background: filled ? (total < low ? "var(--amber)" : i > 18 ? "var(--p)" : "var(--ps2)") : "var(--grs)", transition: "background .4s" }} />;
        })}
        <div style={{ position: "absolute", left: `${(low / max) * 100}%`, top: 0, bottom: 0, borderLeft: "1.5px dashed var(--red)" }} title="low mark" />
      </div>
      <div className="muted xs" style={{ marginTop: 4 }}>dashed line = low mark ({low})</div>
    </div>
  );
}

function SpendCard({ total, budget, providers }: { total: number; budget: number; providers: { provider: string; usd: number; budget_usd: number | null }[] }) {
  return (
    <Card as="div">
      <div className="kpi">
        <div className="lbl">Spend today
          <Link href="/spend" className="cic" aria-label="Open spend" style={{ background: "var(--dark)", color: "#fff", borderColor: "var(--dark)" }}><ArrowUpRight size={15} /></Link>
        </div>
        <div className="row" style={{ marginTop: 16, alignItems: "baseline", flexWrap: "wrap" }}>
          <div className="val num" style={{ margin: 0 }}><small>$</small><CountUp to={total} decimals={2} duration={1.1} trigger="load" /></div>
          <span className="chip right">of {fmtUsd(budget)} budget</span>
        </div>
        <div className="row" style={{ gap: 8, marginTop: 16, alignItems: "flex-end" }}>
          {providers.map((p, i) => {
            const hot = i === 1;
            const pct = p.budget_usd ? p.usd / p.budget_usd : 0;
            return (
              <div key={p.provider} style={{ flex: 1, borderRadius: 14, padding: 10, height: hot ? 104 : 84, background: hot ? "var(--p)" : "var(--grs)", color: hot ? "#fff" : undefined, display: "flex", flexDirection: "column" }}>
                <div className="xs" style={{ color: hot ? "#D7D2FF" : "var(--mute)" }}>{PROVIDER[p.provider] ?? p.provider}</div>
                <div className="semi" style={{ marginTop: "auto" }}>{fmtUsd(p.usd)}</div>
                <div className="xs" style={{ color: hot ? "#D7D2FF" : pct > 0.9 ? "var(--red)" : "var(--mute)" }}>of {fmtUsd(p.budget_usd, p.budget_usd! < 1 ? 2 : 0)}</div>
              </div>
            );
          })}
        </div>
      </div>
    </Card>
  );
}

function PostingPanel({ items }: { items: QueueItem[] }) {
  const [sel, setSel] = useState(0);
  const [tab, setTab] = useState("next");
  const c = items[sel];
  return (
    <section className="dpanel" aria-labelledby="posting-h">
      <div className="row mb" style={{ flexWrap: "wrap" }}>
        <h2 id="posting-h" style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>Posting</h2>
        <div className="right"><Seg dark label="Posting view" value={tab} onChange={(v) => { setTab(v); if (v !== "next") window.location.assign(v === "posted" ? "/posts?status=published" : "/posts?status=failed"); }}
          items={[{ value: "next", label: "Up next", count: items.length }, { value: "posted", label: "Last posted" }, { value: "failed", label: "Failed" }]} /></div>
      </div>
      {!items.length ? (
        <div style={{ padding: "30px 10px", color: "var(--dmute)" }}>Nothing approved is waiting. The next approved clip will appear here.</div>
      ) : (
        <div className="g collapse-md" style={{ gridTemplateColumns: "400px 1fr" }}>
          <div>
            <div className="row"><h3>Up next</h3><span className="right xs" style={{ color: "var(--dmute)" }}>ranked by rank_score</span></div>
            <ul className="dlist" style={{ listStyle: "none", padding: 0 }}>
              {items.map((it, i) => (
                <li key={it.clip_id}>
                  <button type="button" className={`ditem link${i === sel ? " on" : ""}`} onClick={() => setSel(i)} aria-pressed={i === sel} style={{ width: "100%", border: 0, background: i === sel ? undefined : "transparent", textAlign: "left", font: "inherit", color: "inherit" }}>
                    <span className="dbadge" style={{ minWidth: 52, textAlign: "center" }}>{it.lane === "spike" ? "spike" : `#${i + 1}`}</span>
                    <div style={{ minWidth: 0 }}><div className="t1 ellipsis">{series(it.series_no)} · {it.title ?? it.hook}</div><div className="t2">{it.lane} · {it.creator.handle}</div></div>
                    <span className="amt">{it.score ?? "—"}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
          {c && (
            <SpotlightCard className="pcard" spotlightColor="rgba(255,255,255,.18)" theme="dark">
              <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
                <div><div className="lbl">Clip</div><div className="big">{series(c.series_no)}</div></div>
                <span className="dbadge" style={{ background: "rgba(255,255,255,.18)", color: "#fff" }}>approved</span>
                <div className="right row" style={{ gap: 22 }}>
                  <div><div className="lbl">Creator</div><div className="row semi" style={{ marginTop: 4, gap: 6 }}><Plat p={c.creator.platform} />{c.creator.handle}</div></div>
                  <div className="row"><Avatar name={c.creator.handle} size={38} /><div><div className="semi">{c.lane === "spike" ? "Spike lane" : "Hourly lane"}</div><div className="lbl">{c.expires_at ? `expires ${fmtTime(c.expires_at)}` : ""}</div></div></div>
                </div>
              </div>
              <div className="row" style={{ gap: 18, marginTop: 20, alignItems: "stretch" }}>
                <div style={{ width: 118, flex: "none" }}><Thumb seed={c.clip_id} hook={c.hook} dur={fmtClock(c.duration_s)} /></div>
                <div className="g g3 grow" style={{ alignContent: "start" }}>
                  <div className="glass"><span className="ar"><ArrowUpRight size={14} /></span><div className="v">{c.score ?? "—"}</div><div className="k">Writer score</div></div>
                  <div className="glass"><span className="ar"><ArrowUpRight size={14} /></span><div className="v">{c.rank_score?.toFixed(2) ?? "—"}</div><div className="k">Rank score</div></div>
                  <div className="glass"><span className="ar"><ArrowUpRight size={14} /></span><div className="v">{c.duration_s ? `${Math.round(c.duration_s)} s` : <Dash />}</div><div className="k">Length</div></div>
                  <div className="glass"><div className="v">{fmtDuration(c.age_s)}</div><div className="k">Age</div></div>
                  <div className="glass"><div className="v">{c.lane}</div><div className="k">Lane</div></div>
                  <div className="glass dash"><div><Check size={18} /><div className="k" style={{ marginTop: 6 }}>Gate passed</div></div></div>
                </div>
              </div>
              <div className="pfoot" style={{ flexWrap: "wrap" }}>
                <div><div className="k">Title</div><div className="v ellipsis" style={{ maxWidth: 340 }}>{c.title ?? c.hook}</div></div>
                <div className="right row">
                  <Link className="wcirc" href={`/clips/${c.clip_id}`} aria-label="Open clip"><Link2 size={15} /></Link>
                  <Link className="wcirc" href={`/clips/${c.clip_id}#player`} aria-label="Play clip"><Play size={13} /></Link>
                  <Link className="btn w" href={`/clips/${c.clip_id}`}>Open clip</Link>
                </div>
              </div>
            </SpotlightCard>
          )}
        </div>
      )}
    </section>
  );
}
