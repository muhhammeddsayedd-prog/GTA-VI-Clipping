"use client";

import Link from "next/link";
import { useState } from "react";
import { ArrowUpRight, Eye, Film, List, Shield, Users } from "lucide-react";

import SpotlightCard from "@/components/reactbits/SpotlightCard";
import { EventFeed, Kpi } from "@/components/kit/data";
import { Avatar, Badge, Btn, Card, CardHead, Chip, ErrorState, Grid, KV, PageHead, Plat, SkeletonCards, platformName } from "@/components/kit/primitives";
import { MiniClip } from "@/components/domain/sources/MiniClip";
import { POLL, useApi } from "@/lib/api/client";
import type { CreatorDetail } from "@/lib/api/types";
import { fmtCompact, fmtDay, fmtNum } from "@/lib/format";

const TIER_COPY: Record<string, { title: string; express: string; cap: string }> = {
  preferred: { title: "Preferred", express: "yes", cap: "none" },
  auto: { title: "Auto", express: "only with no music", cap: "a few posts per day" },
  restricted: { title: "Restricted", express: "no", cap: "never clipped" },
  blocked: { title: "Blocked", express: "no", cap: "never clipped" },
};

export function CreatorDetailView({ id }: { id: string }) {
  const q = useApi<CreatorDetail>(`admin/creators/${encodeURIComponent(id)}`, undefined, { interval: POLL.slow });
  const [tab, setTab] = useState<"clips" | "events">("clips");
  if (q.error && !q.data) return <><PageHead title="Creator" back="/creators" /><ErrorState error={q.error} retry={() => q.refetch()} /></>;
  if (!q.data) return <><PageHead title="Creator" back="/creators" sub="Loading…" /><SkeletonCards n={4} /></>;
  const c = q.data;
  const name = c.handle ?? c.display_name ?? c.channel_id;
  const tc = TIER_COPY[c.tier] ?? TIER_COPY.auto;
  const channel = c.platform === "twitch" ? `https://twitch.tv/${c.handle}` : `https://youtube.com/channel/${c.channel_id}`;
  const recheck = c.description_checked_at ? new Date(Date.parse(c.description_checked_at) + 14 * 86400e3).toISOString() : null;
  const blocked = c.tier === "blocked" || c.tier === "restricted";

  return (
    <>
      <PageHead back="/creators"
        title={<span className="row" style={{ gap: 14 }}><Avatar name={name} size={52} />{name}</span>}
        sub={<span className="row" style={{ gap: 6, flexWrap: "wrap" }}><Plat p={c.platform} /> {platformName(c.platform)} · {c.language?.toUpperCase() ?? "language unknown"} · discovered {fmtDay(c.discovered_at)} · <Badge status={c.tier} /></span>}
        actions={<><Btn href={channel} external icon={<ArrowUpRight size={15} />}>Open channel</Btn><Btn kind="p" href={`/events?entity_type=creator&entity_id=${c.id}`} icon={<List size={15} />}>Creator events</Btn></>}
      />

      <Grid cols={4}>
        <Kpi label="Followers" value={c.follower_count} icon={<Users size={15} />} />
        <Kpi label="Clips posted" value={c.clips_posted} icon={<Film size={15} />} delta={<span>{fmtNum(c.counts.clips)} rendered in total</span>} />
        <Kpi label="Avg clip views" value={c.avg_clip_views} icon={<Eye size={15} />} delta={<span>rolling average of our posts</span>} />
        <Kpi label="Claims" value={c.claims_count} icon={<Shield size={15} />} iconTone={c.claims_count ? "red" : "green"} delta={<span>{c.claims_count ? "counted toward strikes" : "clean record"}</span>} />
      </Grid>

      <div className="g mt collapse-md" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
        <SpotlightCard className="pcard" spotlightColor="rgba(255,255,255,.16)" theme="dark" style={blocked ? { background: "linear-gradient(140deg,#3A3A4F,#1B1B27)" } : undefined}>
          <div className="lbl">Permission tier</div>
          <div className="big" style={{ marginTop: 4 }}>{tc.title}</div>
          <div style={{ marginTop: 12, background: "rgba(255,255,255,.12)", borderRadius: 14, padding: 14, lineHeight: 1.55, color: "#EEEBFF" }}>
            {c.tier_reason ?? "No statement found in the channel description."}
            <div style={{ fontSize: 11, color: "#CFC9FF", marginTop: 6 }}>
              {c.description_checked_at ? `Read by Claude P6 (permission_read) · ${fmtDay(c.description_checked_at)}` : "Not checked yet"}
            </div>
          </div>
          <div className="pfoot" style={{ flexWrap: "wrap", gap: 24 }}>
            <div><div className="k">Next re-check</div><div className="v">{recheck ? fmtDay(recheck) : "—"}</div></div>
            <div><div className="k">Express allowed</div><div className="v">{tc.express}</div></div>
            <div><div className="k">Daily cap</div><div className="v">{tc.cap}</div></div>
          </div>
        </SpotlightCard>
        <Card>
          <CardHead title="Facecam layout" hint={c.layout ? `Cached · confidence ${c.layout.confidence ?? "—"} · expires ${fmtDay(c.layout.expires_at)}` : "No layout detected yet"} />
          <LayoutBox d={c} />
        </Card>
        <Card>
          <CardHead title="Activity" />
          <KV rows={[
            ["Live sessions", fmtNum(c.counts.live_sessions)],
            ["Videos processed", fmtNum(c.counts.videos)],
            ["Spikes", fmtNum(c.counts.spikes)],
            ["Clips", fmtNum(c.counts.clips)],
            ["Posts", fmtNum(c.counts.posts)],
            ["Takedowns", c.counts.takedowns ? <span key="t" className="down">{c.counts.takedowns}</span> : "none"],
          ]} />
        </Card>
      </div>

      <Card style={{ marginTop: 16 }}>
        <div className="tabs" role="tablist" aria-label="Creator records">
          <button type="button" role="tab" aria-selected={tab === "clips"} className={tab === "clips" ? "on" : ""} onClick={() => setTab("clips")}
            style={{ border: 0, background: "none", padding: "0 0 12px", font: "inherit", cursor: "pointer" }}>Clips <i>{c.counts.clips}</i></button>
          <button type="button" role="tab" aria-selected={tab === "events"} className={tab === "events" ? "on" : ""} onClick={() => setTab("events")}
            style={{ border: 0, background: "none", padding: "0 0 12px", font: "inherit", cursor: "pointer" }}>Events <i>{c.events.length}</i></button>
          <Link href={`/clips?creator_id=${c.id}`}>All clips <i>{c.counts.clips}</i></Link>
          <Link href={`/events?entity_type=creator&entity_id=${c.id}`}>Audit log</Link>
        </div>
        <div role="tabpanel">
          {tab === "clips" ? (
            c.clips.length ? <div className="g g6">{c.clips.map((cl) => <MiniClip key={cl.id} c={cl} />)}</div>
              : <div className="muted small" role="status">No clips from this creator yet.</div>
          ) : <EventFeed events={c.events} />}
        </div>
      </Card>
    </>
  );
}

function LayoutBox({ d }: { d: CreatorDetail }) {
  const l = d.layout;
  if (!l || !l.source_width || !l.source_height) return <div className="muted small" role="status">The facecam is detected on the first render (P4 layout_detect) and cached for 7 days.</div>;
  const W = l.source_width, H = l.source_height;
  const box = (b: { x: number; y: number; w: number; h: number }) => ({ left: `${(b.x / W) * 100}%`, top: `${(b.y / H) * 100}%`, width: `${(b.w / W) * 100}%`, height: `${(b.h / H) * 100}%` });
  return (
    <>
      <div role="img" aria-label={`Source ${W}×${H}. ${l.facecam_box ? "Facecam box drawn in yellow." : "No facecam."} Gameplay box dashed.`}
        style={{ position: "relative", aspectRatio: `${W}/${H}`, borderRadius: 14, overflow: "hidden", background: "linear-gradient(160deg,#2B5876,#4E4376)" }}>
        {l.gameplay_box && (
          <div style={{ position: "absolute", ...box(l.gameplay_box), border: "2px dashed #fff", borderRadius: 8 }}>
            <span style={{ position: "absolute", left: 6, top: 4, fontSize: 10, color: "#fff", fontWeight: 700 }}>gameplay_box</span>
          </div>
        )}
        {l.facecam_box && (
          <div style={{ position: "absolute", ...box(l.facecam_box), border: "2px solid #FFD400", borderRadius: 8, background: "rgba(255,212,0,.15)" }}>
            <span style={{ position: "absolute", left: 6, top: 4, fontSize: 10, color: "#FFD400", fontWeight: 700 }}>facecam_box</span>
          </div>
        )}
      </div>
      <div className="row mt" style={{ gap: 6, flexWrap: "wrap" }}>
        <Chip>{W}×{H} source</Chip>
        <Chip accent>{l.facecam_box ? "layout B · facecam" : "layout A · no facecam"}</Chip>
        {l.expires_at && <Chip>expires {fmtDay(l.expires_at)}</Chip>}
        <Chip>avg views {fmtCompact(d.avg_clip_views)}</Chip>
      </div>
    </>
  );
}
