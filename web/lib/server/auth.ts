import "server-only";

import { createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";
import { createServerClient } from "@supabase/ssr";

import { authMode, serverEnv } from "./env";
import { DEV_COOKIE } from "./constants";

export { DEV_COOKIE };
const DEV_SESSION_S = 60 * 60 * 12;

export interface Operator { email: string }

export async function supabaseServer() {
  const store = await cookies();
  return createServerClient(serverEnv.supabaseUrl!, serverEnv.supabaseKey!, {
    cookies: {
      getAll: () => store.getAll(),
      setAll: (list) => {
        try {
          for (const { name, value, options } of list) store.set(name, value, options);
        } catch {
          // Server Components cannot set cookies; the proxy refreshes the session instead.
        }
      },
    },
  });
}

export function isOperatorEmail(email: string | null | undefined): boolean {
  if (!email || !serverEnv.adminEmail) return false;
  const a = Buffer.from(email.trim().toLowerCase());
  const b = Buffer.from(serverEnv.adminEmail);
  return a.length === b.length && timingSafeEqual(a, b);
}

function sign(payload: string): string {
  return createHmac("sha256", serverEnv.sessionSecret!).update(payload).digest("base64url");
}

export function makeDevSession(email: string): string {
  const payload = Buffer.from(JSON.stringify({ e: email, x: Math.floor(Date.now() / 1000) + DEV_SESSION_S })).toString("base64url");
  return `${payload}.${sign(payload)}`;
}

function readDevSession(value: string | undefined): string | null {
  if (!value) return null;
  const [payload, sig] = value.split(".");
  if (!payload || !sig) return null;
  const want = Buffer.from(sign(payload));
  const got = Buffer.from(sig);
  if (want.length !== got.length || !timingSafeEqual(want, got)) return null;
  try {
    const { e, x } = JSON.parse(Buffer.from(payload, "base64url").toString()) as { e: string; x: number };
    return x > Date.now() / 1000 ? e : null;
  } catch {
    return null;
  }
}

/** The signed-in user's email, verified with the auth server (not just read from a cookie), or null. */
export async function sessionEmail(): Promise<string | null> {
  const mode = authMode();
  if (mode === "supabase") {
    const supabase = await supabaseServer();
    const { data } = await supabase.auth.getUser();
    return data.user?.email ?? null;
  }
  if (mode === "dev") {
    const store = await cookies();
    return readDevSession(store.get(DEV_COOKIE)?.value);
  }
  return null;
}

/** The operator, or null when nobody (or somebody else) is signed in. */
export async function getOperator(): Promise<Operator | null> {
  const email = await sessionEmail();
  return email && isOperatorEmail(email) ? { email } : null;
}

export async function signOutEverywhere(): Promise<void> {
  const mode = authMode();
  if (mode === "supabase") {
    const supabase = await supabaseServer();
    await supabase.auth.signOut();
  }
  const store = await cookies();
  store.delete(DEV_COOKIE);
}

export function devPasswordMatches(password: string): boolean {
  const a = Buffer.from(password);
  const b = Buffer.from(serverEnv.devPassword ?? "");
  return b.length > 0 && a.length === b.length && timingSafeEqual(a, b);
}
