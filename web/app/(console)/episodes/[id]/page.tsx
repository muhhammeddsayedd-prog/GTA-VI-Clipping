import type { Metadata } from "next";

import { EpisodeDetailView } from "./EpisodeDetailView";

export const metadata: Metadata = { title: "Episode" };

export default async function EpisodePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <EpisodeDetailView id={id} />;
}
