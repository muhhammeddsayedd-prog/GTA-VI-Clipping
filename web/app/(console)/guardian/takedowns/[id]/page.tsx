import type { Metadata } from "next";

import { TakedownView } from "./TakedownView";

export const metadata: Metadata = { title: "Takedown" };

export default async function TakedownPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <TakedownView id={id} />;
}
