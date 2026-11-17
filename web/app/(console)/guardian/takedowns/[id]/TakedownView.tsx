"use client";

import { AlertTriangle, Ban, Check, Code, Send, ShieldAlert, Trash2, X } from "lucide-react";
import { useState } from "react";

import SpotlightCard from "@/components/reactbits/SpotlightCard";
import { DataTable, EventFeed, JsonBlock, Steps, type Col } from "@/components/kit/data";
import { Modal } from "@/components/kit/overlays";
import { Badge, Banner, Btn, Card, CardHead, ErrorState, KV, PageHead, Plat, SkeletonCards, When } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Post, TakedownDetail } from "@/lib/api/types";
import { fmtDay, fmtTime, humanize, series } from "@/lib/format";

const POST_COLS: Col<Post>[] = [
  { key: "post", head: "Post", render: (p) => <span className="row" style={{ gap: 6 }}><Plat p={p.platform} /><span className="semi">{series(p.clip_series_no)}</span></span> },
  { key: "acc", head: "Account", render: (p) => p.account_id },
  { key: "status", head: "Status", render: (p) => <Badge status={p.status} /> },
];

type Action = { action?: string; outcome?: string; detail?: string; platform?: string; at?: string; post_id?: string };

const ACTION_TITLE: Record<string, string> = {
  block_creator: "Creator blocked", cancel_pending: "Pending posts cancelled", remove_post: "Post removal", strike: "Strike counted", notify: "Telegram alert",
};

function actionStep(a: Action) {
  const bad = a.outcome === "manual" || a.outcome === "failed";
  const title = a.action === "remove_post"
    ? `${a.platform ? `${a.platform[0].toUpperCase()}${a.platform.slice(1)} post` : "Post"} — ${a.outcome === "manual" ? "delete by hand" : humanize(a.outcome)}`
    : ACTION_TITLE[a.action ?? ""] ?? humanize(a.action);
  const icon = a.action === "block_creator" ? <Ban size={14} /> : a.action === "remove_post" ? (bad ? <X size={14} /> : <Trash2 size={14} />)
    : a.action === "strike" ? <AlertTriangle size={14} /> : a.action === "notify" ? <Send size={14} /> : <Check size={14} />;
  return { state: bad ? ("fail" as const) : ("done" as const), title, sub: a.detail ?? humanize(a.outcome), time: a.at ? fmtTime(a.at) : undefined, icon };
}

export function TakedownView({ id }: { id: string }) {
  const q = useApi<TakedownDetail>(`admin/takedowns/${encodeURIComponent(id)}`, undefined, { interval: POLL.list });
  const [raw, setRaw] = useState(false);
  if (q.error && !q.data) return <><PageHead title="Takedown" back="/guardian" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Takedown" back="/guardian" sub="Loading…" /><SkeletonCards n={2} /></>;
  const t = q.data;
  const acts = (Array.isArray(t.actions) ? t.actions : []) as Action[];
  const manual = acts.filter((a) => a.outcome === "manual").length;
  const platforms = Array.from(new Set(t.matched_posts.map((p) => p.platform)));
  const gmail = t.message_id ? `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(t.message_id)}` : null;

  return (
    <>
      <PageHead back="/guardian" title="Takedown"
        sub={<>{humanize(t.source)} · {t.creator?.handle ?? "no creator matched"} · <Badge status={t.status} />{manual ? ` · ${manual} post${manual > 1 ? "s" : ""} still need${manual > 1 ? "" : "s"} a manual delete` : ""}</>}
        actions={<><Btn icon={<Code size={15} />} onClick={() => setRaw(true)}>Raw JSON</Btn>{gmail && <Btn kind="p" href={gmail} external>Open in Gmail</Btn>}</>} />

      {t.status === "open" && (
        <Banner tone="red" icon={<ShieldAlert size={17} />}>
          <b>This one needs you.</b> {t.is_takedown === null ? "It could not be classified automatically." : t.creator ? "Classification finished, but nothing was actioned." : "It reads as a takedown, but nothing of ours matched it."} Check the email, then block the creator or remove posts by hand if needed.
        </Banner>
      )}

      <div className="g collapse-md" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Card>
          <CardHead title={t.source === "email" ? "Email" : humanize(t.source)} hint={t.source === "email" ? "Gmail message · labelled 6stars-processed" : undefined} />
          <KV rows={[["From", t.sender ?? "—"], ["Received", `${fmtDay(t.received_at)} · ${new Date(t.received_at).toISOString().slice(11, 16)} UTC`], ["Subject", t.subject ?? "—"]]} />
          <blockquote className="quote mt" style={{ margin: "16px 0 0" }}>{t.body_excerpt ?? "No body stored."}</blockquote>
        </Card>
        <SpotlightCard className="pcard" spotlightColor="rgba(255,255,255,.16)" theme="dark">
          <div className="lbl">Claude P7 · takedown_classify</div>
          <div className="big" style={{ marginTop: 4 }}>{t.is_takedown === null ? "Not classified" : t.is_takedown ? "Takedown" : "Not a takedown"}</div>
          <div className="g g2 mt">
            <div className="glass"><div className="v">{t.creator?.handle ?? "—"}</div><div className="k">Creator matched</div></div>
            <div className="glass"><div className="v">{platforms.length ? platforms.map((p) => p[0].toUpperCase() + p.slice(1)).join(" · ") : "—"}</div><div className="k">Platforms</div></div>
            <div className="glass"><div className="v">{t.matched_posts.length}</div><div className="k">Posts matched</div></div>
            <div className="glass"><div className="v">{humanize(t.status)}</div><div className="k">Status</div></div>
          </div>
          <div className="pfoot"><div><div className="k">Message id</div><div className="v mono" style={{ fontSize: 12 }}>{t.message_id ?? "—"}</div></div></div>
        </SpotlightCard>
      </div>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1.3fr 1fr" }}>
        <Card>
          <CardHead title="What the guardian did" hint="Automatically, without waiting for approval" />
          {acts.length ? <Steps steps={acts.map(actionStep)} /> : <div className="muted small" role="status">{t.status === "ignored" ? "Nothing — this email was not a takedown." : "No actions taken yet."}</div>}
        </Card>
        <div className="col">
          <Card>
            <CardHead title="Matched posts" />
            <DataTable cols={POST_COLS} rows={t.matched_posts} rowKey={(p) => p.id} href={(p) => `/posts/${p.id}`} dense caption="Matched posts" empty="No posts matched." />
          </Card>
          <Card>
            <CardHead title="Events" />
            <EventFeed events={t.events} />
          </Card>
        </div>
      </div>
      <div className="muted xs mt">Received <When iso={t.received_at} mode="ago" /></div>

      <Modal open={raw} onClose={() => setRaw(false)} title="Takedown record" width={680} icon={<Code size={22} />}>
        <div className="mt"><JsonBlock value={t} label="Takedown JSON" /></div>
      </Modal>
    </>
  );
}
