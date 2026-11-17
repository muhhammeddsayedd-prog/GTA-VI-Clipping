"use client";

import { useEffect, useState } from "react";

/** Current time, refreshed every `ms` (0 until mounted, so renders stay pure). */
export function useNow(ms = 30_000): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    const tick = () => setNow(Date.now());
    tick();
    const t = setInterval(tick, ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}
