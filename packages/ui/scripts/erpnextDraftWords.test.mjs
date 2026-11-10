// What an approved ERPNext draft card says it made (lib/erpnextDraftWords.ts):
// a ToDo is an open task, never "submit it"; the rest are drafts to submit.
import assert from "node:assert/strict";
import test from "node:test";
import { draftMadeLine } from "../src/lib/erpnextDraftWords.ts";

test("a ToDo is open, with nothing to submit", () => {
  const line = draftMadeLine("ToDo", "7c23hmgiiq", false);
  assert.equal(line, "ToDo 7c23hmgiiq is now in the ERP, open. It's closed there once it's done.");
  assert.doesNotMatch(line, /submit/i);
  assert.match(draftMadeLine("ToDo", "x", true), /^ToDo x was already in the ERP, open\./);
  assert.doesNotMatch(draftMadeLine("ToDo", null, false), /submit/i);
});

test("the documents ERPNext submits are drafts to review and submit", () => {
  for (const doctype of ["Material Request", "Quotation", "Purchase Order"]) {
    assert.equal(
      draftMadeLine(doctype, "MAT-MR-1", false),
      "Draft MAT-MR-1 is now in the ERP, unsubmitted. Review and submit it there.",
    );
  }
  assert.match(draftMadeLine("Quotation", "Q-1", true), /^Draft Q-1 was already in the ERP, unsubmitted\./);
  assert.equal(draftMadeLine("Purchase Order", null, false), "The draft is in the ERP, unsubmitted. Review and submit it there.");
});

test("every line of the card calls ERPNext the ERP, named or not", async () => {
  const words = await import("../src/lib/erpnextDraftWords.ts");
  assert.equal(
    words.draftMadeLine("Quotation", "Q-1", false, true),
    "Draft Q-1 is now in the ERP, unsubmitted. Review and submit it there.",
  );
  assert.equal(words.draftKind("Quotation", true), "Quotation draft");
  assert.equal(words.draftKind("Quotation", false), "Quotation draft");
  assert.equal(words.openDraftLabel("Quotation", false), "Open the Quotation");
  assert.equal(words.openDraftLabel("Quotation", true), "Open the Quotation");
  for (const where of ["erpnext_panel", "erpnext_desk"]) {
    assert.doesNotMatch(words.askedInNote(where, true), /ERPNext/);
    assert.doesNotMatch(words.askedInNote(where, false), /ERPNext/);
  }
  assert.doesNotMatch(words.draftsTip(true), /ERPNext/);
  assert.doesNotMatch(words.draftsTip(false), /ERPNext/);
});

test("a chip follows its card's outcome", async () => {
  const { chipSummary } = await import("../src/lib/erpnextDraftWords.ts");
  const waiting = "Waiting for your approval: Quotation draft";
  assert.equal(chipSummary(waiting, undefined), waiting);
  assert.equal(chipSummary(waiting, "approved"), "Approved: Quotation draft");
  assert.equal(chipSummary(waiting, "dismissed"), "Dismissed: Quotation draft");
  assert.equal(chipSummary(waiting, "closed"), "Closed, nothing made: Quotation draft");
  assert.equal(chipSummary("Waiting for your approval", "approved"), "Approved");
  assert.equal(chipSummary("Booked: Lunch", "approved"), "Booked: Lunch");
});

test("a change or a comment says what it did, never 'submit the new draft'", async () => {
  const words = await import("../src/lib/erpnextDraftWords.ts");
  assert.equal(words.cardKind("Purchase Invoice", "update", true), "Change to a Purchase Invoice");
  assert.equal(words.cardKind("Purchase Invoice", "comment", true), "Comment on a Purchase Invoice");
  assert.equal(words.cardKind("Quotation", undefined, true), "Quotation draft");
  assert.equal(words.changeMadeLine("update", "Purchase Invoice", "PINV-1", false),
    "Purchase Invoice PINV-1 is changed in the ERP, still unsubmitted.");
  assert.equal(words.changeMadeLine("comment", "Purchase Invoice", "PINV-1", false),
    "Your comment is now on Purchase Invoice PINV-1.");
  assert.doesNotMatch(words.changeMadeLine("update", "Purchase Invoice", "PINV-1", true), /submit it/);
});

test("a credit-limit raise card says what it raises and that the order is still a draft", async () => {
  const words = await import("../src/lib/erpnextDraftWords.ts");
  assert.equal(words.cardKind("Customer", "credit", true), "Raise a credit limit");
  assert.equal(words.cardKind("Purchase Invoice", "block", true), "Hold a submitted Purchase Invoice");
  assert.equal(words.jobCardKind("Purchase Invoice", "block", "Payment checks"),
    "Hold a submitted Purchase Invoice from the Payment checks job");
  assert.match(words.changeMadeLine("block", "Purchase Invoice", "PINV-2", false), /on hold in the ERP, until someone unblocks it/);
  assert.doesNotMatch(words.changeMadeLine("block", "Purchase Invoice", "PINV-2", false), /unsubmitted|submit it/);
  assert.equal(words.jobCardKind("Bank Transaction", "match", "Bank matching"),
    "Match a bank line from the Bank matching job");
  assert.match(words.changeMadeLine("match", "Bank Transaction", "BT-1", false), /^Bank line BT-1 is matched in the ERP/);
  assert.doesNotMatch(words.changeMadeLine("match", "Bank Transaction", "BT-1", false), /unsubmitted|submit it/);
  assert.equal(words.changeMadeLine("match", "Bank Transaction", "BT-1", true), "Bank line BT-1 was already matched in the ERP.");
  assert.equal(words.cardKind("Customer", "credit", false), "Raise a credit limit");
  assert.equal(
    words.creditMadeLine("Harbor Electric Co-op", "Aurora Components", "31,000.00 USD", false),
    "Harbor Electric Co-op's credit limit at Aurora Components is now 31,000.00 USD. " +
      "The order is still a draft for its owner to submit.",
  );
  assert.match(words.creditMadeLine("H", "A", "1.00 USD", true), /^H's credit limit at A was already 1\.00 USD\./);
  assert.doesNotMatch(words.CREDIT_CHANGED_NOTE, /ERPNext/);
});

test("a watch job's card names its job and labels its reason line", async () => {
  const words = await import("../src/lib/erpnextDraftWords.ts");
  assert.equal(words.jobCardKind("Dunning", "create", "Collections"), "Dunning draft from the Collections job");
  assert.equal(words.whyNowLine("Usually pays in 5 days; 30 days late now."),
    "Why now: Usually pays in 5 days; 30 days late now.");
});
