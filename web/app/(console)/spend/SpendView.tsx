"use client";

import { useMemo } from "react";
import { Info } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import CountUp from "@/components/reactbits/CountUp";
import { Legend, Ring, Stacked } from "@/components/kit/charts";
import { DataTable, type Col } from "@/components/kit/data";
import { Banner, Card, CardHead, ErrorState, Grid, KV, Mono, PageHead, Prog, RefreshBtn, Seg, SkeletonCards } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { Usage } from "@/lib/api/types";
import { fmtCompact, fmtNum, fmtUsd } from "@/lib/format";
import { useListParams } from "@/lib/use-list";

const COLORS: Record<string, string> = { anthropic: "#5A4AF4", openai: "#9F94FF", groq: "#9F94FF", transcription: "#9F94FF", apify: "#D27A06" };
const NAMES: Record<string, string> = { anthropic: "Anthropic", openai: "Transcription", groq: "Transcription", transcription: "Transcription", local: "Transcription (local)", apify: "Apify", youtube: "YouTube / other", upload_post: "Upload-Post", twitch: "Twitch" };
const TRANSCRIBE = ["openai", "groq", "transcription"];
const color = (p: string) => COLORS[p] ?? "#E1E1EC";

type Op = Usage["operations"][number];

export function SpendView() {
  const lp = useListParams(["range"], { range: "today" });
  const range = lp.get("range");
  const q = useApi<Usage>("admin/usage", { range }, { interval: POLL.slow });
  const month = useApi<Usage>("admin/usage", { range: "month" }, { interval: POLL.slow });

  const chart = useMemo(() => {
    const src = month.data?.daily ?? [];
    const days = [...new Set(src.map((d) => d.day))].sort();
    const groupsOf: string[][] = [["anthropic"], TRANSCRIBE, ["apify"]];
    const known = groupsOf.flat();
    const series = [...groupsOf.map((ps) => days.map((d) => src.filter((x) => x.day === d && ps.includes(x.provider)).reduce((a, x) => a + x.usd, 0))),
      days.map((d) => src.filter((x) => x.day === d && !known.includes(x.provider)).reduce((a, x) => a + x.usd, 0))];
    return { days, series, labels: days.map((d, i) => (i % 5 === 0 || i === days.length - 1 ? String(Number(d.slice(8))) : "")) };
  }, [month.data]);

  if (q.error && !q.data) return <><PageHead title="Spend" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Spend" sub="Loading…" /><SkeletonCards n={4} h={110} /></>;
  const u = q.data;
  const today = range === "today";
  const tot = (p: string) => u.totals.filter((t) => t.provider === p).reduce((a, t) => a + t.usd, 0);
  const monthTotal = (month.data?.totals ?? []).reduce((a, t) => a + t.usd, 0);
  const transcribe = TRANSCRIBE.reduce((a, p) => a + tot(p), 0);
  const rangeLabel = range === "today" ? "today" : range === "7d" ? "7 days" : "this month";
  const budgetScale = range === "today" ? 1 : range === "7d" ? 7 : 30;

  const rings = [
    { name: `Anthropic · ${rangeLabel}`, v: tot("anthropic"), b: u.budgets.anthropic ? u.budgets.anthropic * budgetScale : null, hint: today ? "ai.daily_budget_usd" : "daily budget × days", c: "#5A4AF4", t: "#EFECFF" },
    { name: `Transcription · ${rangeLabel}`, v: transcribe, b: u.budgets.transcribe ? u.budgets.transcribe * budgetScale : null, hint: "transcribe.daily_budget_usd", c: "#5A4AF4", t: "#EFECFF" },
    { name: `Apify · ${rangeLabel}`, v: tot("apify"), b: today ? u.budgets.apify_daily : u.budgets.apify_monthly, hint: today ? `month ${fmtUsd(month.data ? month.data.totals.find((t) => t.provider === "apify")?.usd ?? 0 : null)} / ${fmtUsd(u.budgets.apify_monthly, 0)}` : "monthly budget", c: "#D27A06", t: "#FEF2DF" },
    { name: "Month to date", v: monthTotal, b: u.budgets.month_ceiling, hint: "of the monthly ceiling", c: "#12A150", t: "#E5F7EC" },
  ];

  const opCols: Col<Op>[] = [
    { key: "p", head: "Provider", render: (o) => NAMES[o.provider] ?? o.provider },
    { key: "o", head: "Operation", mobile: "primary", render: (o) => <Mono className="semi">{o.operation}</Mono> },
    { key: "m", head: "Model", render: (o) => o.model ?? <span className="muted">—</span> },
    { key: "c", head: "Calls", align: "right", render: (o) => <span className="num">{fmtNum(o.calls)}</span> },
    { key: "t", head: "Tokens in / out", align: "right", render: (o) => o.input_tokens !== null ? <span className="num">{fmtCompact(o.input_tokens)} / {fmtCompact(o.output_tokens)}</span> : o.units !== null ? <span className="num muted">{fmtCompact(o.units)} units</span> : "—" },
    { key: "u", head: `Cost · ${rangeLabel}`, align: "right", mobile: "aside", render: (o) => <b className="num">{fmtUsd(o.usd)}</b> },
  ];
  const qd = u.quotas;
  const quota = (label: string, used?: number, limit?: number, unit = "") => used !== undefined && limit ? (
    <div style={{ marginBottom: 14 }}>
      <div className="flabel">{label}</div>
      <Prog pct={(used / limit) * 100} tone={used / limit > 0.9 ? "red" : used / limit > 0.7 ? "amber" : undefined} label={label} />
      <div className="muted xs" style={{ marginTop: 6 }}>{fmtNum(used)} of {fmtNum(limit)}{unit} today</div>
    </div>
  ) : null;

  return (
    <>
      <PageHead title="Spend" sub="API spend from api_usage · over budget pauses the VOD lane until 00:00 UTC; express keeps running"
        actions={<><Seg label="Range" value={range} onChange={(v) => lp.set("range", v)} items={[{ value: "today", label: "Today" }, { value: "7d", label: "7 days" }, { value: "month", label: "Month" }]} />
          <RefreshBtn onClick={() => { q.refetch(); month.refetch(); }} spinning={q.isFetching} /></>} />

      <AnimatedContent distance={20} duration={0.5} stagger={0.06} trigger="mount">
        <Grid cols={4}>
          {rings.map((r) => {
            const pct = r.b ? (r.v / r.b) * 100 : 0;
            return (
              <Card key={r.name} as="div">
                <div className="row" style={{ gap: 16 }}>
                  <Ring pct={pct} size={92} fs={17} color={pct > 90 ? "#E5484D" : r.c} track={r.t} label={`${r.name}: ${fmtUsd(r.v)} of ${fmtUsd(r.b)}`} />
                  <div style={{ minWidth: 0 }}>
                    <div className="muted small">{r.name}</div>
                    <div className="num" style={{ fontSize: 24, fontWeight: 600, marginTop: 6 }}>$<CountUp to={r.v} decimals={2} duration={1} trigger="load" /></div>
                    <div className="muted xs">of {fmtUsd(r.b, r.b && r.b >= 10 ? 0 : 2)} · {r.hint}</div>
                  </div>
                </div>
              </Card>
            );
          })}
        </Grid>
      </AnimatedContent>

      <div className="g mt2 collapse-md" style={{ gridTemplateColumns: "1fr 360px" }}>
        <Card>
          <CardHead title="Daily spend by provider" hint="This month · tracked providers only"
            actions={<Legend items={[{ color: color("anthropic"), label: "Anthropic" }, { color: color("openai"), label: "Transcription" }, { color: color("apify"), label: "Apify" }, { color: "#E1E1EC", label: "YouTube / other" }]} />} />
          {month.data ? <Stacked label="Daily spend this month by provider" series={chart.series} colors={["#5A4AF4", "#9F94FF", "#D27A06", "#E1E1EC"]} labels={chart.labels} width={900} height={210} /> : <div className="skel" style={{ height: 210 }} />}
        </Card>
        <Card>
          <CardHead title="Quotas" hint="Not money, but they stop the pipeline when used up" />
          {quota("YouTube Data API units", qd.youtube_units_today, qd.youtube_units_limit)}
          {quota("Search API calls", qd.search_calls_today, qd.search_calls_limit)}
          {quota("Local transcription seconds", qd.transcribe_local_s_today, qd.transcribe_local_s_limit, " s")}
          <div className="flabel">Upload-Post plan</div>
          <Banner tone="amber" style={{ margin: 0, padding: "10px 12px", fontSize: 11.5 }} icon={<Info size={14} />}>Monthly allowance unknown — check the Upload-Post dashboard.</Banner>
        </Card>
      </div>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1fr 360px" }}>
        <Card>
          <CardHead title="By operation" hint="Anthropic operation = prompt id" />
          <DataTable dense caption="Spend by operation" cols={opCols} rows={[...u.operations].sort((a, b) => b.usd - a.usd)} rowKey={(o) => `${o.provider}:${o.operation}:${o.model}`} />
        </Card>
        <Card>
          <CardHead title="Not tracked here" hint="Fixed bills · check monthly by hand" />
          <KV rows={[["Hetzner servers", "≈ $70–165 / month"], ["Supabase (2 projects)", "≈ $35–60"], ["Upload-Post plan", "≈ $30–80"], ["Residential proxy", "≈ $30–120"]]} />
          <p className="muted xs" style={{ marginTop: 12 }}>Estimates from the cost model, not live numbers.</p>
        </Card>
      </div>
    </>
  );
}
