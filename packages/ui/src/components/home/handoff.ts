// A chat hand-off from a page that isn't the chat home (a Home panel on its
// own page, /briefing/<panel>): Discuss, Nudge and the like write the turn
// here and open /chats/new, which sends it once, as Home's own hand-offs do.
// It is the person's own click on this app's page, so it may send without a
// second tap; it lives in sessionStorage (this tab only) and goes stale fast,
// so a later visit to /chats/new never replays it.

const KEY = "oe.chat.handoff";
const FRESH_MS = 60_000;

export interface Handoff {
  prompt: string;
  memoryText?: string;
}

export function leaveHandoff(handoff: Handoff): void {
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify({ ...handoff, at: Date.now() }));
  } catch {
    // No storage: the new chat just opens empty.
  }
}

/** The hand-off left for this tab, once; null when there is none or it is stale. */
export function takeHandoff(): Handoff | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (raw === null) return null;
    window.sessionStorage.removeItem(KEY);
    const parsed = JSON.parse(raw) as { prompt?: unknown; memoryText?: unknown; at?: unknown };
    if (typeof parsed.prompt !== "string" || !parsed.prompt) return null;
    if (typeof parsed.at !== "number" || Date.now() - parsed.at > FRESH_MS) return null;
    return {
      prompt: parsed.prompt,
      memoryText: typeof parsed.memoryText === "string" ? parsed.memoryText : undefined,
    };
  } catch {
    return null;
  }
}
