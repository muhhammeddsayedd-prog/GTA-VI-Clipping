import type { Metadata } from "next";
import { Suspense } from "react";

import { EpisodesView } from "./EpisodesView";

export const metadata: Metadata = { title: "Episodes" };

export default function EpisodesPage() {
  return <Suspense><EpisodesView /></Suspense>;
}
