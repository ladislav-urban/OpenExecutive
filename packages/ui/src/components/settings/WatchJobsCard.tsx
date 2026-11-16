"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import { getWatchJobs, runWatchJobNow } from "@/lib/api";
import {
  REREAD_DELAYS_MS,
  canRunNow,
  lastRunLine,
  runNowRefusal,
  runSettled,
  type WatchJob,
  type WatchJobs,
} from "@/lib/watchJobs";

// Settings → <product>: the watch jobs (watch/registry.py), each with its
// last run and a "Run once now" button (POST /watch/jobs/{id}/run), so a fix
// in ERPNext needn't wait for the job's next slot. The account owner only:
// GET /watch/jobs refuses anyone else, and the card then shows nothing. The
// server checks every run whatever this card offers.
export default function WatchJobsCard() {
  const [jobs, setJobs] = useState<WatchJobs | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const alive = useRef(true);

  // The first read decides whether the card shows; a later read that fails
  // keeps what's shown rather than hiding the card mid-run.
  const load = useCallback(async (signal?: AbortSignal, keep = false) => {
    const next = await getWatchJobs(signal).catch(() => null);
    if (alive.current && !signal?.aborted && (next || !keep)) setJobs(next);
    return next;
  }, []);

  useEffect(() => {
    alive.current = true;
    const ctrl = new AbortController();
    void load(ctrl.signal);
    return () => {
      alive.current = false;
      ctrl.abort();
      timers.current.forEach(clearTimeout);
      timers.current = [];
    };
  }, [load]);

  // After a run starts: a few reads, further apart each time, then stop.
  const reread = (job: WatchJob, step = 0) => {
    if (step >= REREAD_DELAYS_MS.length) return;
    const timer = setTimeout(async () => {
      if (!alive.current) return;
      const next = await load(undefined, true);
      if (!alive.current) return;
      const now = next?.jobs.find((j) => j.id === job.id);
      if (now && runSettled(job.last_run, now.last_run)) return;
      reread(job, step + 1);
    }, REREAD_DELAYS_MS[step]);
    timers.current.push(timer);
  };

  if (!jobs || jobs.jobs.length === 0) return null;

  const run = async (job: WatchJob) => {
    if (busy) return;
    setBusy(job.id);
    setNotes((n) => ({ ...n, [job.id]: "" }));
    try {
      const refused = await runWatchJobNow(job.id);
      if (!alive.current) return;
      setNotes((n) => ({
        ...n,
        [job.id]: refused === null ? "Started. Its result shows here when it's done." : runNowRefusal(refused),
      }));
      if (refused === null) reread(job);
    } catch {
      if (alive.current) setNotes((n) => ({ ...n, [job.id]: runNowRefusal("") }));
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  // Own keys only, so an id like "constructor" never reads Object's.
  const noteFor = (id: string) => (Object.hasOwn(notes, id) ? notes[id] : "");

  return (
    <SettingsCard
      title="Watch jobs"
      description="Unattended checks of ERPNext, each run on its own schedule. Run one once now to see a fix in ERPNext without waiting for its next run. Up to 3 times a day each."
    >
      {!jobs.master_switch && (
        <p className="mb-3 text-sm text-fg-muted">Watch jobs are off on this server, so none of them runs.</p>
      )}
      <ul className="flex flex-col divide-y divide-line">
        {jobs.jobs.map((job) => (
          <li key={job.id} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <p className="text-[15px] font-semibold text-fg">
                {job.title}{" "}
                <span className="font-normal text-fg-muted">· {job.enabled ? `on, runs at ${job.runs_at}` : "off"}</span>
              </p>
              <p className="mt-0.5 text-sm text-fg-muted">{lastRunLine(job.last_run)}</p>
              {noteFor(job.id) && (
                <p role="status" className="mt-1 text-sm text-fg">
                  {noteFor(job.id)}
                </p>
              )}
            </div>
            <button
              type="button"
              onClick={() => void run(job)}
              disabled={busy !== null || !canRunNow(jobs, job)}
              className="flex-shrink-0 rounded-lg border border-line px-3 py-1.5 text-sm font-medium text-fg hover:bg-surface disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy === job.id ? "Starting…" : "Run once now"}
            </button>
          </li>
        ))}
      </ul>
    </SettingsCard>
  );
}
