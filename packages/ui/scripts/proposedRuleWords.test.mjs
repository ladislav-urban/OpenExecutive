// The proposed bank rule's line on Home (lib/proposedRuleWords.ts).
import assert from "node:assert/strict";
import test from "node:test";
import { proposedRuleLine } from "../src/lib/proposedRuleWords.ts";

const RULE = { word: "servicefee", account: "Bank Charges - AC", transaction_type: "Withdrawal", lines: 6, misses: 0 };

test("a proposed rule says which lines it tags, as what, on what evidence", () => {
  assert.equal(
    proposedRuleLine(RULE),
    "Tag money going out whose description contains “servicefee” as Bank Charges - AC. "
      + "Seen in 6 reconciled lines, none of them booked elsewhere.",
  );
  assert.match(proposedRuleLine({ ...RULE, transaction_type: "Deposit", lines: 1 }), /^Tag money coming in .* Seen in 1 reconciled line,/);
  assert.match(proposedRuleLine({ ...RULE, transaction_type: "Odd", misses: 2 }), /^Tag bank lines .*, 2 booked elsewhere\.$/);
});

test("the proposals card names no product and decides nothing here", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../src/components/ProposedRules.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(source, /dangerouslySetInnerHTML/);
  assert.doesNotMatch(source, /ERPNext|OpenExecutive|Open Executive|the Executive/);
  assert.doesNotMatch(source, /method: "POST"|fetch\(/);
});
