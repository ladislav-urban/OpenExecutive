// The Home screen's wording: the greeting and the single status chip a
// collapsed proposal card keeps. The summary line comes from GET /today
// (`summary`, briefing/home_rules.py). Kept apart from the components so `npm test` can check it (see
// scripts/briefingSummary.test.mjs). No imports, so the test can load this
// under `node --experimental-strip-types`.

/** "Good morning" before noon, "Good afternoon" until 6pm, then "Good evening". */
export function greeting(firstName: string | undefined, now: Date = new Date()): string {
  const h = now.getHours();
  const part = h < 12 ? "morning" : h < 18 ? "afternoon" : "evening";
  const name = firstName?.trim();
  return name ? `Good ${part}, ${name}` : `Good ${part}`;
}

/** Compact age label ("3h", "9d") for a card chip; "" for an unparseable stamp. */
export function ageLabel(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const mins = Math.max(0, Math.round((now.getTime() - t) / 60000));
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/** Whole days between now and an ISO stamp: 0 = due within the day, negative =
 * past (floor, so an 11-hour-old deadline is -1 → "overdue", never "due
 * today"). null if unparseable. */
export function daysUntil(iso: string | null | undefined, now: Date = new Date()): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor((t - now.getTime()) / 86400000);
}

export type ChipTone = "rose" | "amber" | "neutral";

/** The one status chip a collapsed proposal card shows: its deadline when it
 * has one (overdue in red), else "Likely stale" when the review says so, else
 * how long it has waited. The full chip row shows when the card is opened. */
export function proposalStatusChip(
  p: { due_at?: string | null; review_verdict?: string; created_at: string },
  now: Date = new Date(),
): { label: string; tone: ChipTone } | null {
  const dueIn = daysUntil(p.due_at, now);
  if (dueIn != null) {
    if (dueIn < 0) return { label: "Overdue", tone: "rose" };
    return { label: dueIn === 0 ? "Due today" : `Due in ${dueIn}d`, tone: "amber" };
  }
  if ((p.review_verdict ?? "") === "likely_stale") return { label: "Likely stale", tone: "amber" };
  const age = ageLabel(p.created_at, now);
  return age ? { label: `${age} old`, tone: "neutral" } : null;
}
