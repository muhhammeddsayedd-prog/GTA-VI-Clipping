"use client";

import Link from "next/link";
import { useState } from "react";
import { ArrowUpRight, Check, Copy } from "lucide-react";

import SpotlightCard from "@/components/reactbits/SpotlightCard";
import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { Ring } from "@/components/kit/charts";
import { DataTable, Pager, type Col } from "@/components/kit/data";
import { useToast } from "@/components/kit/overlays";
import { Badge, Card, CardHead, ErrorState, Grid, KV, PageHead, Plat, RefreshBtn, Seg, SkeletonCards, SkeletonTable, When, Dash } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Guardian, Page, Post, Takedown } from "@/lib/api/types";
import { takedownSummary } from "@/lib/takedown";
import { fmtAgo, fmtDay, series } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

const KEYS = ["status"];
const DEFAULTS = {};

export const DELETE_STEPS: Record<string, string[]> = {
  tiktok: ["Open the TikTok app as the posting account", "Profile → the video → ··· → Delete", "The guardian sees it gone on its next check and clears this item"],
  instagram: ["Open Instagram as the posting account", "Profile → Reels → the reel → ··· → Delete", "The guardian sees it gone on its next check and clears this item"],
  youtube: ["Open YouTube Studio for the channel", "Content → Shorts → the video → ⋮ → Delete forever", "The guardian sees it gone on its next check and clears this item"],
};

function tdActions(t: Takedown): string { return takedownSummary(t.status, t.actions); }

const TD_COLS: Col<Takedown>[] = [
  { key: "at", head: "Received", render: (t) => <When iso={t.received_at} /> },
  { key: "from", head: "From", render: (t) => <span className="ellipsis" style={{ display: "block", maxWidth: 200 }}>{t.sender ?? "—"}</span> },
  { key: "subj", head: "Subject", render: (t) => <span className="ellipsis" style={{ display: "block", maxWidth: 360 }}>{t.subject ?? "—"}</span> },
  { key: "creator", head: "Creator", render: (t) => (t.creator ? t.creator.handle : <span className="muted">{t.is_takedown ? "no match" : "—"}</span>) },
  { key: "status", head: "Status", render: (t) => <Badge status={t.status} /> },
  { key: "acts", head: "Actions", render: (t) => <span className={t.status === "open" ? "down semi" : undefined}>{tdActions(t)}</span> },
];

export function GuardianView() {
  const g = useApi<Guardian>("admin/guardian", undefined, { interval: POLL.list });
  const list = useListParams(KEYS, DEFAULTS);
  const td = useApi<Page<Takedown>>("admin/takedowns", list.params, { interval: POLL.list });
  const tc = td.data?.counts ?? {};
  const status = list.get("status");

  return (
    <>
      <PageHead title="Guardian" sub="Claims, takedowns, strikes and removals · the one place the system asks for you"
        actions={<RefreshBtn onClick={() => { g.refetch(); td.refetch(); }} spinning={g.isFetching} />} />
      {g.error && !g.data ? <ErrorState error={g.error} retry={() => g.refetch()} /> : !g.data ? <SkeletonCards n={3} h={200} /> : (
        <>
          <AnimatedContent distance={20} duration={0.5} trigger="mount">
            <Grid cols={3}>
              <Strikes g={g.data} />
              <Card>
                <CardHead title="Claims check" hint={`Last run ${g.data.claims.last_run_at ? fmtAgo(g.data.claims.last_run_at) : "never"}`}
                  actions={<Badge status={g.data.claims.removed_by_platform ? "claimed" : g.data.claims.suspect ? "suspect" : "ok"} />} />
                <KV rows={[
                  ["OK", g.data.claims.ok],
                  ["Suspect", <span key="s" className={g.data.claims.suspect ? "warn" : undefined}>{g.data.claims.suspect}</span>],
                  ["Unverifiable", <span key="u">{g.data.claims.unverifiable}{g.data.claims.unverifiable ? <span className="muted"> · e.g. private YouTube</span> : null}</span>],
                  ["Removed by platform", <span key="r" className={g.data.claims.removed_by_platform ? "down" : undefined}>{g.data.claims.removed_by_platform}</span>],
                ]} />
              </Card>
              <Card>
                <CardHead title="Takedown inbox" hint="Gmail → n8n every 5 min → Claude P7"
                  actions={g.data.takedown_counts.open ? <Badge tone="red">{g.data.takedown_counts.open} need{g.data.takedown_counts.open === 1 ? "s" : ""} you</Badge> : <Badge tone="green">clear</Badge>} />
                <KV rows={[
                  ["Open", <span key="o" className={g.data.takedown_counts.open ? "down" : undefined}>{g.data.takedown_counts.open}</span>],
                  ["Actioned", g.data.takedown_counts.actioned],
                  ["Partial", g.data.takedown_counts.partial],
                  ["Ignored (not takedowns)", g.data.takedown_counts.ignored],
                ]} />
              </Card>
            </Grid>
          </AnimatedContent>
          <div className="mt2"><ManualRemovals posts={g.data.manual_removals} /></div>
        </>
      )}

      <Card style={{ marginTop: 16 }}>
        <CardHead title="Takedowns" hint="Emails, platform removals and claims"
          actions={<Seg label="Takedown status" value={status} onChange={(v) => list.set("status", v)} items={[
            { value: "", label: "All", count: tc.all }, { value: "open", label: "Open", count: tc.open ?? 0 }, { value: "actioned", label: "Actioned", count: tc.actioned ?? 0 },
            { value: "partial", label: "Partial", count: tc.partial ?? 0 }, { value: "ignored", label: "Ignored", count: tc.ignored ?? 0 }]} />} />
        {td.error && !td.data ? <ErrorState error={td.error} retry={() => td.refetch()} /> : !td.data ? <SkeletonTable rows={4} /> : (
          <>
            <DataTable cols={TD_COLS} rows={td.data.items} rowKey={(t) => t.id} href={(t) => `/guardian/takedowns/${t.id}`} dense caption="Takedowns" empty="No takedowns in this view." />
            <Pager shown={td.data.items.length} total={status ? tc[status] : tc.all} cursors={list.cursors} next={td.data.next_cursor} onPage={list.setCursors} noun="takedowns" />
          </>
        )}
      </Card>
    </>
  );
}

function Strikes({ g }: { g: Guardian }) {
  const s = g.strikes;
  const full = s.count >= s.limit;
  const oldest = s.items[s.items.length - 1];
  const expires = oldest ? new Date(Date.parse(oldest.at) + s.window_d * 86400e3).toISOString() : null;
  return (
    <Card>
      <CardHead title="Strikes" hint={`Rolling ${s.window_d} days · ${s.limit} strikes → emergency stop`} />
      <div className="row" style={{ gap: 18, flexWrap: "wrap" }}>
        <Ring pct={(s.count / Math.max(s.limit, 1)) * 100} size={110} color={full ? "#E5484D" : s.count ? "#D27A06" : "#12A150"} track={full ? "#FDEBEC" : s.count ? "#FEF2DF" : "#E5F7EC"}
          text={`${s.count} / ${s.limit}`} sub="strikes" fs={20} label={`${s.count} of ${s.limit} strikes in ${s.window_d} days`} />
        <div className="grow" style={{ minWidth: 170 }}>
          <KV rows={[
            ["Latest", s.items[0] ? `${s.items[0].creator?.handle ?? "unknown"} · ${fmtDay(s.items[0].at)}` : "none"],
            ["Next expiry", expires ? fmtDay(expires) : "—"],
            ["Emergency stop", <Badge key="e" status={g.emergency_stop ? "stopped" : "off"} tone={g.emergency_stop ? "red" : "green"}>{g.emergency_stop ? "on" : "off"}</Badge>],
          ]} />
        </div>
      </div>
      {g.emergency_stop && g.stop_reason && <div className="xs down" style={{ marginTop: 10 }}>Stop reason: {g.stop_reason}</div>}
    </Card>
  );
}

function ManualRemovals({ posts }: { posts: Post[] }) {
  const [sel, setSel] = useState(0);
  const toast = useToast();
  const p = posts[Math.min(sel, posts.length - 1)];
  return (
    <section className="dpanel" aria-labelledby="manual-h">
      <div className="row">
        <h2 id="manual-h" style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Delete by hand</h2>
        <span className="right dbadge" style={posts.length ? { background: "var(--red)", color: "#fff" } : undefined}>{posts.length ? `${posts.length} waiting` : "nothing waiting"}</span>
      </div>
      <div className="xs" style={{ color: "var(--dmute)", marginTop: 6 }}>Posts the system could not remove (TikTok and Instagram have no delete API) · repeated in every daily report until gone</div>
      {!posts.length ? (
        <div className="row" style={{ padding: "26px 4px", color: "#CFCFE0", gap: 10 }}><Check size={18} /> Nothing to delete by hand.</div>
      ) : (
        <div className="g mt collapse-md" style={{ gridTemplateColumns: "1fr 1fr" }}>
          <ul className="dlist" style={{ margin: 0, listStyle: "none", padding: 0 }}>
            {posts.map((x, i) => (
              <li key={x.id}>
                <button type="button" className={`ditem link${i === sel ? " on" : ""}`} aria-pressed={i === sel} onClick={() => setSel(i)}
                  style={{ width: "100%", border: 0, textAlign: "left", font: "inherit", color: "inherit", background: i === sel ? undefined : "transparent" }}>
                  <Plat p={x.platform} />
                  <div style={{ minWidth: 0 }}><div className="t1 ellipsis">{x.account_id} · {series(x.clip_series_no)} · {x.clip_hook ?? "clip"}</div>
                    <div className="t2">{x.claim_status ?? x.status} · {x.published_at ? `posted ${fmtDay(x.published_at)}` : "—"}</div></div>
                  <span className="amt"><span className="dbadge">{x.platform}</span></span>
                </button>
              </li>
            ))}
          </ul>
          {p && (
            <SpotlightCard className="pcard" spotlightColor="rgba(255,255,255,.16)" theme="dark">
              <div className="lbl">Delete by hand · {p.platform === "tiktok" ? "TikTok" : p.platform === "instagram" ? "Instagram" : "YouTube"}</div>
              <div className="big" style={{ marginTop: 4 }}>{series(p.clip_series_no)} on {p.account_id}</div>
              <ol style={{ marginTop: 14, lineHeight: 1.9, color: "#EEEBFF", padding: 0, listStyle: "none" }}>
                {(DELETE_STEPS[p.platform] ?? DELETE_STEPS.tiktok).map((s, i) => <li key={i}><b>{i + 1}</b> · {s}</li>)}
              </ol>
              <div className="pfoot" style={{ flexWrap: "wrap" }}>
                <div><div className="k">Why manual</div><div className="v">{p.platform === "youtube" ? "API delete failed" : "No delete API on this platform"}</div></div>
                <div className="right row">
                  {p.post_url && (
                    <button type="button" className="wcirc" aria-label="Copy post link" style={{ background: "transparent" }}
                      onClick={() => navigator.clipboard.writeText(p.post_url!).then(() => toast("Link copied"), () => toast("Could not copy", true))}><Copy size={15} /></button>
                  )}
                  {p.post_url ? <a className="btn w" href={p.post_url} target="_blank" rel="noopener noreferrer">Open post <ArrowUpRight size={14} /></a>
                    : <Link className="btn w" href={`/posts/${p.id}`}>Open post record</Link>}
                </div>
              </div>
            </SpotlightCard>
          )}
        </div>
      )}
      {posts.length > 0 && !p?.post_url && <div className="xs" style={{ color: "var(--dmute)", marginTop: 10 }}><Dash /> No public link was recorded for this post.</div>}
    </section>
  );
}
