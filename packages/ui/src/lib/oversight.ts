// The ERPNext draft oversight record (GET /erpnext/drafts/oversight), as the
// principal downloads it from the audit log: every draft card in a range,
// who asked, what it showed, who decided and what became of it in ERPNext.
// The route answers the principal only, so the page offers it only to them.

const API_BASE = "/api/backend";

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** The day after a `YYYY-MM-DD` date, in the same form. */
function nextDay(day: string): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10);
}

/**
 * The export's URL for the days `from` to `to`, both included (`YYYY-MM-DD`,
 * either may be empty for an open end). The route's range is [since, until),
 * so `until` is the day after `to`. ERPNext's state of each made draft is
 * asked for (`erpnext=1`), as an auditor needs it. Null for a malformed date
 * or a range that ends before it starts.
 */
export function oversightCsvUrl(from: string, to: string): string | null {
  const since = from.trim();
  const last = to.trim();
  if ((since && !DAY.test(since)) || (last && !DAY.test(last))) return null;
  if (since && last && last < since) return null;
  const params = new URLSearchParams();
  if (since) params.set("since", since);
  if (last) params.set("until", nextDay(last));
  params.set("erpnext", "1");
  params.set("format", "csv");
  return `${API_BASE}/erpnext/drafts/oversight?${params}`;
}

/** The filename the route gives, or a dated fallback. */
export function oversightFilename(disposition: string | null, today: Date = new Date()): string {
  const match = disposition ? /filename="([^"]+)"/.exec(disposition) : null;
  const named = match?.[1] ?? "";
  if (/^[\w.-]+\.csv$/.test(named)) return named;
  return `erpnext-draft-oversight-${today.toISOString().slice(0, 10).replaceAll("-", "")}.csv`;
}

/** The route's own message for a refused or failed export. */
async function failure(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    // not JSON: fall through
  }
  return `The export failed (${res.status}).`;
}

/** Downloads the record for the range; throws with the route's message on failure. */
export async function downloadOversightCsv(from: string, to: string): Promise<void> {
  const url = oversightCsvUrl(from, to);
  if (url === null) throw new Error("Choose a start date on or before the end date.");
  const res = await fetch(url);
  if (!res.ok) throw new Error(await failure(res));
  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = oversightFilename(res.headers.get("Content-Disposition"));
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoked on the next tick: some browsers start the download after click returns.
  setTimeout(() => URL.revokeObjectURL(href), 0);
}
