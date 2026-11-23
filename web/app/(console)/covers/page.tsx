import type { Metadata } from "next";
import { Suspense } from "react";

import { CoversView } from "./CoversView";

export const metadata: Metadata = { title: "Covers" };

export default function CoversPage() {
  return <Suspense><CoversView /></Suspense>;
}
