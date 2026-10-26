// What an approved ERPNext draft card says it made (lib/erpnextDraftWords.ts):
// a ToDo is an open task, never "submit it"; the rest are drafts to submit.
import assert from "node:assert/strict";
import test from "node:test";
import { draftMadeLine } from "../src/lib/erpnextDraftWords.ts";

test("a ToDo is open, with nothing to submit", () => {
  const line = draftMadeLine("ToDo", "7c23hmgiiq", false);
  assert.equal(line, "ToDo 7c23hmgiiq is now in ERPNext, open. It's closed there once it's done.");
  assert.doesNotMatch(line, /submit/i);
  assert.match(draftMadeLine("ToDo", "x", true), /^ToDo x was already in ERPNext, open\./);
  assert.doesNotMatch(draftMadeLine("ToDo", null, false), /submit/i);
});

test("the documents ERPNext submits are drafts to review and submit", () => {
  for (const doctype of ["Material Request", "Quotation", "Purchase Order"]) {
    assert.equal(
      draftMadeLine(doctype, "MAT-MR-1", false),
      "Draft MAT-MR-1 is now in ERPNext, unsubmitted. Review and submit it there.",
    );
  }
  assert.match(draftMadeLine("Quotation", "Q-1", true), /^Draft Q-1 was already in ERPNext, unsubmitted\./);
  assert.equal(draftMadeLine("Purchase Order", null, false), "The draft is in ERPNext, unsubmitted. Review and submit it there.");
});
