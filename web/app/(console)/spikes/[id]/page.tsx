import type { Metadata } from "next";

import { SpikeDetailView } from "./SpikeDetailView";

export const metadata: Metadata = { title: "Spike" };

export default async function SpikePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <SpikeDetailView id={id} />;
}
