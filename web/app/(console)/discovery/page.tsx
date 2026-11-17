import type { Metadata } from "next";
import { Suspense } from "react";

import { DiscoveryView } from "./DiscoveryView";

export const metadata: Metadata = { title: "Discovery" };

export default function DiscoveryPage() {
  return <Suspense><DiscoveryView /></Suspense>;
}
