import type { Metadata } from "next";

import { ClipsView } from "./ClipsView";

export const metadata: Metadata = { title: "Clips" };

export default async function ClipsPage({ searchParams }: { searchParams: Promise<{ view?: string }> }) {
  const sp = await searchParams;
  return <ClipsView view={sp.view === "table" ? "table" : "grid"} />;
}
