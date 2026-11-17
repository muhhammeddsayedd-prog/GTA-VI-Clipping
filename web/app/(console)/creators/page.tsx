import type { Metadata } from "next";
import { Suspense } from "react";

import { CreatorsView } from "./CreatorsView";

export const metadata: Metadata = { title: "Creators" };

export default function CreatorsPage() {
  return <Suspense><CreatorsView /></Suspense>;
}
