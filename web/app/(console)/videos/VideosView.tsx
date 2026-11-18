"use client";

import { DataTable, Pager, type Col } from "@/components/kit/data";
import { BlockBanners, readBlocks } from "@/components/domain/sources/BlockBanner";
import { Badge, Card, ErrorState, PageHead, RefreshBtn, Seg, SelectField, SkeletonTable, Two, When, Who } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Page, Video } from "@/lib/api/types";
import { fmtDuration, fmtNum, humanize } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

type VideoPage = Page<Video> & { meta?: { source_health?: unknown } };

const STATUSES = ["new", "processing", "done", "failed", "skipped"];
const KEYS = ["status", "platform"];

const cols: Col<Video>[] = [
  { key: "c", head: "Creator", render: (v) => <Who name={v.creator.handle} p={v.platform} size={30} sub={`${fmtDuration(v.duration_s)}${v.has_chat ? "" : " · no chat"}`} /> },
  { key: "v", head: "Video", mobile: "primary", render: (v) => <Two a={<span className="clamp2">{v.title ?? v.source_id}</span>} b={<>published <When iso={v.published_at} /></>} /> },
  { key: "s", head: "Status", mobile: "aside", render: (v) => <Badge status={v.status} /> },
  { key: "a", head: "Attempts", align: "right", render: (v) => <span className={v.attempts >= 3 ? "down semi" : undefined}>{v.attempts}</span> },
  { key: "ch", head: "Chat", mobile: "hide", render: (v) => (v.has_chat === null ? "—" : <Badge tone={v.has_chat ? "green" : "gray"}>{v.has_chat ? "yes" : "no"}</Badge>) },
  { key: "t", head: "Trend", align: "right", render: (v) => <b className="num">{v.trend_score ?? "—"}</b> },
  {
    key: "r", head: "Result", render: (v) =>
      v.skip_reason ? <span className="muted">{humanize(v.skip_reason)}</span>
        : v.status === "failed" ? <span className="down">failed after {v.attempts} attempts</span>
        : v.clips ? <span>{v.clips} clip{v.clips > 1 ? "s" : ""}</span> : <span className="muted">—</span>,
  },
];

export function VideosView() {
  const lp = useListParams(KEYS);
  const q = useApi<VideoPage>("admin/videos", lp.params, { interval: POLL.list });
  const counts = q.data?.counts ?? {};
  const blocked = readBlocks(q.data?.meta?.source_health);

  return (
    <>
      <PageHead title="VOD queue" sub="Finished streams and uploads · chat-first, transcript fallback when chat is thin"
        actions={<RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} />} />
      <BlockBanners blocks={blocked} what="downloads" />
      <div className="filters">
        <span className="lab">Filters <span className="cnt">{KEYS.filter((k) => lp.get(k)).length}</span></span>
        <SelectField label="Platform" value={lp.get("platform")} onChange={(v) => lp.set("platform", v)} width={170}
          options={[{ value: "", label: "All platforms" }, { value: "twitch", label: "Twitch" }, { value: "youtube", label: "YouTube" }]} />
        <span className="right">
          <Seg label="Video status" value={lp.get("status")} onChange={(v) => lp.set("status", v)}
            items={[{ value: "", label: "All", count: counts.all ?? null }, ...STATUSES.map((s) => ({ value: s, label: humanize(s).replace(/^./, (c) => c.toUpperCase()), count: counts[s] ?? 0 }))]} />
        </span>
      </div>
      {q.error && !q.data ? <ErrorState error={q.error} retry={() => q.refetch()} /> : !q.data ? <SkeletonTable /> : (
        <Card>
          <DataTable cols={cols} rows={q.data.items} rowKey={(v) => v.id} href={(v) => `/videos/${v.id}`} caption="Videos in the VOD lane"
            empty={lp.get("status") ? `No ${humanize(lp.get("status"))} videos.` : "No videos yet. Discovery queues finished streams here."} />
          <Pager shown={q.data.items.length} total={lp.get("status") ? counts[lp.get("status")] : counts.all} cursors={lp.cursors} next={q.data.next_cursor} onPage={lp.setCursors} noun="videos" />
        </Card>
      )}
      <div className="muted xs" style={{ marginTop: 10 }}>{q.data ? `${fmtNum(counts.all)} videos in the last 48 h window` : ""}</div>
    </>
  );
}
