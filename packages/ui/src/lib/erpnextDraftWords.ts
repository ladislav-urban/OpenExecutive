// What an ERPNext draft card and its chip say (components/ErpnextDraftCards.tsx,
// components/Message.tsx).
//
// Most DocTypes a card prepares (Material Request, Quotation, Purchase Order)
// are made unsubmitted, for the person to review and submit in ERPNext. A
// ToDo can't be submitted: it is simply an open task, so asking them to
// submit it would send them looking for a button that isn't there.
//
// What the person reads calls ERPNext "the ERP", never by the name of the
// part it is built from, with or without a product name (OE_PRODUCT_NAME)
// or a host's frame. The `_named` arguments are kept for the callers that
// pass them.
//
// No imports, so `npm test` can exercise this.

// DocTypes ERPNext doesn't submit, of those a card may prepare
// (ERPNEXT_DRAFT_DOCTYPES, integrations/erpnext_drafts.py).
const NOT_SUBMITTED = new Set(["ToDo"]);

/** What the person is told ERPNext is called. */
export function erpName(_named: boolean): string {
  return "the ERP";
}

/** The line an approved card shows: what is in the ERP now, and what is left to do there. */
export function draftMadeLine(doctype: string, name: string | null, alreadyCreated: boolean, _named = false): string {
  const now = alreadyCreated ? "was already" : "is now";
  const erp = erpName(true);
  if (NOT_SUBMITTED.has(doctype)) {
    return name
      ? `${doctype} ${name} ${now} in ${erp}, open. It's closed there once it's done.`
      : `The ${doctype} is in ${erp}, open. It's closed there once it's done.`;
  }
  return name
    ? `Draft ${name} ${now} in ${erp}, unsubmitted. Review and submit it there.`
    : `The draft is in ${erp}, unsubmitted. Review and submit it there.`;
}

/** The line an approved change or comment shows. */
export function changeMadeLine(operation: string, doctype: string, name: string, already: boolean): string {
  if (operation === "comment") {
    return already ? `Your comment was already on ${doctype} ${name}.` : `Your comment is now on ${doctype} ${name}.`;
  }
  return already
    ? `${doctype} ${name} already had this change. It is still unsubmitted.`
    : `${doctype} ${name} is changed in the ERP, still unsubmitted.`;
}

/** The card's kind, over its headline: a new draft, a change or a comment. */
export function cardKind(doctype: string, operation: string | undefined, named: boolean): string {
  if (operation === "update") return `Change to a ${doctype}`;
  if (operation === "comment") return `Comment on a ${doctype}`;
  return draftKind(doctype, named);
}

/** The card's kind, over its headline. */
export function draftKind(doctype: string, _named: boolean): string {
  return `${doctype} draft`;
}

/** The warning on a card asked for outside this app's own chat. */
export function askedInNote(askedIn: "erpnext_panel" | "erpnext_desk", _named: boolean): string {
  if (askedIn === "erpnext_desk") {
    return "Asked for from a page in the ERP. Approve it only if you asked for it there.";
  }
  return "Asked for in the ERP's Ask panel. Approve it only if you asked for it there.";
}

/** The link to the draft once made. */
export function openDraftLabel(doctype: string, _named: boolean): string {
  return `Open the ${doctype}`;
}

/** What the draft cards are, for the Needs you header's tip. */
export function draftsTip(_named: boolean): string {
  return (
    "Drafts you asked the Executive to prepare. Each shows exactly what will be " +
    "written, and the ERP has already checked it. Nothing is created until you tap Approve, and then only an " +
    "unsubmitted draft, as your own ERP user, for you to review and submit in the ERP. " +
    "Only you see these."
  );
}

// What a card became, as its chip says it.
export type CardOutcome = "approved" | "dismissed" | "closed";

const OUTCOME_LEAD: Record<CardOutcome, string> = {
  approved: "Approved",
  dismissed: "Dismissed",
  closed: "Closed, nothing made",
};

const WAITING = "Waiting for your approval";

/** A chip's summary once its card is no longer waiting: "Waiting for your
 * approval: Quotation draft" becomes "Approved: Quotation draft". Any other
 * summary is left as it is. */
export function chipSummary(summary: string, outcome: CardOutcome | undefined): string {
  if (!outcome || !summary.startsWith(WAITING)) return summary;
  const rest = summary.slice(WAITING.length).replace(/^:\s*/, "");
  return rest ? `${OUTCOME_LEAD[outcome]}: ${rest}` : OUTCOME_LEAD[outcome];
}
