import "server-only";

/**
 * Server-side configuration. Nothing here is ever sent to the browser.
 *
 * Deployed (Vercel): SUPABASE_URL + SUPABASE_PUBLISHABLE_KEY, CONTROL_API_URL + DASHBOARD_API_TOKEN, ADMIN_EMAIL, APP_ENV.
 * Local development: leave Supabase unset to use the dev login (ADMIN_EMAIL + DEV_ADMIN_PASSWORD + SESSION_SECRET),
 * and leave CONTROL_API_URL unset (or MOCK_API=1) to use generated sample data.
 */

export type AppEnv = "production" | "staging" | "dev";

function str(name: string): string | undefined {
  const v = process.env[name];
  return v && v.trim() !== "" ? v.trim() : undefined;
}

const onVercel = Boolean(process.env.VERCEL);
const isProdBuild = process.env.NODE_ENV === "production";

export const serverEnv = {
  appEnv: (str("APP_ENV") as AppEnv | undefined) ?? "dev",
  adminEmail: (str("ADMIN_EMAIL") ?? "").toLowerCase(),
  supabaseUrl: str("SUPABASE_URL"),
  supabaseKey: str("SUPABASE_PUBLISHABLE_KEY"),
  apiUrl: str("CONTROL_API_URL")?.replace(/\/+$/, ""),
  apiToken: str("DASHBOARD_API_TOKEN"),
  devPassword: str("DEV_ADMIN_PASSWORD"),
  sessionSecret: str("SESSION_SECRET"),
  forceMock: str("MOCK_API") === "1",
  onVercel,
};

/** Supabase Auth when configured; the dev login only outside Vercel, and only in production builds when explicitly allowed. */
export function authMode(): "supabase" | "dev" | "misconfigured" {
  if (serverEnv.supabaseUrl && serverEnv.supabaseKey) return "supabase";
  const devAllowed = !onVercel && (!isProdBuild || str("CONSOLE_DEV_AUTH") === "1");
  if (devAllowed && serverEnv.devPassword && serverEnv.sessionSecret && serverEnv.adminEmail) return "dev";
  return "misconfigured";
}

/** Sample data is never used on Vercel: a deployed console must show real data or say it cannot. */
export function mockApiEnabled(): boolean {
  if (onVercel) return false;
  return serverEnv.forceMock || !serverEnv.apiUrl || !serverEnv.apiToken;
}
