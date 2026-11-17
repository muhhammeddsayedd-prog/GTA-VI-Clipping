export type DiffLine = { kind: "ctx" | "del" | "add"; text: string };

/** Line diff (LCS) of two texts; unchanged runs longer than `context` lines on each side are folded. */
export function lineDiff(a: string, b: string, context = 2): DiffLine[] {
  const x = a.split("\n"), y = b.split("\n");
  const n = x.length, m = y.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push({ kind: "ctx", text: x[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ kind: "del", text: x[i++] });
    else out.push({ kind: "add", text: y[j++] });
  }
  while (i < n) out.push({ kind: "del", text: x[i++] });
  while (j < m) out.push({ kind: "add", text: y[j++] });
  // fold long unchanged runs
  const keep = out.map((l, k) => l.kind !== "ctx" || out.slice(Math.max(0, k - context), k + context + 1).some((o) => o.kind !== "ctx"));
  const folded: DiffLine[] = [];
  out.forEach((l, k) => {
    if (keep[k]) folded.push(l);
    else if (keep[k - 1] !== false) folded.push({ kind: "ctx", text: "  …" });
  });
  return folded;
}

export function jsonKind(v: unknown): "null" | "boolean" | "number" | "string" | "array" | "object" {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  if (typeof v === "boolean") return "boolean";
  if (typeof v === "number") return "number";
  if (typeof v === "string") return "string";
  return "object";
}

/** Same rule as the control API (`kind_allowed`): keep the JSON type; a stored null accepts anything; a string may become null. */
export function kindAllowed(old: unknown, next: unknown): boolean {
  const o = jsonKind(old), n = jsonKind(next);
  return o === n || o === "null" || (o === "string" && n === "null");
}
