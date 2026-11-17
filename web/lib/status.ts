/** One colour and label per state across the console. Values come from the DB CHECK constraints. Unknown → gray. */

export type Tone = "green" | "purple" | "blue" | "amber" | "red" | "gray" | "dark";

const TONES: Partial<Record<Tone, string>> = {
  green: "published posted approved done recording live ok actioned sent delivered clipped confirmed preferred healthy passed fresh enabled succeeded up recovered on_time",
  purple: "rendering gating processing running assigned spike express sending",
  blue: "queued new hourly info pending planned scheduled",
  amber: "precheck_pending partial warn paused dry_run suspect stale restricted retrying deferred released slow failover unverifiable",
  red: "failed rejected removed remove_failed blocked critical error claimed stopped open down removed_by_platform incident unreachable undelivered",
  gray: "ended expired skipped cancelled ignored merged replay_done replay_queued debug auto disabled n/a unsent not_allowed empty",
};

const MAP: Record<string, Tone> = {};
for (const [tone, words] of Object.entries(TONES)) for (const w of (words ?? "").split(" ")) MAP[w] = tone as Tone;

export function toneOf(status: string | null | undefined): Tone {
  if (!status) return "gray";
  return MAP[status.toLowerCase().replace(/[\s-]/g, "_")] ?? "gray";
}

export const LEVEL_TONE: Record<string, Tone> = { debug: "gray", info: "blue", warn: "amber", error: "red", critical: "red" };
export const SEVERITY_TONE: Record<string, Tone> = { info: "blue", warn: "amber", critical: "red" };
