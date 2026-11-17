import type { Metadata } from "next";

import { VideoDetailView } from "./VideoDetailView";

export const metadata: Metadata = { title: "Video" };

export default async function VideoPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <VideoDetailView id={id} />;
}
