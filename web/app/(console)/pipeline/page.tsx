import type { Metadata } from "next";

import { PipelineView } from "./PipelineView";

export const metadata: Metadata = { title: "Pipeline" };

export default function PipelinePage() {
  return <PipelineView />;
}
