"use client";

import Link from "next/link";

import { Thumb } from "@/components/kit/data";
import { Badge, Plat } from "@/components/kit/primitives";
import type { Clip } from "@/lib/api/types";
import { fmtClock, series } from "@/lib/format";

export function MiniClip({ c }: { c: Clip }) {
  return (
    <Link href={`/clips/${c.id}`} className="clipcard" aria-label={`Clip ${series(c.series_no)}: ${c.hook ?? c.title ?? ""}, ${c.status}`}>
      <Thumb seed={c.id} hook={c.hook} dur={fmtClock(c.duration_s)} live={c.lane === "spike"}>
        <div style={{ position: "absolute", right: 8, bottom: 44 }}><Badge status={c.status} /></div>
      </Thumb>
      <div className="meta">
        <div className="row"><span className="semi pc">{series(c.series_no)}</span><span className="right semi">{c.score ?? "—"}</span></div>
        <div className="t2"><Plat p={c.source_platform} /> {c.creator.handle}</div>
        <div className="t2"><Badge tone={c.lane === "spike" ? "purple" : "blue"}>{c.lane}</Badge></div>
      </div>
    </Link>
  );
}
