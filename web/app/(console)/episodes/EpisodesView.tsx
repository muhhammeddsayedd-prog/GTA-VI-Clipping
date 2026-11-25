"use client";

import { DataTable, type Col } from "@/components/kit/data";
import { Badge, Banner, Card, ErrorState, PageHead, SkeletonCards } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { EpisodeRow } from "@/lib/api/types";
import { fmtClock } from "@/lib/format";

const COLS: Col<EpisodeRow>[] = [
  { key: "d", head: "Day", render: (e) => <span className="num semi">{e.episode_date}</span> },
  { key: "t", head: "Title", render: (e) => e.title ?? <span className="muted">Not assembled yet</span> },
  { key: "s", head: "Status", render: (e) => <Badge status={e.status} /> },
  {
    key: "m", head: "Moments", align: "right",
    render: (e) => <span className="num">{e.rendered}/{e.items}{e.dropped ? <span className="muted"> ({e.dropped} dropped)</span> : null}</span>,
  },
  { key: "l", head: "Length", align: "right", render: (e) => <span className="num">{e.duration_s ? fmtClock(e.duration_s) : "—"}</span> },
  { key: "e", head: "Note", render: (e) => (e.error ? <span className="small down">{e.error}</span> : <span className="muted">—</span>) },
];

/** The daily 16:9 long-form episodes (development-plan/M20). */
export function EpisodesView() {
  const q = useApi<{ items: EpisodeRow[] }>("admin/episodes", undefined, { interval: POLL.list });
  if (q.error && !q.data) return <><PageHead title="Episodes" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Episodes" sub="Loading…" /><SkeletonCards n={2} /></>;
  return (
    <>
      <PageHead title="Episodes" sub="One 16:9 Top-N video a day, built from the moments the engine already picked" />
      <Banner tone="blue">
        An episode is planned at the build hour; its moments are downloaded again, rendered in 16:9 and joined. Open one to watch it.
      </Banner>
      <Card style={{ marginTop: 16 }}>
        <DataTable caption="Episodes" cols={COLS} rows={q.data.items} rowKey={(e) => e.id} href={(e) => `/episodes/${e.id}`} empty="No episode planned yet." />
      </Card>
    </>
  );
}
