import { describe, expect, it } from "vitest";

import { keywordSet, numericEntries, showValue, splitReason } from "./shape";

describe("numericEntries", () => {
  it("keeps only finite numbers", () => {
    const parts = { hook: 8, source: "visual", visual: { score: 8 }, writer: { score: 4 }, clarity: 8, ok: true, bad: Number.NaN };
    expect(numericEntries(parts)).toEqual([["hook", 8], ["clarity", 8]]);
  });
  it("handles non-objects", () => {
    expect(numericEntries(null)).toEqual([]);
    expect(numericEntries([1, 2])).toEqual([]);
  });
});

describe("keywordSet", () => {
  it("reads the writer's line-keyed object", () => {
    expect([...keywordSet({ "1": ["big boy"], "3": ["Mount him!"] })]).toEqual(["big", "boy", "mount", "him"]);
  });
  it("reads the older array shape", () => {
    expect(keywordSet([{ line: 1, words: ["Jet"] }]).has("jet")).toBe(true);
  });
  it("ignores junk", () => {
    expect(keywordSet("x").size).toBe(0);
    expect(keywordSet({ a: 3 }).size).toBe(0);
  });
});

describe("showValue", () => {
  it("formats nested gate values", () => {
    expect(showValue({ tier: "auto", clips_today: 0 })).toBe("tier auto · clips today 0");
    expect(showValue([20, 60])).toBe("20–60");
    expect(showValue([])).toBe("none");
    expect(showValue(0.43333)).toBe("0.43");
    expect(showValue(null)).toBe("—");
  });
});

describe("splitReason", () => {
  it("separates writer sentences from codes", () => {
    expect(splitReason("writer: Sexual content: dance")).toEqual({ kind: "writer", text: "Sexual content: dance" });
    expect(splitReason("music_over_limit")).toEqual({ kind: "code", text: "music_over_limit" });
    expect(splitReason(null).kind).toBeNull();
  });
});
