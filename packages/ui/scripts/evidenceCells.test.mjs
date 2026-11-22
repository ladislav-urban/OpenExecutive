import assert from "node:assert/strict";
import test from "node:test";

import { evidenceCellText } from "../src/lib/evidenceCells.ts";

test("money is in the table's currency, or a bare number without one", () => {
  assert.equal(evidenceCellText("money", 4200, "USD", "en-US"), "$4,200.00");
  assert.equal(evidenceCellText("money", -800, "USD", "en-US"), "-$800.00");
  assert.equal(evidenceCellText("money", 1500.5, null, "en-US"), "1,500.5");
});

test("a percent cell is already in points, never multiplied", () => {
  assert.equal(evidenceCellText("percent", 12.5, "USD", "en-US"), "12.5%");
  assert.equal(evidenceCellText("percent", -6, null, "en-US"), "-6%");
});

test("a date is its own day in the reader's locale", () => {
  assert.equal(evidenceCellText("date", "2026-10-02", null, "en-US"), "Oct 2, 2026");
});

test("a doc cell shows its DocType and name as text", () => {
  assert.equal(
    evidenceCellText("doc", { doctype: "Purchase Invoice", name: "ACC-PINV-2026-00041" }, null, "en-US"),
    "Purchase Invoice ACC-PINV-2026-00041",
  );
});

test("text stays text, and an empty or mistyped cell is a dash", () => {
  assert.equal(evidenceCellText("text", "<b>Cable & Wire</b>", null, "en-US"), "<b>Cable & Wire</b>");
  assert.equal(evidenceCellText("money", null, "USD", "en-US"), "–");
  assert.equal(evidenceCellText("money", "4,200", "USD", "en-US"), "–");
  assert.equal(evidenceCellText("doc", "ACC-1", null, "en-US"), "–");
});
