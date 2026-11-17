import { redirect } from "next/navigation";

import { Shell } from "@/components/shell/Shell";
import { getOperator, sessionEmail, signOutEverywhere } from "@/lib/server/auth";
import { serverEnv, mockApiEnabled } from "@/lib/server/env";

// Never prerender: every console page depends on the signed-in session.
export const dynamic = "force-dynamic";

export default async function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const operator = await getOperator();
  if (!operator) {
    // Signed in as somebody else: end that session and explain; nobody signed in: go to the login page.
    if (await sessionEmail()) redirect("/denied");
    redirect("/login");
  }

  async function signOut() {
    "use server";
    await signOutEverywhere();
    redirect("/login");
  }

  return (
    <Shell env={serverEnv.appEnv} email={operator.email} mock={mockApiEnabled()} signOut={signOut}>
      {children}
    </Shell>
  );
}
