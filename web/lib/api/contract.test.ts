/**
 * Contract tests: real payloads captured from the production control API (trimmed) must read cleanly.
 * The console was first built against sample data whose shapes differed from the backend; when a backend
 * shape changes again, refresh the fixture and these tests show which reader breaks.
 */
import { describe, expect, it } from "vitest";

import { readBlocks } from "@/components/domain/sources/BlockBanner";
import { isRecord, keywordSet, numericEntries, showValue, splitReason } from "@/lib/shape";
import type { ClipDetail, Discovery, Pipeline, QueueView, SpikeDetail } from "./types";

import approved from "./__fixtures__/clip-approved.json";
import candidates from "./__fixtures__/candidates-page.json";
import discovery from "./__fixtures__/discovery.json";
import gateRejected from "./__fixtures__/clip-gate-rejected.json";
import pipeline from "./__fixtures__/pipeline.json";
import queue from "./__fixtures__/queue.json";
import spike from "./__fixtures__/spike-detail.json";
import videos from "./__fixtures__/videos-page.json";
import writerRejected from "./__fixtures__/clip-writer-rejected.json";

const clip = approved as unknown as ClipDetail;

describe("clip detail", () => {
  it("reads keywords as the writer stores them (object keyed by line)", () => {
    expect(Array.isArray(clip.keywords)).toBe(false);
    expect(keywordSet(clip.keywords).size).toBeGreaterThan(0);
  });
  it("keeps only numeric score parts and finds the visual description", () => {
    const parts = numericEntries(clip.score_parts);
    expect(parts.length).toBeGreaterThan(0);
    expect(parts.every(([, v]) => typeof v === "number")).toBe(true);
    expect(isRecord(clip.score_parts?.visual) && typeof clip.score_parts.visual.seen).toBe("string");
  });
  it("formats every gate check value and limit as text", () => {
    for (const c of Object.values(clip.gate_result?.checks ?? {})) {
      expect(showValue(c.value)).not.toContain("[object");
      expect(showValue(c.limit)).not.toContain("[object");
    }
  });
  it("has per-stage render timings in the clip.rendered event", () => {
    const ev = clip.events.find((e) => e.type === "clip.rendered");
    expect(numericEntries(ev?.data?.timings_ms).length).toBeGreaterThan(0);
  });
  it("tells writer rejections from gate rejections", () => {
    const w = writerRejected as unknown as ClipDetail;
    const g = gateRejected as unknown as ClipDetail;
    expect(splitReason(w.reject_reason).kind).toBe("writer");
    expect(w.gate_result).toBeNull();
    expect(w.has_file).toBe(false);
    expect(splitReason(g.reject_reason).kind).toBe("code");
    expect(g.gate_result?.codes).toContain(g.reject_reason);
  });
});

describe("sources", () => {
  it("reads YouTube blocks from the videos page meta", () => {
    const b = readBlocks((videos as { meta: { source_health: unknown } }).meta.source_health);
    expect(b.map((x) => x.platform)).toEqual(["youtube"]);
    expect(b[0].since).toBeTruthy();
  });
  it("reads the per-source report and run counters from the last discovery run", () => {
    const d = discovery as unknown as Discovery;
    const run = d.runs[0];
    expect(Array.isArray(run.data?.sources)).toBe(true);
    expect(typeof run.data?.sessions_assigned).toBe("number");
    expect(readBlocks(d.source_health.vod).length + readBlocks(d.source_health.live).length).toBeGreaterThan(0);
  });
  it("candidate score parts mix weighted parts with other values", () => {
    const parts = (candidates as { items: { score_parts: unknown }[] }).items[0].score_parts;
    expect(numericEntries(parts).length).toBeLessThan(Object.keys(parts as object).length);
  });
});

describe("pipeline, queue and spikes", () => {
  it("express stages may have no median", () => {
    expect((pipeline as unknown as Pipeline).express_stages.some((s) => s.median_s === null)).toBe(true);
  });
  it("slot decisions carry `code`, and empty slots are their own entity", () => {
    const q = queue as unknown as QueueView;
    expect(q.decisions.some((e) => e.entity_type === "slot")).toBe(true);
    expect(q.decisions.some((e) => typeof e.data?.code === "string")).toBe(true);
  });
  it("chat sample lines carry t_rel", () => {
    const s = spike as unknown as SpikeDetail;
    expect(typeof s.chat_sample?.[0]?.t_rel).toBe("number");
  });
});
