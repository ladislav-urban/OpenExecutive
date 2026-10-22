// The navigation's counts (GET /badges): Home's "Needs you", the knowledge
// review queue and the watch suggestions, keyed by destination. A count the
// API couldn't read is absent, so a badge keeps what it showed.

export type BadgeCounts = Record<string, number>;

/** The sidebar's Knowledge badge from these counts; `previous` when the
 *  review queue's count is missing. */
export function reviewBadgeFrom(counts: BadgeCounts, previous: number): number {
  const n = counts.knowledge;
  return typeof n === "number" && Number.isInteger(n) && n >= 0 ? n : previous;
}
