import type { Metadata } from "next";

import { LiveDetailView } from "./LiveDetailView";

export const metadata: Metadata = { title: "Live session" };

export default async function LiveSessionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <LiveDetailView id={id} />;
}
