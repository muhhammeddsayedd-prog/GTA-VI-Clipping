"use client";

import { useMemo } from "react";

import { useApi } from "@/lib/api/client";
import type { AccountSummary } from "@/lib/api/types";

/**
 * The posting accounts by id, so screens can show the channel's username (`@6starsclippy`) instead of the
 * internal id (`instagram_main`). One cached request shared by every screen; refreshed every 10 minutes.
 */
export function useAccounts() {
  const q = useApi<{ items: AccountSummary[] }>("admin/accounts", undefined, { interval: 10 * 60 * 1000, staleTime: 10 * 60 * 1000 });
  const byId = useMemo(() => new Map((q.data?.items ?? []).map((a) => [a.id, a])), [q.data]);
  return { items: q.data?.items ?? [], byId, label: (id: string | null | undefined) => accountLabel(id ? byId.get(id) : undefined, id) };
}

/** Username of the account, with " · spike" when it posts only spike clips; the id when no handle is known. */
export function accountLabel(a: Pick<AccountSummary, "handle" | "lanes"> | undefined, id?: string | null): string {
  if (!a?.handle) return id ?? "—";
  const handle = a.handle.startsWith("@") ? a.handle : `@${a.handle}`;
  return a.lanes.length === 1 && a.lanes[0] === "spike" ? `${handle} · spike` : handle;
}
