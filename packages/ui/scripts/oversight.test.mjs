// The audit log's oversight record download (lib/oversight.ts): the days
// chosen, both included, become the route's [since, until) range, with
// ERPNext's state asked for, and a bad range downloads nothing.
import assert from "node:assert/strict";
import test from "node:test";
import { oversightCsvUrl, oversightFilename } from "../src/lib/oversight.ts";

const BASE = "/api/backend/erpnext/drafts/oversight?";

test("both days are included: until is the day after the last", () => {
  assert.equal(
    oversightCsvUrl("2026-10-01", "2026-10-31"),
    `${BASE}since=2026-10-01&until=2026-11-01&erpnext=1&format=csv`,
  );
  // Across a year's end and a leap day.
  assert.equal(oversightCsvUrl("", "2026-12-31"), `${BASE}until=2027-01-01&erpnext=1&format=csv`);
  assert.equal(oversightCsvUrl("2028-02-29", "2028-02-29"), `${BASE}since=2028-02-29&until=2028-03-01&erpnext=1&format=csv`);
});

test("an open end is left out, and with none the whole record is asked for", () => {
  assert.equal(oversightCsvUrl("2026-10-01", ""), `${BASE}since=2026-10-01&erpnext=1&format=csv`);
  assert.equal(oversightCsvUrl("", ""), `${BASE}erpnext=1&format=csv`);
});

test("a range that ends before it starts, or a malformed date, gives no URL", () => {
  assert.equal(oversightCsvUrl("2026-10-31", "2026-10-01"), null);
  assert.equal(oversightCsvUrl("2026-10-01T00:00", ""), null);
  assert.equal(oversightCsvUrl("", "../x"), null);
});

test("the route's filename is kept when it is a plain CSV name", () => {
  assert.equal(
    oversightFilename('attachment; filename="erpnext-draft-oversight-20261103.csv"'),
    "erpnext-draft-oversight-20261103.csv",
  );
  const today = new Date("2026-11-03T10:00:00Z");
  assert.equal(oversightFilename(null, today), "erpnext-draft-oversight-20261103.csv");
  assert.equal(oversightFilename('attachment; filename="../evil.sh"', today), "erpnext-draft-oversight-20261103.csv");
});
