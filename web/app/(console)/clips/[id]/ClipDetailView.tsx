"use client";

import { useState } from "react";
import { Code, ExternalLink, X } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { HBars, SegBar } from "@/components/kit/charts";
import { DataTable, EventFeed, ExtLink, JsonBlock, TraceBar, type Col } from "@/components/kit/data";
import { Drawer } from "@/components/kit/overlays";
import { Badge, Banner, Btn, Card, CardHead, Chip, ErrorState, KV, PageHead, Plat, Seg, SkeletonCards, When } from "@/components/kit/primitives";
import { ClipPlayer, GateChecklist, firstFailing, laneTone } from "@/components/domain/clips/ClipBits";
import { POLL, useApi } from "@/lib/api/client";
import type { ClipDetail, Post } from "@/lib/api/types";
import { fmtNum, humanize, series } from "@/lib/format";

const POST_COLS: Col<Post>[] = [
  { key: "acc", head: "Account", render: (p) => <span className="row" style={{ gap: 8 }}><Plat p={p.platform} />{p.account_id}</span> },
  { key: "st", head: "Status", render: (p) => <Badge status={p.status} /> },
  { key: "pub", head: "Published", render: (p) => <When iso={p.published_at} seconds /> },
  { key: "link", head: "Link", render: (p) => (p.post_url && /^https?:\/\//.test(p.post_url) ? <ExtLink href={p.post_url}>open</ExtLink> : <span className="muted">{p.error ? humanize(p.error) : p.dry_run ? "dry run" : "no URL yet"}</span>) },
  { key: "v", head: "Views", align: "right", render: (p) => <span className="num">{fmtNum(p.views)}</span> },
  { key: "l", head: "Likes", align: "right", render: (p) => <span className="num">{fmtNum(p.likes)}</span> },
  { key: "c", head: "Claim", render: (p) => (p.claim_status ? <Badge status={p.claim_status} /> : <span className="muted">—</span>) },
];

export function ClipDetailView({ id }: { id: string }) {
  const q = useApi<ClipDetail>(`admin/clips/${id}`, undefined, { interval: POLL.detail });
  const [raw, setRaw] = useState(false);
  if (q.error && !q.data) return <><PageHead title="Clip" back="/clips" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Clip" back="/clips" sub="Loading…" /><SkeletonCards n={3} h={420} /></>;
  const c = q.data;
  const fail = firstFailing(c.gate_result);
  const failCheck = fail ? c.gate_result?.checks?.[fail] : undefined;
  const live = c.posts.find((p) => p.post_url && /^https?:\/\//.test(p.post_url) && p.status === "published");
  const renderS = c.render_ms ? Math.round(c.render_ms / 1000) : null;

  return (
    <>
      <PageHead back="/clips"
        title={<>Clip <span className="pc">{series(c.series_no)}</span></>}
        sub={<span className="row" style={{ gap: 8, flexWrap: "wrap" }}>{c.hook ?? c.title} <Badge status={c.status} /> <Badge tone={laneTone(c.lane)}>{c.lane}</Badge> {c.gate_profile && <span>· {c.gate_profile} profile</span>}</span>}
        actions={<>
          <Btn icon={<Code size={15} />} onClick={() => setRaw(true)}>Raw JSON</Btn>
          {live && <Btn kind="p" href={live.post_url!} external icon={<ExternalLink size={15} />}>Open on {live.platform === "youtube" ? "YouTube" : live.platform === "tiktok" ? "TikTok" : "Instagram"}</Btn>}
        </>}
      />
      <TraceBar trace={c.trace} on="clip" />

      {c.status === "rejected" && (
        <Banner tone="red" icon={<X size={17} strokeWidth={2.4} />}>
          <b>Rejected: {humanize(c.reject_reason ?? fail ?? "gate")}.</b>{" "}
          {failCheck ? <>First failing check <b>{humanize(fail)}</b>: {String(failCheck.value ?? "—")} against {String(failCheck.limit ?? "—")}.</> : null}
          {c.gate_result?.codes && c.gate_result.codes.length > 1 && <> Also failed: {c.gate_result.codes.filter((x) => x !== c.reject_reason).map(humanize).join(", ")}.</>} Never posted.
        </Banner>
      )}

      <AnimatedContent distance={20} duration={0.5} stagger={0.08} trigger="mount">
        <div className="g collapse-md" style={{ gridTemplateColumns: "330px 1fr 1fr", alignItems: "start" }}>
          <ClipPlayer clip={c} />
          <div className="col">
            <Card>
              <CardHead title="Copy written by Claude (P2)" hint="clip_writer · hook, title, caption, hashtags" />
              <KV rows={[
                ["Hook", c.hook ?? "—"],
                ["YouTube title", c.title ?? "—"],
                ["Caption", c.caption ?? "—"],
                ["Topic slug", <span key="t" className="mono">{c.topic ?? "—"}</span>],
              ]} />
              {!!c.hashtags?.length && <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 12 }}>{c.hashtags.map((h) => <Chip key={h} accent>#{h.replace(/^#+/, "")}</Chip>)}</div>}
              {c.score_parts && (
                <>
                  <div className="flabel" style={{ marginTop: 16 }}>Score parts · {c.score ?? "—"}</div>
                  <HBars labelWidth={110} items={Object.entries(c.score_parts).map(([k, v]) => ({ label: humanize(k).replace(/^\w/, (x) => x.toUpperCase()), value: v }))} />
                </>
              )}
            </Card>
            <Card>
              <CardHead title="Render" hint={`${c.lane === "spike" ? "express.render" : "clip.render"}${renderS ? ` · ${renderS} s total` : ""}`} />
              {renderS ? (
                <SegBar height={26} parts={splitRender(renderS)} />
              ) : <div className="muted small">Render timing not recorded yet.</div>}
              <div className="mt"><KV rows={[
                ["Bleeps", c.profanity_count ? `${c.profanity_count} profanity` : "none"],
                ["Music ratio", c.music_ratio !== null ? c.music_ratio.toFixed(2) : "—"],
                ["Spoiler", c.spoiler ? <span key="s" className="down">yes</span> : "no"],
                ["Source", `${c.source_type === "live" ? "Live" : "VOD"} · ${c.start_s !== null ? `${Math.round(c.start_s)}–${Math.round(c.end_s ?? 0)} s` : "—"}`],
                ["Language", c.original_language ?? "—"],
              ]} /></div>
            </Card>
          </div>
          <GateChecklist gate={c.gate_result} status={c.status} />
        </div>
      </AnimatedContent>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1fr 1fr" }}>
        <Card>
          <CardHead title="Posts" hint="One row per account" />
          <DataTable caption="Posts of this clip" cols={POST_COLS} rows={c.posts} rowKey={(p) => p.id} href={(p) => `/posts/${p.id}`} dense empty="Not posted anywhere." />
        </Card>
        <Transcript c={c} />
      </div>
      <Card style={{ marginTop: 16 }}>
        <CardHead title="Clip events" hint="Last 50 events for this clip" />
        <EventFeed events={c.events} />
      </Card>

      <Drawer open={raw} onClose={() => setRaw(false)} title="Raw clip JSON" sub={c.id} width={620}>
        <JsonBlock value={c} maxHeight={760} label="Clip JSON" />
      </Drawer>
    </>
  );
}

/** The API stores only the total render time; show it as one segment plus the gate. */
function splitRender(total: number) {
  return [{ label: "Render", value: Math.max(total - 4, 1), color: "#3A2CC7" }, { label: "Gate", value: Math.min(4, total), color: "#12A150" }];
}

function Transcript({ c }: { c: ClipDetail }) {
  const [mode, setMode] = useState("words");
  const words = c.transcript?.words ?? [];
  const kw = new Set((c.keywords ?? []).flatMap((k) => k.words.map((w) => w.toLowerCase())));
  return (
    <Card>
      <CardHead title="Transcript" hint={`${c.original_language === "en" || !c.original_language ? "English" : `Translated from ${c.original_language}`} · keywords highlighted`}
        actions={<Seg label="Transcript view" value={mode} onChange={setMode} items={[{ value: "words", label: "Timed" }, { value: "text", label: "Plain" }]} />} />
      {!words.length && !c.transcript?.text ? <div className="muted small">No transcript stored.</div> : mode === "text" ? (
        <p style={{ lineHeight: 1.8, margin: 0 }}>{c.transcript?.text ?? words.map((w) => w.w ?? w.word).join(" ")}</p>
      ) : (
        <p style={{ lineHeight: 2, fontSize: 13.5, margin: 0 }}>
          {words.map((w, i) => {
            const text = w.w ?? w.word ?? "";
            const start = w.s ?? w.start ?? 0;
            const hot = kw.has(text.toLowerCase().replace(/[^a-z0-9']/g, ""));
            return (
              <span key={i}>
                {i % 8 === 0 && <span className="muted mono xs" style={{ marginRight: 4 }}>{fmtStamp(start)}</span>}
                {hot ? <b style={{ background: "#FFF4C2", padding: "1px 4px", borderRadius: 5 }}>{text}</b> : text}{" "}
              </span>
            );
          })}
        </p>
      )}
    </Card>
  );
}
function fmtStamp(s: number) { const t = Math.floor(s); return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`; }
