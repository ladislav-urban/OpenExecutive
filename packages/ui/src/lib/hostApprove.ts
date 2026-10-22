// Approving a card from a host's own screen (a host origin's native Home,
// lib/embed.ts): the host may not approve, so its Approve opens this app's
// window for that card, /confirm/alert/<id>. The window is a page of this
// app's own origin that no page may frame, it shows the card to whoever is
// signed in here, and only its own Approve, clicked there, approves. It then
// tells the host which card is done (`card-done`) and closes, so the person
// stays on the host's screen.
//
// A host opens the window as the person clicks, so it could open it right
// under their cursor and catch a second click. The Approve button arms only
// after the window has been in front for ARM_DELAY_MS, and disarms whenever
// it loses focus.
//
// No imports, so `npm test` can exercise this.

export const ARM_DELAY_MS = 700;

/** The alert id from the window's address, or null for anything else. */
export function alertIdFrom(param: string | undefined | null): number | null {
  if (!param || !/^[1-9]\d{0,9}$/.test(param)) return null;
  return Number(param);
}

/** Whether the card offers Approve to this person: the window approves
 *  nothing the card itself doesn't offer them (GET /alerts/{id}/card). */
export function offersApprove(actions: readonly { key: string }[] | null | undefined): boolean {
  return (actions ?? []).some((a) => a.key === "approve");
}

/** The line the window shows once approved. */
export function approvedLine(kind: "decision" | "alert", status?: string, outcome?: string): string {
  if (kind === "decision") return "Approved.";
  if (status === "failed") return outcome ? `It couldn't be done: ${outcome}` : "It couldn't be done.";
  return "Approved. The Executive is doing it in a new chat, which you'll find in Chats.";
}
