import type { Metadata } from "next";
import { Suspense } from "react";

import { SpikesView } from "./SpikesView";

export const metadata: Metadata = { title: "Spikes" };

export default function SpikesPage() {
  return <Suspense><SpikesView /></Suspense>;
}
