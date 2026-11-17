/** Pure request guards, shared by the API proxy and the sign-in action (unit-tested in guards.test.ts). */

const GET_ALLOWED = [/^admin\/[a-z0-9/_.\-%]+$/i, /^status$/, /^reports\/daily$/];
const POST_ALLOWED = new Set(["control/stop", "control/start", "control/settings"]);

/** Which control-API paths the console may call. Trigger routes (discovery/run, posting/hourly, …) are never allowed. */
export function allowedPath(method: "GET" | "POST", path: string): boolean {
  if (!path || path.includes("..") || path.includes("//") || path.includes("\\")) return false;
  return method === "GET" ? GET_ALLOWED.some((re) => re.test(path)) : POST_ALLOWED.has(path);
}

/** A same-site path to go to after sign-in; anything else becomes "/". */
export function safeNext(raw: unknown): string {
  const v = typeof raw === "string" ? raw : "";
  if (!v.startsWith("/") || v.startsWith("//") || v.startsWith("/\\") || v.startsWith("/login") || v.startsWith("/api/")) return "/";
  return v.length > 512 ? "/" : v;
}

/** Same-origin check for state-changing requests (CSRF defence for route handlers). */
export function sameOrigin(origin: string | null, host: string | null): boolean {
  if (!origin || !host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
