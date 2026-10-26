// What an approved ERPNext draft card says it made (components/ErpnextDraftCards.tsx).
//
// Most DocTypes a card prepares (Material Request, Quotation, Purchase Order)
// are made unsubmitted, for the person to review and submit in ERPNext. A
// ToDo can't be submitted: it is simply an open task, so asking them to
// submit it would send them looking for a button that isn't there.
//
// No imports, so `npm test` can exercise this.

// DocTypes ERPNext doesn't submit, of those a card may prepare
// (ERPNEXT_DRAFT_DOCTYPES, integrations/erpnext_drafts.py).
const NOT_SUBMITTED = new Set(["ToDo"]);

/** The line an approved card shows: what is in ERPNext now, and what is left to do there. */
export function draftMadeLine(doctype: string, name: string | null, alreadyCreated: boolean): string {
  const now = alreadyCreated ? "was already" : "is now";
  if (NOT_SUBMITTED.has(doctype)) {
    return name
      ? `${doctype} ${name} ${now} in ERPNext, open. It's closed there once it's done.`
      : `The ${doctype} is in ERPNext, open. It's closed there once it's done.`;
  }
  return name
    ? `Draft ${name} ${now} in ERPNext, unsubmitted. Review and submit it there.`
    : "The draft is in ERPNext, unsubmitted. Review and submit it there.";
}
