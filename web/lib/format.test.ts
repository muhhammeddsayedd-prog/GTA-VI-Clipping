import { describe, expect, it } from "vitest";

import { fmtAgo, fmtClock, fmtDay, fmtDuration, fmtFull, fmtTime, fmtUsd, isValidZone, series, utcHourInZone, utcTitle, zoneLabel, zoneOffset, zonedHour } from "./format";
import { toneOf } from "./status";

const NOW = Date.parse("2026-11-18T14:32:00Z");

describe("format", () => {
  it("formats relative times both ways", () => {
    expect(fmtAgo("2026-11-18T14:23:00Z", NOW)).toBe("9 min ago");
    expect(fmtAgo("2026-11-18T15:00:00Z", NOW)).toBe("in 28 min");
    expect(fmtAgo(null, NOW)).toBe("—");
  });
  it("shows clock times in the zone, with the date when not today", () => {
    const now = new Date(NOW);
    expect(fmtTime("2026-11-18T09:05:00Z", false, now, "UTC")).toBe("09:05");
    expect(fmtTime("2026-11-17T09:05:07Z", true, now, "UTC")).toBe("17 Nov 09:05:07");
  });
  it("defaults to Cairo (UTC+2 in winter, UTC+3 in summer)", () => {
    const now = new Date(NOW);
    expect(fmtTime("2026-11-18T09:05:00Z", false, now)).toBe("11:05");
    expect(zonedHour("2026-07-10T12:00:00Z")).toBe(15);
    expect(zoneOffset("Africa/Cairo", NOW)).toBe("UTC+2");
    expect(zoneOffset("Africa/Cairo", Date.parse("2026-07-10T12:00:00Z"))).toBe("UTC+3");
    expect(zoneOffset("Asia/Kolkata", NOW)).toBe("UTC+5:30");
    expect(zoneOffset("UTC", NOW)).toBe("UTC");
  });
  it("rolls the day over at local midnight", () => {
    const now = new Date("2026-11-18T23:00:00Z"); // 01:00 on 19 Nov in Cairo
    expect(fmtTime("2026-11-18T22:30:00Z", false, now)).toBe("00:30");
    expect(fmtTime("2026-11-18T21:30:00Z", false, now)).toBe("18 Nov 23:30");
    expect(fmtDay("2026-11-18T22:30:00Z")).toBe("19 Nov 2026");
    expect(fmtDay("2026-11-18T22:30:00Z", "UTC")).toBe("18 Nov 2026");
    expect(fmtFull("2026-11-18T22:30:00Z", "America/New_York")).toBe("18 Nov 2026 17:30 New York");
  });
  it("hover titles carry UTC, UTC slots show the zone hour", () => {
    expect(utcTitle("2026-11-18T09:05:07Z")).toBe("18 Nov 2026 09:05:07 UTC");
    expect(utcHourInZone(0, "Africa/Cairo", NOW)).toBe("02:00 Cairo");
    expect(utcHourInZone(23, "UTC", NOW)).toBe("23:00 UTC");
  });
  it("labels and validates zones", () => {
    expect(zoneLabel("Africa/Cairo")).toBe("Cairo");
    expect(zoneLabel("America/Los_Angeles")).toBe("Los Angeles");
    expect(zoneLabel("UTC")).toBe("UTC");
    expect(isValidZone("Europe/London")).toBe(true);
    expect(isValidZone("Mars/Base")).toBe(false);
    expect(isValidZone("")).toBe(false);
    expect(isValidZone(4)).toBe(false);
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
