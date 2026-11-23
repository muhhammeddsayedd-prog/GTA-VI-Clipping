"use client";

import { useSyncExternalStore } from "react";

import { DEFAULT_ZONE, getDisplayZone, subscribeDisplayZone } from "@/lib/format";

/** The app's display zone (`settings.app.timezone`); re-renders when the setting changes. */
export function useDisplayZone(): string {
  return useSyncExternalStore(subscribeDisplayZone, getDisplayZone, () => DEFAULT_ZONE);
}
