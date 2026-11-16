import assert from "node:assert/strict";
import test from "node:test";
import {
  REREAD_DELAYS_MS,
  RUN_NOW_FALLBACK,
  RUN_NOW_REFUSALS,
  canRunNow,
  lastRunLine,
  runNowRefusal,
  runSettled,
} from "../src/lib/watchJobs.ts";

const JOB = { id: "collections", title: "Collections", enabled: true, runs_at: "07:00", card_cap_day: 5, last_run: null };
const ON = { master_switch: true, jobs: [JOB] };
const RUN = { day: "2026-11-16", slot: "", status: "done", cards: 2, error: "" };

test("every code the run-now route answers has its own words", () => {
  for (const code of [
    "job_off", "paused", "held", "already_running", "run_now_cap",
    "unknown_job", "run_now_unavailable", "not_yours", "no_principal",
  ]) {
    assert.equal(runNowRefusal(code), RUN_NOW_REFUSALS[code], code);
  }
  assert.match(runNowRefusal("job_off"), /Turn the job on first/);
  assert.match(runNowRefusal("already_running"), /running now/);
  assert.match(runNowRefusal("run_now_cap"), /3 times today/);
});

test("an unknown, missing or inherited code gets the fixed fallback, never the server's text", () => {
  for (const code of ["<b>Acme Ltd</b> isn't yours", "", null, undefined, 42, "toString", "__proto__"]) {
    assert.equal(runNowRefusal(code), RUN_NOW_FALLBACK, String(code));
  }
});

test("a run on request reads as such, with its time, never as a schedule slot", () => {
  assert.equal(lastRunLine({ ...RUN, slot: "now@14:05:09" }), "Last run 2026-11-16, run on request at 14:05: done, 2 notices.");
  assert.equal(lastRunLine({ ...RUN, run_now: true }), "Last run 2026-11-16, run on request: done, 2 notices.");
  assert.doesNotMatch(lastRunLine({ ...RUN, slot: "now@14:05:09" }), /now@|at now/);
});

test("a run says its cards to approve and its notices apart", () => {
  assert.equal(lastRunLine({ ...RUN, cards: 1, draft_cards: 5 }), "Last run 2026-11-16: done, 5 cards to approve, 1 notice.");
  assert.equal(lastRunLine({ ...RUN, cards: 0, draft_cards: 1 }), "Last run 2026-11-16: done, 1 card to approve.");
  assert.equal(lastRunLine({ ...RUN, cards: 0, draft_cards: 0 }), "Last run 2026-11-16: done, nothing new.");
});

test("a scheduled run names its slot, and a once-a-day run its day", () => {
  assert.equal(lastRunLine({ ...RUN, slot: "09:00", cards: 1 }), "Last run 2026-11-16 at 09:00: done, 1 notice.");
  assert.equal(lastRunLine(RUN), "Last run 2026-11-16: done, 2 notices.");
  assert.equal(lastRunLine(null), "Hasn't run yet.");
  assert.equal(lastRunLine({ ...RUN, status: "running" }), "Running now (started 2026-11-16).");
});

test("a failed run's reason comes from a known code, else plain 'it failed'", () => {
  assert.equal(lastRunLine({ ...RUN, status: "failed", error: "read_timeout" }), "Last run 2026-11-16: didn't finish, ERPNext didn't answer in time.");
  const odd = lastRunLine({ ...RUN, status: "failed", error: "Customer 'Acme' not found" });
  assert.equal(odd, "Last run 2026-11-16: didn't finish, it failed.");
  assert.equal(lastRunLine({ ...RUN, status: "failed", error: "constructor" }), "Last run 2026-11-16: didn't finish, it failed.");
});

test("the button is offered only for a job that's on, not running, under the master switch", () => {
  assert.equal(canRunNow(ON, JOB), true);
  assert.equal(canRunNow({ ...ON, master_switch: false }, JOB), false);
  assert.equal(canRunNow(ON, { ...JOB, enabled: false }), false);
  assert.equal(canRunNow(ON, { ...JOB, last_run: { ...RUN, status: "running" } }), false);
});

test("the re-reads after a start are few and bounded", () => {
  assert.ok(REREAD_DELAYS_MS.length <= 5);
  for (let i = 1; i < REREAD_DELAYS_MS.length; i++) assert.ok(REREAD_DELAYS_MS[i] > REREAD_DELAYS_MS[i - 1]);
});

test("the re-reads stop once a new finished run shows", () => {
  assert.equal(runSettled(null, null), false);
  assert.equal(runSettled(null, { ...RUN, status: "running" }), false);
  assert.equal(runSettled(null, RUN), true);
  assert.equal(runSettled(RUN, RUN), false);
  assert.equal(runSettled({ ...RUN, slot: "09:00" }, { ...RUN, run_now: true }), true);
});

test("a missing slot reads as a once-a-day run, never a crash", () => {
  assert.equal(lastRunLine({ ...RUN, slot: null }), "Last run 2026-11-16: done, 2 notices.");
});
