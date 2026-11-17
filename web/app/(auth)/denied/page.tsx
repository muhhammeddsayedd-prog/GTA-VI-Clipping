import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { ArrowLeft, Lock } from "lucide-react";

import { sessionEmail, signOutEverywhere } from "@/lib/server/auth";

export const metadata: Metadata = { title: "Access denied" };
export const dynamic = "force-dynamic";

export default async function DeniedPage() {
  const email = await sessionEmail();

  async function leave() {
    "use server";
    await signOutEverywhere();
    redirect("/login");
  }

  return (
    <div className="app" style={{ minHeight: "calc(100vh - 56px)", display: "grid", placeItems: "center" }}>
      <div className="empty" role="alert">
        <div>
          <div className="eic" style={{ background: "var(--rs)", color: "var(--red)" }}><Lock size={34} /></div>
          <h2>This account can’t open the console</h2>
          <p>
            {email ? <>You are signed in as <b>{email}</b>. </> : null}
            Only the operator account is allowed here.
          </p>
          <form action={leave} className="row" style={{ justifyContent: "center" }}>
            <button type="submit" className="btn p"><ArrowLeft size={15} />Sign out and go back</button>
          </form>
        </div>
      </div>
    </div>
  );
}
