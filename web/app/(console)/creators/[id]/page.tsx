import type { Metadata } from "next";

import { CreatorDetailView } from "./CreatorDetailView";

export const metadata: Metadata = { title: "Creator" };

export default async function CreatorPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <CreatorDetailView id={id} />;
}
