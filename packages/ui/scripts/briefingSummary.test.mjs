import assert from "node:assert/strict";
import test from "node:test";
import {
  ageLabel,
  briefingSummary,
  daysUntil,
  greeting,
  homeSummary,
  proposalStatusChip,
} from "../src/lib/briefingSummary.ts";

// Local-time constructors, so the labels hold in any test-runner time zone.
const at = (h) => new Date(2026, 9, 3, h, 30);

test("greeting follows the time of day and drops a missing name", () => {
  assert.equal(greeting("Jordan", at(8)), "Good morning, Jordan");
  assert.equal(greeting("Jordan", at(13)), "Good afternoon, Jordan");
  assert.equal(greeting("Jordan", at(19)), "Good evening, Jordan");
  assert.equal(greeting(undefined, at(8)), "Good morning");
  assert.equal(greeting("  ", at(8)), "Good morning");
});

// The summary line's cases moved with it to the API: tests/unit/test_home_rules.py.

test("ageLabel and daysUntil", () => {
  const now = new Date(2026, 9, 3, 12, 0);
  assert.equal(ageLabel(new Date(2026, 9, 3, 11, 30).toISOString(), now), "30m");
  assert.equal(ageLabel(new Date(2026, 9, 2, 12, 0).toISOString(), now), "24h");
  assert.equal(ageLabel(new Date(2026, 8, 24, 12, 0).toISOString(), now), "9d");
  assert.equal(ageLabel("nonsense", now), "");
  assert.equal(daysUntil(new Date(2026, 9, 3, 1, 0).toISOString(), now), -1);
  assert.equal(daysUntil(new Date(2026, 9, 3, 20, 0).toISOString(), now), 0);
  assert.equal(daysUntil(null, now), null);
});

test("a collapsed card keeps one status chip: deadline, then stale, then age", () => {
  const now = new Date(2026, 9, 3, 12, 0);
  const created = new Date(2026, 9, 1, 12, 0).toISOString();
  assert.deepEqual(
    proposalStatusChip({ created_at: created, due_at: new Date(2026, 9, 2, 12, 0).toISOString() }, now),
    { label: "Overdue", tone: "rose" },
  );
  assert.deepEqual(
    proposalStatusChip({ created_at: created, due_at: new Date(2026, 9, 6, 13, 0).toISOString() }, now),
    { label: "Due in 3d", tone: "amber" },
  );
  assert.deepEqual(
    proposalStatusChip({ created_at: created, review_verdict: "likely_stale" }, now),
    { label: "Likely stale", tone: "amber" },
  );
  assert.deepEqual(proposalStatusChip({ created_at: created }, now), { label: "2d old", tone: "neutral" });
  assert.equal(proposalStatusChip({ created_at: "" }, now), null);
});

test("Home's line counts only its two lists", () => {
  assert.deepEqual(homeSummary(0, 0), []);
  assert.deepEqual(
    homeSummary(2, 3).map((p) => [p.text, p.target.kind]),
    [["2 things need you", "needsYou"], ["I did 3 things for you today", "doneForYou"]],
  );
  assert.deepEqual(homeSummary(1, 1).map((p) => p.text), ["1 thing needs you", "I did 1 thing for you today"]);
});
