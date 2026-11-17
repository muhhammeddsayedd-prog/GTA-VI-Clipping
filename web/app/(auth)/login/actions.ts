"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { DEV_COOKIE, devPasswordMatches, isOperatorEmail, makeDevSession, supabaseServer } from "@/lib/server/auth";
import { authMode } from "@/lib/server/env";
import { safeNext } from "@/lib/guards";

export interface SignInState { error: string | null; email?: string }

const GENERIC = "Email or password is incorrect.";

export async function signIn(_prev: SignInState, form: FormData): Promise<SignInState> {
  const email = String(form.get("email") ?? "").trim().toLowerCase();
  const password = String(form.get("password") ?? "");
  const next = safeNext(form.get("next"));
  if (!email || !password) return { error: "Enter your email and password.", email };
  if (email.length > 254 || password.length > 256) return { error: GENERIC, email };

  // Same answer, same rough timing, for every failure: nobody learns which part was wrong.
  const started = Date.now();
  const slow = async () => { const wait = 450 - (Date.now() - started); if (wait > 0) await new Promise((r) => setTimeout(r, wait)); };

  const mode = authMode();
  if (mode === "misconfigured") return { error: "Sign-in is not configured for this environment yet.", email };

  if (!isOperatorEmail(email)) { await slow(); return { error: GENERIC, email }; }

  if (mode === "supabase") {
    const supabase = await supabaseServer();
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) { await slow(); return { error: error.status === 429 ? "Too many attempts. Wait a minute and try again." : GENERIC, email }; }
  } else {
    if (!devPasswordMatches(password)) { await slow(); return { error: GENERIC, email }; }
    const store = await cookies();
    store.set(DEV_COOKIE, makeDevSession(email), { httpOnly: true, sameSite: "lax", secure: process.env.NODE_ENV === "production", path: "/", maxAge: 60 * 60 * 12 });
  }
  redirect(next);
}
