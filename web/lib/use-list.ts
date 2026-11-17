"use client";

import { useCallback, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

/**
 * Filters live in the URL (shareable, survive reloads); the cursor stack lives in state and resets when filters change.
 * `params` is ready to pass to useApi; it includes `cursor` when not on page 1.
 */
export function useListParams(keys: string[], defaults: Record<string, string> = {}) {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const filterKey = keys.map((k) => `${k}=${sp.get(k) ?? ""}`).join("&");
  const [stack, setStack] = useState<{ key: string; cursors: (string | null)[] }>({ key: filterKey, cursors: [null] });
  const cursors = useMemo<(string | null)[]>(() => (stack.key === filterKey ? stack.cursors : [null]), [stack, filterKey]);

  const get = useCallback((k: string) => sp.get(k) ?? defaults[k] ?? "", [sp, defaults]);
  const set = useCallback((k: string, v: string) => {
    const next = new URLSearchParams(sp.toString());
    if (v && v !== defaults[k]) next.set(k, v); else next.delete(k);
    router.replace(`${pathname}${next.size ? `?${next}` : ""}`, { scroll: false });
  }, [sp, router, pathname, defaults]);

  const params = useMemo(() => {
    const p: Record<string, string> = {};
    for (const k of keys) { const v = sp.get(k) ?? defaults[k]; if (v) p[k] = v; }
    const c = cursors[cursors.length - 1];
    if (c) p.cursor = c;
    return p;
  }, [keys, sp, defaults, cursors]);

  return {
    get, set, params, cursors,
    setCursors: (c: (string | null)[]) => setStack({ key: filterKey, cursors: c.length ? c : [null] }),
  };
}
