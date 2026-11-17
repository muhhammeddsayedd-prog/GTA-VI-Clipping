import { describe, expect, it } from "vitest";

import { fmtAgo, fmtClock, fmtDuration, fmtTime, fmtUsd, series } from "./format";
import { toneOf } from "./status";

const NOW = Date.parse("2026-11-18T14:32:00Z");

describe("format", () => {
  it("formats relative times both ways", () => {
    expect(fmtAgo("2026-11-18T14:23:00Z", NOW)).toBe("9 min ago");
    expect(fmtAgo("2026-11-18T15:00:00Z", NOW)).toBe("in 28 min");
    expect(fmtAgo(null, NOW)).toBe("—");
  });
  it("shows UTC clock times, with the date when not today", () => {
    const now = new Date(NOW);
    expect(fmtTime("2026-11-18T09:05:00Z", false, now)).toBe("09:05");
    expect(fmtTime("2026-11-17T09:05:07Z", true, now)).toBe("17 Nov 09:05:07");
  });
  it("formats durations, clip lengths, money and series numbers", () => {
    expect(fmtDuration(42)).toBe("42 s");
    expect(fmtDuration(3600 * 3 + 600)).toBe("3h 10m");
    expect(fmtClock(34)).toBe("0:34");
    expect(fmtClock(3725)).toBe("1:02:05");
    expect(fmtUsd(14.8)).toBe("$14.80");
    expect(series(142)).toBe("#0142");
    expect(series(null)).toBe("—");
  });
});

describe("status tones", () => {
  it("maps known states and falls back to gray", () => {
    expect(toneOf("published")).toBe("green");
    expect(toneOf("remove_failed")).toBe("red");
    expect(toneOf("precheck_pending")).toBe("amber");
    expect(toneOf("rendering")).toBe("purple");
    expect(toneOf("Not Allowed")).toBe("gray");
    expect(toneOf("something_new")).toBe("gray");
    expect(toneOf(null)).toBe("gray");
  });
});
