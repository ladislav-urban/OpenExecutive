// Whether a web chat turn goes out as typed (POST /chat's `typed`).
//
// The answer check lets a projection's worked-out figures stand only when
// the person typed the question (packages/core answer_check._projection).
// The composer is also filled by code: an Edit of the last message (a
// hand-off's card-built seed, for one), a stopped turn's text given back, a
// `?draft=` link, an accepted follow-up suggestion. Text code put there can
// carry an outsider's words ("Updated forecast for Q1"), so a send is typed
// only from the composer's own Send while it holds nothing code put there,
// and from leftovers every part of which was typed. Everything else (a
// hand-off, a starter prompt, Approve, Try again, an Edit) sends false.
//
// `fromCode` is Chat.tsx's composerFromCodeRef: set whenever code puts
// words in the composer, cleared only when the composer is emptied.

import type { QueuedMessage } from "./queuedMessages";

// After code put `text` in the composer: emptied, it holds nothing code put
// there; with words in it, it does.
export function afterCodeFill(text: string): boolean {
  return Boolean(text.trim());
}

// After the person changed the composer to `value` with an input event of
// `inputType`. The browser's own undo and redo can bring back words code
// put there, so they count as code; anything else keeps what was, until
// the composer is emptied.
export function afterInput(fromCode: boolean, value: string, inputType?: string): boolean {
  if (!value.trim()) return false;
  if (inputType === "historyUndo" || inputType === "historyRedo") return true;
  return fromCode;
}

// After a turn that ended with nothing saved gave text back to the
// composer: the turn's own text (`turnTyped`: that turn went out typed) and
// the messages queued while it worked. Code unless the turn and every part
// were typed.
export function afterRestore(turnTyped: boolean, restored: QueuedMessage[]): boolean {
  // As queuedMessages.allTyped (a type-only import keeps this file
  // loadable by the node tests on its own).
  return !(turnTyped && restored.every((m) => m.typed || !m.text.trim()));
}

export type SendKind =
  // The composer's own Send (Enter or the button), not while editing.
  | "composer"
  // What was typed while the last turn worked, sent as its own turn.
  | "leftovers"
  // A hand-off, a starter prompt, Approve, Try again, an Edit.
  | "code";

// Whether a send goes out as typed. `leftoversTyped`: every part of the
// leftovers was typed (SettledQueue.nextTyped).
export function sendTyped(kind: SendKind, fromCode: boolean, leftoversTyped = false): boolean {
  if (fromCode) return false;
  if (kind === "composer") return true;
  if (kind === "leftovers") return leftoversTyped;
  return false;
}
