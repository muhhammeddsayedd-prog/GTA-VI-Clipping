"use client";

import { useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

/** One URL param that opens a drawer (deep-linkable), kept apart from list filters so paging is not reset. */
export function useDrawerParam(name: string) {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const value = sp.get(name) ?? "";
  const hrefFor = useCallback((v: string) => {
    const next = new URLSearchParams(sp.toString());
    if (v) next.set(name, v); else next.delete(name);
    return `${pathname}${next.size ? `?${next}` : ""}`;
  }, [sp, pathname, name]);
  const set = useCallback((v: string) => router.replace(hrefFor(v), { scroll: false }), [router, hrefFor]);
  return { value, set, hrefFor };
}
