import type { Metadata } from "next";
import { Suspense } from "react";

import { GuardianView } from "./GuardianView";

export const metadata: Metadata = { title: "Guardian" };

export default function GuardianPage() {
  return <Suspense><GuardianView /></Suspense>;
}
