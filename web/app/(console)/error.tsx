"use client";

import { ErrorState } from "@/components/kit/primitives";

export default function ConsoleError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <div style={{ marginTop: 26 }}><ErrorState error={{ message: error.message || "This page failed to load." }} retry={reset} /></div>;
}
