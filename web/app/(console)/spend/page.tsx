import type { Metadata } from "next";
import { Suspense } from "react";

import { SpendView } from "./SpendView";

export const metadata: Metadata = { title: "Spend" };

export default function SpendPage() {
  return <Suspense><SpendView /></Suspense>;
}
