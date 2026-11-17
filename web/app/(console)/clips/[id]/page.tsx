import type { Metadata } from "next";

import { ClipDetailView } from "./ClipDetailView";

export const metadata: Metadata = { title: "Clip" };

export default async function ClipPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ClipDetailView id={id} />;
}
