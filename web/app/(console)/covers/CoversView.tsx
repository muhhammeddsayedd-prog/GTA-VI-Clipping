"use client";

import { useState } from "react";

import { Badge, Banner, Card, CardHead, ErrorState, PageHead, Plat, SkeletonCards } from "@/components/kit/primitives";
import { POLL, useApi } from "@/lib/api/client";
import type { CoverGrid, CoverTile } from "@/lib/api/types";

const FILTERS: { key: CoverGrid["filter"]; label: string }[] = [
  { key: "all", label: "All" },
  { key: "fallbacks", label: "Fallbacks" },
  { key: "failed", label: "Failed" },
];

function tone(status: string): "green" | "amber" | "red" | "gray" {
  if (status === "applied" || status === "passed") return "green";
  if (status === "failed" || status === "apply_failed") return "red";
  if (status.startsWith("fallback") || status === "skipped_deadline") return "amber";
  return "gray";
}

function Tile({ c }: { c: CoverTile }) {
  const hover = [
    `${c.status} · ${c.arm}`,
    c.qa_total !== null ? `QA ${c.qa_total}/10` : null,
    c.line_text ? `“${c.line_text}”` : null,
    c.problems ? `rules: ${c.problems}` : null,
  ].filter(Boolean).join("\n");
  return (
    <figure className="col" style={{ gap: 6, margin: 0 }} title={hover}>
      <div style={{ aspectRatio: c.aspect === "9:16" ? "9 / 16" : "3 / 4", background: "var(--surface-2, #111)", borderRadius: 8, overflow: "hidden" }}>
        {c.preview_url ? (
          <img src={c.preview_url} alt={c.line_text ?? "cover without a line"} style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
        ) : (
          <div className="muted" style={{ padding: 12, fontSize: 12 }}>no image ({c.status})</div>
        )}
      </div>
      <figcaption className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        <Badge tone={tone(c.status)}>{c.status.replace("_", " ")}</Badge>
        {c.qa_total !== null && <span className="num muted">QA {c.qa_total}</span>}
      </figcaption>
    </figure>
  );
}

/** The cover engine's grid: each account's last 30 covers as the platform grid shows them (docs/29a C8). */
export function CoversView() {
  const [account, setAccount] = useState("youtube_main");
  const [filter, setFilter] = useState<CoverGrid["filter"]>("all");
  const q = useApi<CoverGrid>("admin/covers", { account, filter }, { interval: POLL.slow });
  if (q.error && !q.data) return <><PageHead title="Covers" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Covers" sub="Loading…" /><SkeletonCards n={3} /></>;
  const d = q.data;
  return (
    <>
      <PageHead title="Covers" sub="Each account's last 30 covers as its grid shows them · hover a cover for its arm and QA score" />
      <Banner tone="blue">
        TikTok and Instagram show the centre 3:4 of a cover in the profile grid; YouTube shows the whole 9:16 thumbnail. Fallbacks posted with the plain best frame or the platform&apos;s own frame.
      </Banner>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", marginTop: 16 }}>
        {d.accounts.map((a) => (
          <button key={a.account_id} type="button" className={a.account_id === account ? "btn p" : "btn"} onClick={() => setAccount(a.account_id)}>
            <Plat p={a.platform} /> {a.account_id}
          </button>
        ))}
        <span style={{ flex: 1 }} />
        {FILTERS.map((f) => (
          <button key={f.key} type="button" className={f.key === filter ? "btn p" : "btn"} onClick={() => setFilter(f.key)}>{f.label}</button>
        ))}
      </div>
      <Card style={{ marginTop: 16 }}>
        <CardHead title={account} hint={`${d.covers.length} covers`} />
        {d.covers.length === 0 ? (
          <p className="muted" style={{ padding: 16 }}>No covers yet for this account and filter.</p>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: 12, padding: 16 }}>
            {d.covers.map((c) => <Tile key={c.id} c={c} />)}
          </div>
        )}
      </Card>
    </>
  );
}
