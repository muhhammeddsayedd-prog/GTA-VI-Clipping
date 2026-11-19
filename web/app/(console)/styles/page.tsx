import type { Metadata } from "next";
import { Suspense } from "react";

import { StylesView } from "./StylesView";

export const metadata: Metadata = { title: "Design styles" };

export default function StylesPage() {
  return <Suspense><StylesView /></Suspense>;
}
