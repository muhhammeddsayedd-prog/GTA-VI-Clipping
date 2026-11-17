export interface MockResult { status: number; body: unknown }
export interface MockRoute {
  method: "GET" | "POST";
  pattern: RegExp;
  handle: (m: RegExpMatchArray, q: URLSearchParams, body: unknown) => unknown;
}
export class MockNotFound extends Error {}
export const notFound = (what = "item"): MockResult => ({ status: 404, body: { error: { code: "not_found", message: `unknown ${what}` } } });

/** Throw inside a handler for a 404. */
export function need<T>(v: T | undefined | null): T {
  if (v === undefined || v === null) throw new MockNotFound();
  return v;
}

/** Return from a handler to answer with a non-200 status. */
export function withStatus(status: number, body: unknown) {
  return { __status: status, body };
}

/** In-memory control state for the dev server. */
export const controlState = { stopped: false, reason: null as string | null, dryRun: false };
