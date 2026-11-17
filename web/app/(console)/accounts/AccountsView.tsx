"use client";

import { ArrowUpRight, UserSquare2 } from "lucide-react";

import AnimatedContent from "@/components/reactbits/AnimatedContent";
import { Btn, Card, Empty, ErrorState, PageHead, RefreshBtn, SkeletonCards } from "@/components/kit/primitives";
import { AccountBig } from "@/components/domain/sources/AccountBig";
import { POLL, useApi } from "@/lib/api/client";
import type { AccountSummary } from "@/lib/api/types";

export function AccountsView() {
  const q = useApi<{ items: AccountSummary[] }>("admin/accounts", undefined, { interval: POLL.list });
  return (
    <>
      <PageHead title="Accounts"
        sub="Where clips are posted, through Upload-Post · caps are rolling 24h windows per account, under each platform's global ceiling"
        actions={<><RefreshBtn onClick={() => q.refetch()} spinning={q.isFetching} /><Btn href="https://app.upload-post.com" external icon={<ArrowUpRight size={15} />}>Open Upload-Post</Btn></>} />
      {q.error && !q.data ? <ErrorState error={q.error} retry={() => q.refetch()} /> : !q.data ? <SkeletonCards n={2} h={300} /> : !q.data.items.length ? (
        <Card><Empty icon={<UserSquare2 size={30} />} title="No posting accounts">Accounts are seeded by migration into platform_accounts. None exist in this environment yet.</Empty></Card>
      ) : (
        <AnimatedContent distance={24} duration={0.55} stagger={0.08} trigger="mount">
          <div className="g g2">{q.data.items.map((a) => <AccountBig key={a.id} a={a} />)}</div>
        </AnimatedContent>
      )}
    </>
  );
}
