import { describe, expect, it } from "vitest";

import type { Json } from "@/lib/api/types";

import { readTakedownActions, takedownSummary } from "./takedown";

const actioned: Json = JSON.parse(JSON.stringify({
  claimed_received_at: null, truncated: false, kind: "copyright", summary: "Remove clip", state: "done", strike: true,
  matched: { creators: ["darkviperau"], posts: 2, how: "url" },
  items: [
    { action: "block_creator", creator: "darkviperau", newly_blocked: true, clips_dropped: 1, pending_posts_cancelled: 0 },
    { action: "remove_post", post_id: "p1", platform: "youtube", outcome: "removed" },
    { action: "remove_post", post_id: "p2", platform: "tiktok", outcome: "manual", detail: "Upload-Post cannot delete tiktok posts" },
  ],
}));

describe("readTakedownActions", () => {
  it("reads the guardian's object form", () => {
    const a = readTakedownActions(actioned);
    expect(a.strike).toBe(true);
    expect(a.items).toHaveLength(3);
    expect(a.matched).toEqual({ creators: ["darkviperau"], posts: 2, how: "url" });
  });
  it("reads the older array form and junk", () => {
    expect(readTakedownActions([{ action: "strike" }]).strike).toBe(true);
    expect(readTakedownActions(null).items).toEqual([]);
  });
});

describe("takedownSummary", () => {
  it("summarises actions", () => {
    expect(takedownSummary("partial", actioned)).toBe("blocked · 1 removed · 1 by hand · strike");
    expect(takedownSummary("ignored", null)).toBe("not a takedown");
    expect(takedownSummary("open", { state: "unclassified", why: "limit" })).toBe("needs you · not classified");
  });
});
