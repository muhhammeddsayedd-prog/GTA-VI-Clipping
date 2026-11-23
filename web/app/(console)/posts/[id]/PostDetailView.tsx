"use client";

import { useState } from "react";
import { ArrowUpRight, Code, Copy, ExternalLink } from "lucide-react";

import CountUp from "@/components/reactbits/CountUp";
import SpotlightCard from "@/components/reactbits/SpotlightCard";
import { HBars } from "@/components/kit/charts";
import { JsonBlock, Steps, TraceBar } from "@/components/kit/data";
import { Drawer, useToast } from "@/components/kit/overlays";
import { Badge, Banner, Btn, Card, CardHead, ErrorState, KV, PageHead, Plat, platformName, SkeletonCards, When } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Event, PostDetail } from "@/lib/api/types";
import { fmtAgo, fmtDuration, fmtTime, humanize, series, zoneLabel } from "@/lib/format";
import { AccountName } from "@/components/domain/AccountName";

function stepOf(e: Event): "done" | "fail" | "run" {
  if (e.level === "error" || e.level === "critical" || /failed/.test(e.type)) return "fail";
  if (/pending|retry/.test(e.type)) return "run";
  return "done";
}

export function PostDetailView({ id }: { id: string }) {
  const q = useApi<PostDetail>(`admin/posts/${id}`, undefined, { interval: POLL.detail });
  const [raw, setRaw] = useState(false);
  const toast = useToast();
  if (q.error && !q.data) return <><PageHead title="Post" back="/posts" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Post" back="/posts" sub="Loading…" /><SkeletonCards n={2} h={300} /></>;
  const p = q.data;
  const url = p.post_url && /^https?:\/\//.test(p.post_url) ? p.post_url : null;
  const latency = p.published_at ? (Date.parse(p.published_at) - Date.parse(p.created_at)) / 1000 : null;
  const timeline = [...p.events].filter((e) => e.type.startsWith("post.") || e.type.startsWith("clip.posted")).sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  const views = p.views ?? 0;
  const rate = (n: number | null) => (views && n !== null ? Math.round((n / views) * 1000) / 10 : 0);

  return (
    <>
      <PageHead back="/posts" title="Post"
        sub={<span className="row" style={{ gap: 8, flexWrap: "wrap" }}><Plat p={p.platform} /> <AccountName id={p.account_id} /> · clip <span className="pc semi">{series(p.clip_series_no)}</span> <Badge status={p.status} /></span>}
        actions={<>
          <Btn icon={<Code size={15} />} onClick={() => setRaw(true)}>Raw JSON</Btn>
          {url && <Btn kind="p" href={url} external icon={<ExternalLink size={15} />}>Open on {platformName(p.platform)}</Btn>}
        </>} />
      <TraceBar trace={p.trace} on="posts" />

      {p.status === "remove_failed" && (
        <Banner tone="red"><b>Delete this post by hand.</b> {platformName(p.platform)} has no delete API. Open the app as the account owner, find the video and delete it; the guardian clears this on its next check.</Banner>
      )}
      {p.status === "failed" && p.error && <Banner tone="red"><b>Upload failed:</b> {humanize(p.error)} after {p.attempts} attempt{p.attempts === 1 ? "" : "s"}.</Banner>}
      {p.status === "pending" && !url && <Banner tone="blue">Waiting for {platformName(p.platform)} to return a link.{p.platform === "tiktok" ? " TikTok can take hours to give a post ID, or never does for private posts." : ""}</Banner>}

      <div className="g collapse-md" style={{ gridTemplateColumns: "1.2fr 1fr" }}>
        <SpotlightCard className="pcard" spotlightColor="rgba(255,255,255,.18)" theme="dark">
          <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
            <div><div className="lbl">Post</div><div className="big"><AccountName id={p.account_id} /> · {series(p.clip_series_no)}</div></div>
            <span className="dbadge" style={{ background: "#fff", color: p.status === "published" ? "var(--green)" : "var(--ink)" }}>{humanize(p.status)}</span>
            <div className="right row" style={{ gap: 12 }}><Plat p={p.platform} lg /><div><div className="semi">{p.lane === "spike" ? "Spike lane" : "Hourly lane"}</div><div className="lbl">{p.dry_run ? "dry run" : p.slot_at ? `slot ${fmtTime(p.slot_at)}` : "posted immediately"}</div></div></div>
          </div>
          <div className="g g4 mt2">
            {([["Views", p.views], ["Likes", p.likes], ["Comments", p.comments], ["Shares", p.shares]] as const).map(([k, v]) => (
              <div className="glass" key={k}><span className="ar"><ArrowUpRight size={14} /></span>
                <div className="v num">{v === null ? "—" : <CountUp to={v} separator="," duration={1.2} trigger="load" />}</div><div className="k">{k}</div></div>
            ))}
          </div>
          <div className="pfoot" style={{ flexWrap: "wrap" }}>
            <div><div className="k">Published</div><div className="v">{p.published_at ? `${fmtTime(p.published_at, true)} ${zoneLabel()}` : "—"}</div></div>
            <div><div className="k">{p.lane === "spike" ? "Request → live" : "Upload took"}</div><div className="v">{latency !== null ? fmtDuration(latency) : "—"}</div></div>
            <div><div className="k">Stats updated</div><div className="v">{p.stats_updated_at ? fmtAgo(p.stats_updated_at) : "—"}</div></div>
            <div className="right row">
              {url && <button type="button" className="wcirc" aria-label="Copy post link" style={{ background: "none" }}
                onClick={() => navigator.clipboard.writeText(url).then(() => toast("Link copied"), () => toast("Could not copy", true))}><Copy size={15} /></button>}
              {url && <a className="btn w" href={url} target="_blank" rel="noopener noreferrer">Open post</a>}
            </div>
          </div>
        </SpotlightCard>
        <Card>
          <CardHead title="Engagement" hint="Share of viewers who reacted · from Upload-Post analytics" />
          {views ? (
            <HBars labelWidth={110} unit="%" items={[
              { label: "Likes / view", value: rate(p.likes) },
              { label: "Comments / view", value: rate(p.comments) },
              { label: "Shares / view", value: rate(p.shares) },
            ]} />
          ) : <div className="muted small">No views recorded yet. Stats are collected every 2 hours for 7 days after posting.</div>}
          <div className="mt"><KV rows={[["Claim check", p.claim_status ? <Badge key="c" status={p.claim_status} /> : "not checked yet"], ["Last claim check", p.claim_checked_at ? fmtAgo(p.claim_checked_at) : "—"]]} /></div>
        </Card>
      </div>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Card>
          <CardHead title="Publishing timeline" hint={`attempt ${p.attempts} of 3`} />
          {timeline.length ? (
            <Steps steps={timeline.map((e) => ({ state: stepOf(e), title: e.message ?? humanize(e.type), sub: <span className="mono">{e.type}</span>, time: <When iso={e.created_at} seconds /> }))} />
          ) : <div className="muted small">No publishing events recorded.</div>}
        </Card>
        <Card>
          <CardHead title="Details" />
          <KV rows={[
            ["Account", <AccountName key="acc" id={p.account_id} withId />],
            ["Lane", p.lane],
            ["Slot", p.slot_at ? `${fmtTime(p.slot_at)} ${zoneLabel()}` : "— (spike lane)"],
            ["Dry run", p.dry_run ? "yes" : "no"],
            ["Attempts", String(p.attempts)],
            ["Upload request", <span key="u" className="mono">{p.upload_request_id ?? "—"}</span>],
            ["Idempotency key", <span key="i" className="mono xs">{p.idempotency_key}</span>],
            ["Platform post id", <span key="pp" className="mono">{p.platform_post_id ?? "—"}</span>],
            ["Removed", p.removed_at ? `${fmtTime(p.removed_at)} · ${p.remove_reason ?? ""}` : p.remove_reason ?? "—"],
          ]} />
        </Card>
      </div>

      <Drawer open={raw} onClose={() => setRaw(false)} title="Raw post JSON" sub={p.id} width={600}>
        <JsonBlock value={p} maxHeight={760} label="Post JSON" />
      </Drawer>
    </>
  );
}
