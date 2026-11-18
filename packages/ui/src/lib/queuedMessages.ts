// Messages typed while the Executive is still working on a turn. Each one is
// handed to the running turn (POST /chat/add), which takes it at its next step
// and says so with a `message_added` event. When the turn ends, the ones it
// took are shown as part of that exchange, and the rest are sent as the next
// turn, so nothing is lost and nothing is answered twice.

export interface QueuedMessage {
  id: string;
  text: string;
  // The running turn took it into its answer.
  taken: boolean;
  // The person typed it: the composer held nothing code put there when it
  // was queued (lib/composerOrigin.ts). It travels with the text, so a
  // leftover or a restore of it counts as typed only if every part was.
  typed: boolean;
}

export function markTaken(queued: QueuedMessage[], ids: string[]): QueuedMessage[] {
  if (ids.length === 0) return queued;
  const set = new Set(ids);
  return queued.map((m) => (set.has(m.id) ? { ...m, taken: true } : m));
}

export interface SettledQueue {
  // Shown as the user's own bubbles, after the turn's message and before the
  // reply that answered them.
  taken: string[];
  // Sent together as the next turn; null when there is nothing left.
  next: string | null;
  // Every part of `next` was typed (QueuedMessage.typed).
  nextTyped: boolean;
}

// How a turn that produced a reply settles its queue.
export function settleQueue(queued: QueuedMessage[]): SettledQueue {
  const left = queued.filter((m) => !m.taken);
  return {
    taken: queued.filter((m) => m.taken).map((m) => m.text),
    next: left.length ? left.map((m) => m.text).join("\n\n") : null,
    nextTyped: left.length > 0 && allTyped(left),
  };
}

// A turn that ended with nothing saved (stopped before any output, or
// failed) keeps none of it: everything goes back into the composer, after
// whatever is being returned there already.
export function composerText(first: string, queued: QueuedMessage[]): string {
  return [first, ...queued.map((m) => m.text)].filter((t) => t.trim()).join("\n\n");
}

// Whether every queued message with words in it was typed by the person.
export function allTyped(queued: QueuedMessage[]): boolean {
  return queued.every((m) => m.typed || !m.text.trim());
}
