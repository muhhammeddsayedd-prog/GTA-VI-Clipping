import type { Metadata } from "next";

import { AccountDetailView } from "./AccountDetailView";

export const metadata: Metadata = { title: "Account" };

export default async function AccountPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <AccountDetailView id={id} />;
}
