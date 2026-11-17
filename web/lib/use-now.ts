"use client";

import { useSyncExternalStore } from "react";

/** A clock that ticks every `stepMs` without calling Date.now() during render. Server snapshot is 0. */
const listeners = new Set<() => void>();
let now = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(cb: () => void) {
  listeners.add(cb);
  if (!timer) {
    now = Date.now();
    timer = setInterval(() => {
      now = Date.now();
      listeners.forEach((l) => l());
    }, 15_000);
  }
  return () => {
    listeners.delete(cb);
    if (!listeners.size && timer) { clearInterval(timer); timer = null; }
  };
}

export function useNow(): number {
  return useSyncExternalStore(subscribe, () => now || Date.now(), () => 0);
}

const noop = () => () => {};
/** True after hydration (for portals). */
export function useMounted(): boolean {
  return useSyncExternalStore(noop, () => true, () => false);
}
