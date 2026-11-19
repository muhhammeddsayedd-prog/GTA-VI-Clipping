"use client";

import { useAccounts } from "@/lib/use-accounts";

/** The account's username (`@6starsclippy`) for an account id; the id shows on hover, or inline with `withId`. */
export function AccountName({ id, className, withId }: { id: string | null | undefined; className?: string; withId?: boolean }) {
  const { label } = useAccounts();
  if (!id) return <span className="muted">—</span>;
  const name = label(id);
  return (
    <span className={className} title={id}>
      {name}
      {withId && name !== id && <span className="muted mono xs"> · {id}</span>}
    </span>
  );
}
