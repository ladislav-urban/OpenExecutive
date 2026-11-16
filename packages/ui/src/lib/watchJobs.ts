// Settings → <product> → Watch jobs: the words the card shows, kept apart
// from the card so they can be tested (scripts/watchJobs.test.mjs). Every
// line is ours: a code from the API is looked up here, and whatever the API
// says in prose is never shown.

export interface WatchRun {
  day: string;
  // "HH:MM" for a scheduled slot, "" for a once-a-day job or a run on request.
  slot: string;
  status: string;
  cards: number;
  // A reason code (watch/runner.py REASONS), never ERPNext's text.
  error: string;
  run_now?: boolean;
}

export interface WatchJob {
  id: string;
  title: string;
  enabled: boolean;
  runs_at: string;
  slots?: string[];
  weekdays?: number[];
  card_cap_day: number;
  last_run: WatchRun | null;
}

export interface WatchJobs {
  master_switch: boolean;
  jobs: WatchJob[];
}

// What POST /watch/jobs/{id}/run answers when it won't start the run, by code.
export const RUN_NOW_REFUSALS: Record<string, string> = {
  job_off: "Turn the job on first. Only a job that's on can run.",
  paused: "Autonomous work is paused. Resume it first.",
  held: "Jobs are on hold until the company is set up.",
  already_running: "It's running now. Try again once it's done.",
  run_now_cap: "It has already been run on request 3 times today.",
  unknown_job: "This server doesn't know that job.",
  run_now_unavailable: "Couldn't start it. Try again in a minute.",
  not_yours: "Only the account owner can run a watch job.",
  no_principal: "Watch jobs need the account owner's entry in People, with their email.",
  caller_signing_required: "Watch jobs need signed sign-ins on this server.",
};

export const RUN_NOW_FALLBACK = "Couldn't start it. Try again in a minute.";

/** The line for a refused run, from its code alone. */
export function runNowRefusal(code: unknown): string {
  return typeof code === "string" && Object.hasOwn(RUN_NOW_REFUSALS, code)
    ? RUN_NOW_REFUSALS[code]
    : RUN_NOW_FALLBACK;
}

// Why a run failed, by its reason code; any other code reads "it failed".
const RUN_ERRORS: Record<string, string> = {
  erpnext_not_connected: "ERPNext isn't connected",
  read_refused: "ERPNext refused a read",
  read_timeout: "ERPNext didn't answer in time",
  read_failed: "a read couldn't be made",
  run_stalled: "it stopped before it finished",
};

const RUN_NOW_PREFIX = "now@";

/** When the last run happened: its day, then its slot or "on request". */
function whenLine(run: WatchRun): string {
  // The API gives a run on request as run_now with no slot; a raw
  // "now@HH:MM:SS" key, if one ever reaches here, is read the same way and
  // never shown as a schedule slot.
  const slot = String(run.slot ?? "");
  if (slot.startsWith(RUN_NOW_PREFIX)) {
    const at = slot.slice(RUN_NOW_PREFIX.length).slice(0, 5);
    return /^\d\d:\d\d$/.test(at) ? `${run.day}, run on request at ${at}` : `${run.day}, run on request`;
  }
  if (run.run_now) return `${run.day}, run on request`;
  return slot ? `${run.day} at ${slot}` : run.day;
}

/** The job's last run in one line, or that it hasn't run yet. */
export function lastRunLine(run: WatchRun | null): string {
  if (!run) return "Hasn't run yet.";
  const when = whenLine(run);
  if (run.status === "running") return `Running now (started ${when}).`;
  if (run.status === "failed") {
    const why = Object.hasOwn(RUN_ERRORS, run.error) ? RUN_ERRORS[run.error] : "it failed";
    return `Last run ${when}: didn't finish, ${why}.`;
  }
  if (run.status === "done") {
    const cards = run.cards === 1 ? "1 card" : `${run.cards} cards`;
    return `Last run ${when}: done, ${cards}.`;
  }
  return `Last run ${when}.`;
}

/** Whether to offer the button (the server decides either way). */
export function canRunNow(jobs: WatchJobs, job: WatchJob): boolean {
  return jobs.master_switch && job.enabled && job.last_run?.status !== "running";
}

// After a run starts, the card reads the jobs again this many ms apart, then
// stops: at most this many reads, never an open-ended interval.
export const REREAD_DELAYS_MS: readonly number[] = [3000, 6000, 12000, 24000, 45000];

/** Whether a re-read shows the asked-for run has finished (a new last run
 * that isn't running), so the re-reads can stop early. */
export function runSettled(before: WatchRun | null, after: WatchRun | null): boolean {
  if (!after || after.status === "running") return false;
  if (!before) return true;
  return after.day !== before.day || after.slot !== before.slot || after.status !== before.status
    || after.cards !== before.cards || after.run_now !== before.run_now;
}
