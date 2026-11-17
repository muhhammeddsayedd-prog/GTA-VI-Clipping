import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { getOperator } from "@/lib/server/auth";
import { serverEnv } from "@/lib/server/env";
import { LoginArt } from "./LoginArt";
import { LoginForm } from "./LoginForm";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string; expired?: string }> }) {
  if (await getOperator()) redirect("/");
  const sp = await searchParams;
  return (
    <div style={{ minHeight: "100vh" }}>
      <div className="login">
        <LoginArt />
        <LoginForm next={sp.next ?? "/"} expired={sp.expired === "1"} env={serverEnv.appEnv} />
      </div>
    </div>
  );
}
