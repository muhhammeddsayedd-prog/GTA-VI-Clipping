"use client";

import { useActionState, useState } from "react";
import { AlertTriangle, ArrowUpRight, Clock, Eye, EyeOff, Info, Lock, Mail } from "lucide-react";

import { signIn, type SignInState } from "./actions";

export function LoginForm({ next, expired, env }: { next: string; expired: boolean; env: string }) {
  const [state, action, pending] = useActionState<SignInState, FormData>(signIn, { error: null });
  const [show, setShow] = useState(false);
  const err = Boolean(state.error);
  return (
    <div className="form">
      <span className="chip p" style={{ alignSelf: "flex-start" }}><Lock size={12} /> Private console · one operator</span>
      <h1 style={{ marginTop: 18 }}>Sign in</h1>
      <p className="muted" style={{ margin: "8px 0 30px" }}>Use the operator account for this environment.</p>

      {expired && !err && (
        <div className="banner blue" role="status" style={{ margin: "0 0 18px" }}>
          <Clock size={17} /><div><b>Your session ended.</b><div className="small" style={{ marginTop: 2 }}>Sign in again to go back where you were.</div></div>
        </div>
      )}
      {err && (
        <div className="banner red" role="alert" style={{ margin: "0 0 18px" }}>
          <AlertTriangle size={17} /><div><b>{state.error}</b><div className="small" style={{ marginTop: 2 }}>Check both and try again.</div></div>
        </div>
      )}

      <form action={action} noValidate>
        <input type="hidden" name="next" value={next} />
        <label className="flabel" htmlFor="email" style={{ display: "block" }}>Email</label>
        <div className={`field${err ? " err" : ""}`}>
          <Mail size={15} />
          <input id="email" name="email" type="email" className="field-input" autoComplete="username" required defaultValue={state.email ?? ""} placeholder="Email address" autoFocus aria-invalid={err} />
        </div>
        <label className="flabel" htmlFor="password" style={{ marginTop: 16, display: "block" }}>Password</label>
        <div className={`field${err ? " err" : ""}`}>
          <Lock size={15} />
          <input id="password" name="password" type={show ? "text" : "password"} className="field-input" autoComplete="current-password" required aria-invalid={err} />
          <button type="button" onClick={() => setShow((s) => !s)} aria-label={show ? "Hide password" : "Show password"} style={{ border: 0, background: "none", color: "var(--mute)", display: "grid", padding: 0 }}>
            {show ? <EyeOff size={15} /> : <Eye size={15} />}
          </button>
        </div>
        <button type="submit" className="btn p" disabled={pending} style={{ justifyContent: "center", marginTop: 26, padding: 15, width: "100%" }}>
          {pending ? "Signing in…" : <>Sign in <ArrowUpRight size={15} /></>}
        </button>
      </form>

      <div className="muted small" style={{ marginTop: 22, lineHeight: 1.6, display: "flex", gap: 8 }}>
        <Info size={14} style={{ flex: "none", marginTop: 3 }} />
        <span>Sign-up is turned off. Only the operator account can open this console. Lost the password? Reset it in the Supabase dashboard for this environment.</span>
      </div>
      <div className="row muted xs" style={{ marginTop: "auto", paddingTop: 40 }}>
        <span className={`envchip${env === "staging" ? " stg" : ""}`} style={{ padding: "5px 9px" }}>{env === "production" ? "PROD" : env === "staging" ? "STAGING" : "DEV"}</span>
        6 Stars Clips console
      </div>
    </div>
  );
}
