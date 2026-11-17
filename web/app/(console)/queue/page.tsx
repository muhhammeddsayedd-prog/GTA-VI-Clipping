import type { Metadata } from "next";

import { QueueView } from "./QueueView";

export const metadata: Metadata = { title: "Posting queue" };

export default function QueuePage() {
  return <QueueView />;
}
