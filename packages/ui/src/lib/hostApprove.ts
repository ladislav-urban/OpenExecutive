// Approving a card from a host's own screen (a host origin's native Home,
// lib/embed.ts): the host may not approve, so where its Approve would be it
// places a small frame of this app's own, /embed/approve/<id>. The frame
// shows the card's own words to whoever is signed in here, and only its own
// Approve, clicked there, approves; it then tells the host which card is
// done (`card-done`).
//
// Any host page could place the frame, so it offers Approve only to a host
// that asks: it announces itself to its parent (`approve-ready`, addressed
// to each listed host origin) and offers Approve only once that parent
// answers `approve-ask` for this card from a listed host origin. With no
// host origin listed it never does, and links to the card's own page.
//
// The host places the frame and could hide it, cover it or catch a click
// meant for something else on it, so its Approve counts only once the
// browser has reported it seen (lib/seen.ts). A browser that can't report
// that links to the card's own page instead, top level in the same tab.
//
// No imports, so `npm test` can exercise this.

/** The alert id from the page's address, or null for anything else. */
export function alertIdFrom(param: string | undefined | null): number | null {
  if (!param || !/^[1-9]\d{0,9}$/.test(param)) return null;
  return Number(param);
}

/** Whether the card offers Approve to this person: it approves
 *  nothing the card itself doesn't offer them (GET /alerts/{id}/card). */
export function offersApprove(actions: readonly { key: string }[] | null | undefined): boolean {
  return (actions ?? []).some((a) => a.key === "approve");
}

/** The line shown once approved. */
export function approvedLine(kind: "decision" | "alert", status?: string, outcome?: string): string {
  if (kind === "decision") return "Approved.";
  if (status === "failed") return outcome ? `It couldn't be done: ${outcome}` : "It couldn't be done.";
  return "Approved. The Executive is doing it in a new chat, which you'll find in Conversations.";
}
