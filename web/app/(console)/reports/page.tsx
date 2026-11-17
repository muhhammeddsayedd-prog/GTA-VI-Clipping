import type { Metadata } from "next";
import { Suspense } from "react";

import { ReportView } from "./ReportView";

export const metadata: Metadata = { title: "Daily report" };

export default function ReportsPage() {
  return <Suspense><ReportView /></Suspense>;
}
