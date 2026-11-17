"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient, type UseQueryOptions } from "@tanstack/react-query";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export type Params = Record<string, string | number | boolean | null | undefined>;

export function qs(params?: Params): string {
  if (!params) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  const s = sp.toString();
  return s ? `?${s}` : "";
}

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 401 && typeof window !== "undefined") {
    const next = window.location.pathname + window.location.search;
    // A full reload on purpose: the session is gone, so every cached query must go with it.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign(`/login?expired=1&next=${encodeURIComponent(next)}`);
    throw new ApiError(401, "unauthorized", "Session ended");
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    // Control API errors are {error: "<message>", code, request_id}; the console's own proxy uses {error: {code, message}}.
    const body = data as { error?: string | { code?: string; message?: string }; code?: string } | null;
    const e = body?.error;
    const code = typeof e === "object" ? e?.code : body?.code;
    const message = typeof e === "object" ? e?.message : e;
    throw new ApiError(res.status, code ?? "error", message ?? `Request failed (${res.status})`);
  }
  return data as T;
}

/** GET through the console's own API proxy, e.g. apiGet("admin/clips", { status: "approved" }). */
export async function apiGet<T>(path: string, params?: Params, signal?: AbortSignal): Promise<T> {
  return handle<T>(await fetch(`/api/backend/${path}${qs(params)}`, { cache: "no-store", signal }));
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return handle<T>(
    await fetch(`/api/backend/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

/** Poll intervals (ms). Polling pauses while the tab is hidden (TanStack Query default). */
export const POLL = { status: 10_000, list: 30_000, detail: 15_000, slow: 60_000 } as const;

/** Read a control-API route with caching and polling. */
export function useApi<T>(
  path: string | null,
  params?: Params,
  opts?: { interval?: number | false } & Partial<UseQueryOptions<T, ApiError>>,
) {
  const { interval = POLL.list, ...rest } = opts ?? {};
  return useQuery<T, ApiError>({
    queryKey: [path, params ?? {}],
    queryFn: ({ signal }) => apiGet<T>(path!, params, signal),
    enabled: path !== null,
    refetchInterval: interval,
    placeholderData: keepPreviousData,
    retry: (n, e) => e.status >= 500 && n < 2,
    ...rest,
  });
}

/** POST a control route, then refresh everything that might show the change. */
export function useControl<TBody, TRes>(path: string) {
  const qc = useQueryClient();
  return useMutation<TRes, ApiError, TBody>({
    mutationFn: (body) => apiPost<TRes>(path, body),
    onSuccess: () => qc.invalidateQueries(),
  });
}
