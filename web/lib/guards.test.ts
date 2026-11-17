import { describe, expect, it } from "vitest";

import { allowedPath, safeNext, sameOrigin } from "./guards";

describe("allowedPath", () => {
  it("allows admin reads, status and the daily report", () => {
    expect(allowedPath("GET", "admin/clips")).toBe(true);
    expect(allowedPath("GET", "admin/clips/9c1e-aa/media")).toBe(true);
    expect(allowedPath("GET", "status")).toBe(true);
    expect(allowedPath("GET", "reports/daily")).toBe(true);
  });
  it("refuses trigger routes and anything outside the list", () => {
    for (const p of ["discovery/run", "posting/hourly", "guardian/check", "guardian/takedown", "stats/collect", "health", ""]) {
      expect(allowedPath("GET", p)).toBe(false);
      expect(allowedPath("POST", p)).toBe(false);
    }
  });
  it("refuses traversal", () => {
    expect(allowedPath("GET", "admin/../control/stop")).toBe(false);
    expect(allowedPath("GET", "admin//x")).toBe(false);
  });
  it("allows only the three control writes", () => {
    expect(allowedPath("POST", "control/stop")).toBe(true);
    expect(allowedPath("POST", "control/start")).toBe(true);
    expect(allowedPath("POST", "control/settings")).toBe(true);
    expect(allowedPath("POST", "admin/clips")).toBe(false);
    expect(allowedPath("GET", "control/stop")).toBe(false);
  });
});

describe("safeNext", () => {
  it("keeps same-site paths", () => {
    expect(safeNext("/clips/abc?x=1")).toBe("/clips/abc?x=1");
  });
  it("drops open redirects and loops", () => {
    for (const v of ["https://evil.example", "//evil.example", "/\\evil", "/login?next=/", "/api/backend/status", "", null, 5]) {
      expect(safeNext(v)).toBe("/");
    }
  });
});

describe("sameOrigin", () => {
  it("matches only the same host", () => {
    expect(sameOrigin("https://console.example", "console.example")).toBe(true);
    expect(sameOrigin("https://evil.example", "console.example")).toBe(false);
    expect(sameOrigin(null, "console.example")).toBe(false);
    expect(sameOrigin("not a url", "console.example")).toBe(false);
  });
});
