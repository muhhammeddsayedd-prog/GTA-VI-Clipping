import type { Metadata } from "next";
import { Suspense } from "react";

import { VideosView } from "./VideosView";

export const metadata: Metadata = { title: "VOD queue" };

export default function VideosPage() {
  return <Suspense><VideosView /></Suspense>;
}
