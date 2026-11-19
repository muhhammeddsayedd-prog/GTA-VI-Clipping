"use client";

import { DataTable, type Col } from "@/components/kit/data";
import { Badge, Banner, Card, CardHead, ErrorState, PageHead, Plat, SkeletonCards } from "@/components/kit/primitives";
import { styleName } from "@/components/domain/clips/ClipBits";
import { POLL, useApi } from "@/lib/api/client";
import type { StylePerformance, StyleRow } from "@/lib/api/types";
import { fmtNum } from "@/lib/format";

/** How each design style performs on each account (development-plan/M18 Phase D). */
export function StylesView() {
  const q = useApi<StylePerformance>("admin/styles", undefined, { interval: POLL.slow });
  if (q.error && !q.data) return <><PageHead title="Design styles" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Design styles" sub="Loading…" /><SkeletonCards n={3} /></>;
  const d = q.data;
  const cols: Col<StyleRow>[] = [
    { key: "s", head: "Style", render: (r) => <span className="semi">{styleName(r.style_id)}</span> },
    { key: "p7", head: "Posts, 7 days", align: "right", render: (r) => <span className="num">{fmtNum(r.posts_7d)}</span> },
    { key: "n", head: "Scored posts", align: "right", render: (r) => <span className="num">{fmtNum(r.scored)}</span> },
    { key: "v", head: "Avg views", align: "right", render: (r) => <span className="num">{r.avg_views === null ? "—" : fmtNum(r.avg_views)}</span> },
    {
      key: "x", head: "vs the account's usual", align: "right",
      render: (r) => r.views_ratio === null ? <span className="muted">—</span> : <span className={`num ${r.views_ratio >= 1 ? "up" : "down"}`}>×{r.views_ratio.toFixed(2)}</span>,
    },
    { key: "l", head: "State", render: (r) => r.learned ? <Badge tone="green">judged</Badge> : <Badge tone="gray">learning</Badge> },
  ];
  return (
    <>
      <PageHead title="Design styles" sub={`Picker: ${d.director.mode === "bandit" ? "learns from views" : "weighted (fixed)"} · ${Math.round(d.director.explore_floor * 100)}% of picks keep exploring · a style is judged after ${d.director.min_samples} scored posts`} />
      <Banner tone="blue">
        A post is scored once it is {d.director.min_age_h} hours old (and for {d.director.window_d} days): its views against the median of that account&apos;s posts. ×2 means twice the account&apos;s usual views. Dry-run and private posts have no views, so staging stays in “learning”.
      </Banner>
      <div className="col" style={{ gap: 16, marginTop: 16 }}>
        {d.accounts.map((a) => (
          <Card key={a.account_id}>
            <CardHead title={<span className="row" style={{ gap: 8 }}><Plat p={a.platform} />{a.account_id}</span>} hint="One row per style in factory.styles" />
            <DataTable caption={`Design styles on ${a.account_id}`} cols={cols} rows={a.styles} rowKey={(r) => r.style_id} dense />
          </Card>
        ))}
      </div>
    </>
  );
}
