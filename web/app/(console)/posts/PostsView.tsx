"use client";

import { Clock, Send, Trash2, X } from "lucide-react";

import { DataTable, ExtLink, Kpi, Pager, Thumb, type Col } from "@/components/kit/data";
import { Badge, Card, ErrorState, Grid, PageHead, Plat, RefreshBtn, Seg, SelectField, SkeletonTable, When } from "@/components/kit/primitives";
import { laneTone } from "@/components/domain/clips/ClipBits";
import { useApi } from "@/lib/api/client";
import type { Page, Post } from "@/lib/api/types";
import { fmtNum, humanize, series } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

const KEYS = ["status", "account_id", "lane", "clip_id"];
const DEFAULTS = {};
const STATUSES = ["published", "pending", "failed", "removed", "remove_failed", "skipped"];

const COLS: Col<Post>[] = [
  { key: "clip", head: "Clip", render: (p) => <div className="row"><div style={{ width: 30, flex: "none" }}><Thumb seed={p.clip_id} /></div><div style={{ minWidth: 0 }}><div className="semi pc">{series(p.clip_series_no)}</div><div className="sub ellipsis" style={{ maxWidth: 200 }}>{p.clip_hook ?? ""}</div></div></div> },
  { key: "acc", head: "Account", render: (p) => <span className="row" style={{ gap: 8 }}><Plat p={p.platform} /><span className="semi">{p.account_id}</span></span> },
  { key: "lane", head: "Lane", mobile: "hide", render: (p) => <Badge tone={laneTone(p.lane)}>{p.lane}</Badge> },
  { key: "st", head: "Status", mobile: "aside", render: (p) => <Badge status={p.status} /> },
  { key: "pub", head: "Published", mobile: "meta", render: (p) => <When iso={p.published_at} /> },
  {
    key: "link", head: "Link", mobile: "meta", render: (p) => p.post_url && /^https?:\/\//.test(p.post_url) ? <ExtLink href={p.post_url}>open</ExtLink>
      : p.error ? <span className="down xs">{humanize(p.error)}</span>
      : <span className="muted">{p.dry_run ? "dry run" : p.status === "pending" ? (p.platform === "tiktok" ? "no URL yet (TikTok)" : "no URL yet") : p.post_url ?? "—"}</span>,
  },
  { key: "v", head: "Views", align: "right", mobile: "meta", render: (p) => <span className="num">{fmtNum(p.views)}</span> },
  { key: "l", head: "Likes", align: "right", mobile: "hide", render: (p) => <span className="num">{fmtNum(p.likes)}</span> },
  { key: "c", head: "Claim", mobile: "hide", render: (p) => (p.claim_status ? <Badge status={p.claim_status} /> : <span className="muted">—</span>) },
];

export function PostsView() {
  const lp = useListParams(KEYS, DEFAULTS);
  const q = useApi<Page<Post>>("admin/posts", lp.params);
  const counts = q.data?.counts ?? {};
  const status = lp.get("status");

  return (
    <>
      <PageHead title="Posts" sub="One row per clip per account · stats refreshed every 2 h for 7 days" actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />
      <Grid cols={4} className="mb">
        <Kpi label="Published" value={counts.published ?? null} icon={<Send size={15} />} iconTone="green" delta={<span>in the current filter</span>} />
        <Kpi label="Pending" value={counts.pending ?? 0} icon={<Clock size={15} />} delta={<span>uploading or waiting for a URL</span>} />
        <Kpi label="Failed" value={counts.failed ?? 0} icon={<X size={15} />} iconTone="red" delta={<span>after 3 attempts · 30 s / 2 min / 5 min</span>} />
        <Kpi label="Needs manual delete" value={counts.remove_failed ?? 0} icon={<Trash2 size={15} />} iconTone="red"
          delta={<span className={counts.remove_failed ? "down" : undefined}>TikTok and Instagram have no delete API</span>} />
      </Grid>
      <div className="filters" style={{ flexWrap: "wrap" }}>
        <SelectField label="Account" value={lp.get("account_id")} onChange={(v) => lp.set("account_id", v)}
          options={[{ value: "", label: "All accounts" }, ...["tiktok_main", "tiktok_live", "instagram_main", "youtube_main"].map((a) => ({ value: a, label: a }))]} />
        <SelectField label="Lane" value={lp.get("lane")} onChange={(v) => lp.set("lane", v)} width={140}
          options={[{ value: "", label: "All lanes" }, { value: "hourly", label: "Hourly" }, { value: "spike", label: "Spike" }]} />
        {lp.get("clip_id") && <button type="button" className="chip p" onClick={() => lp.set("clip_id", "")} aria-label="Remove clip filter">One clip only ✕</button>}
        <span className="right">
          <Seg label="Post status" value={status} onChange={(v) => lp.set("status", v)}
            items={[{ value: "", label: "All", count: counts.all ?? null }, ...STATUSES.map((s) => ({ value: s, label: humanize(s).replace(/^\w/, (x) => x.toUpperCase()), count: counts[s] ?? 0 }))]} />
        </span>
      </div>
      {q.error && !q.data ? <ErrorState error={q.error} retry={() => q.refetch()} /> : !q.data ? <SkeletonTable /> : (
        <Card>
          <DataTable caption="Posts" cols={COLS} rows={q.data.items} rowKey={(p) => p.id} href={(p) => `/posts/${p.id}`} empty="No posts match these filters." />
          <Pager shown={q.data.items.length} total={status ? counts[status] : counts.all} cursors={lp.cursors} next={q.data.next_cursor} onPage={lp.setCursors} noun="posts" />
        </Card>
      )}
    </>
  );
}
