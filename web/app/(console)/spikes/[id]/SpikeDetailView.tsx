"use client";

import Link from "next/link";
import { Code2 } from "lucide-react";
import { useState } from "react";

import SpotlightCard from "@/components/reactbits/SpotlightCard";
import { HBars } from "@/components/kit/charts";
import { EventFeed, JsonBlock, TraceBar } from "@/components/kit/data";
import { Drawer } from "@/components/kit/overlays";
import { Badge, Btn, Card, CardHead, ErrorState, KV, PageHead, Plat, SkeletonCards, TierChip, When } from "@/components/kit/primitives";
import { ModeBadge, PART_LABELS, spikeAt } from "@/components/domain/content/bits";
import { POLL, useApi } from "@/lib/api/client";
import type { SpikeDetail } from "@/lib/api/types";
import { fmtClock, fmtTime, series } from "@/lib/format";

const PART_ORDER = ["msgs_z", "uniq_z", "react_z", "clipreq_z", "audio_z"];

export function SpikeDetailView({ id }: { id: string }) {
  const q = useApi<SpikeDetail>(`admin/spikes/${id}`, undefined, { interval: POLL.detail });
  const [raw, setRaw] = useState(false);
  if (q.error && !q.data) return <><PageHead title="Spike" back="/spikes" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Spike" back="/spikes" sub="Loading…" /><SkeletonCards n={2} h={240} /></>;
  const s = q.data;
  const parts = s.parts ?? {};
  const [momentType, ...rest] = (s.claude_reason ?? "").split(" · ");
  const real = s.status !== "rejected" && s.tier !== "rejected";
  const winLen = s.window_start_s !== null && s.window_end_s !== null ? s.window_end_s - s.window_start_s
    : s.window_start_wall && s.window_end_wall ? (Date.parse(s.window_end_wall) - Date.parse(s.window_start_wall)) / 1000 : null;
  const winFrom = s.window_start_wall ? fmtTime(s.window_start_wall, true) : fmtClock(s.window_start_s);
  const winTo = s.window_end_wall ? fmtTime(s.window_end_wall, true) : fmtClock(s.window_end_s);
  const sample = s.chat_sample ?? [];
  // The peak line is the message closest to t_rel 0; older samples have no t_rel, so take the middle.
  const peakIdx = sample.some((m) => typeof m.t_rel === "number")
    ? sample.reduce((best, m, i) => (Math.abs(m.t_rel ?? Infinity) < Math.abs(sample[best].t_rel ?? Infinity) ? i : best), 0)
    : Math.floor(sample.length / 2);

  return (
    <>
      <PageHead back="/spikes"
        title={<span className="row" style={{ gap: 12 }}>Spike <TierChip tier={s.tier} /> · {s.creator.handle}</span>}
        sub={<span className="row" style={{ gap: 8, flexWrap: "wrap" }}><Plat p={s.creator.platform} /><ModeBadge mode={s.mode} /> detected <When iso={s.detected_at} /> UTC · <Badge status={s.status} />{s.spike_to_post_s !== null && <> · spike → post {s.spike_to_post_s} s</>}</span>}
        actions={<Btn icon={<Code2 size={15} />} onClick={() => setRaw(true)}>Raw JSON</Btn>} />
      <TraceBar trace={s.trace} on="spike" />

      <div className="g g2 collapse-md">
        <Card>
          <CardHead title={`Why this spike scored ${s.score_z ?? "—"}`} hint="z-score parts against a 10 min baseline · tier thresholds come from settings (spikes.z_s / z_a / z_b)" />
          {s.mode === "transcript" ? <div className="muted small">Picked by Claude from the transcript (no chat on this video), so there are no chat parts.</div> : (
            <HBars labelWidth={200} items={PART_ORDER.filter((k) => k in parts).map((k) => ({ label: <span>{PART_LABELS[k]}<div className="mono muted xs">{k}</div></span>, value: typeof parts[k] === "number" ? Math.round((parts[k] as number) * 100) / 100 : Number.NaN }))} />
          )}
          <div className="row" style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--line)" }}>
            <span className="semi">Weighted z-score</span><span className="right"><TierChip tier={s.tier} /></span>
            <span className="semi num" style={{ fontSize: 20 }}>{s.score_z ?? "—"}</span>
          </div>
        </Card>

        <SpotlightCard className="pcard" spotlightColor="rgba(255,255,255,.16)" theme="dark">
          <div className="lbl">Claude P1 · spike_confirm</div>
          <div className="big" style={{ marginTop: 6 }}>{real ? "Real moment" : s.status === "merged" ? "Merged into a stronger spike" : "Not a moment"}</div>
          {rest.length > 0 && <div style={{ marginTop: 8, color: "#E8E5FF", lineHeight: 1.55 }}>“{rest.join(" · ")}”</div>}
          <div className="g g3 mt">
            <div className="glass"><div className="v">{s.claude_confidence !== null ? s.claude_confidence.toFixed(2) : "—"}</div><div className="k">Confidence</div></div>
            <div className="glass"><div className="v" style={{ fontSize: 16 }}>{momentType || "—"}</div><div className="k">Moment type</div></div>
            <div className="glass"><div className="v">{s.tier ?? "—"}</div><div className="k">Final tier</div></div>
          </div>
          <div className="pfoot" style={{ flexWrap: "wrap" }}>
            <div><div className="k">Mode</div><div className="v">{s.mode}</div></div>
            {s.merged_into && <div><div className="k">Merged into</div><div className="v"><Link href={`/spikes/${s.merged_into}`} style={{ color: "#fff", textDecoration: "underline" }}>open spike</Link></div></div>}
            {s.clip_id && <div className="right"><Link href={`/clips/${s.clip_id}`} className="btn w sm">Open clip {series(s.clip_series_no)}</Link></div>}
          </div>
        </SpotlightCard>
      </div>

      <div className="g g2 mt collapse-md">
        <Card>
          <CardHead title="Clip window" hint="start = spike − lead · end when z drops below the end threshold, plus a short tail · 15–60 s" />
          <svg viewBox="0 0 640 110" width="100%" height={110} role="img" aria-label={`Clip window from ${winFrom} to ${winTo}`}>
            <rect x={0} y={54} width={640} height={10} rx={5} fill="#F0F0F5" />
            <rect x={150} y={48} width={300} height={22} rx={8} fill="#DDD7FF" />
            <rect x={150} y={48} width={100} height={22} rx={8} fill="#C9C2FF" />
            <line x1={250} x2={250} y1={20} y2={96} stroke="#5A4AF4" strokeWidth={2} /><circle cx={250} cy={20} r={6} fill="#5A4AF4" />
            <text x={150} y={98} fontSize={11} fill="#6b6b82">{winFrom} start</text>
            <text x={236} y={12} fontSize={11} fill="#5A4AF4" fontWeight={700}>peak {spikeAt(s)}</text>
            <text x={400} y={98} fontSize={11} fill="#6b6b82">{winTo} end</text>
            <text x={166} y={63} fontSize={10} fill="#4134C9" fontWeight={700}>lead</text>
          </svg>
          <KV rows={[["Length", winLen !== null ? `${Math.round(winLen)} s` : "—"], ["Position", spikeAt(s)], ["Mode", s.mode === "live" ? "live · express path for tier S" : s.mode]]} />
        </Card>

        <section className="dpanel" aria-labelledby="chat-h">
          <div className="row"><h3 id="chat-h">Chat around the peak</h3><span className="right dbadge">{sample.length} kept for audit</span></div>
          {sample.length ? (
            <ul style={{ listStyle: "none", margin: "14px 0 0", padding: 0, display: "flex", flexDirection: "column", gap: 2 }}>
              {sample.slice(Math.max(0, peakIdx - 6), peakIdx + 6).map((m, i, arr) => {
                const on = i === Math.min(6, peakIdx) && arr.length > 6;
                const ts = m.ts ?? m.t;
                // The detector stores `t_rel`: seconds relative to the spike peak.
                const rel = typeof m.t_rel === "number" ? `${m.t_rel > 0 ? "+" : ""}${Math.round(m.t_rel)} s` : null;
                return (
                  <li key={i} className={`chatline${on ? " on" : ""}`}>
                    <span className="ts">{rel ?? (ts ? fmtTime(ts, true) : "")}</span>
                    <span className="u">{m.user ?? m.author ?? "?"}</span>
                    <span>{m.text ?? m.message ?? ""}</span>
                  </li>
                );
              })}
            </ul>
          ) : <div style={{ color: "var(--dmute)", marginTop: 14 }}>No chat sample (transcript spikes have none).</div>}
          <div className="row" style={{ marginTop: 14, gap: 8, flexWrap: "wrap" }}>
            <span className="dbadge">{s.msgs_in_bucket ?? "—"} msgs in bucket</span>
            <span className="dbadge">{s.unique_chatters ?? "—"} unique</span>
            <span className="dbadge">{s.reaction_count ?? "—"} reactions</span>
            <span className="dbadge">{s.clip_requests ?? "—"} clip requests</span>
          </div>
        </section>
      </div>

      <Card style={{ marginTop: 16 }}>
        <CardHead title="Spike events" />
        <EventFeed events={s.events.slice(0, 10)} empty="No events for this spike." />
      </Card>

      <Drawer open={raw} onClose={() => setRaw(false)} title="Spike JSON" sub={s.id}>
        <JsonBlock value={s} maxHeight={720} label="Spike as JSON" />
      </Drawer>
    </>
  );
}
