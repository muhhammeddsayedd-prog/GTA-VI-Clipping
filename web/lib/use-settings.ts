"use client";

import { useMemo } from "react";

import { POLL, useApi } from "@/lib/api/client";
import type { Json, Setting } from "@/lib/api/types";

/**
 * Live values from the `settings` table, so pages show the thresholds the system really uses instead of
 * numbers copied into the UI. Shares one cached request with the Settings page.
 */
export function useSettings() {
  const q = useApi<{ items: Setting[] }>("admin/settings", undefined, { interval: POLL.slow });
  const map = useMemo(() => new Map((q.data?.items ?? []).map((s) => [s.key, s.value] as [string, Json])), [q.data]);
  return {
    ready: Boolean(q.data),
    get: (key: string): Json | undefined => map.get(key),
    num: (key: string): number | null => { const v = map.get(key); return typeof v === "number" && Number.isFinite(v) ? v : null; },
  };
}
