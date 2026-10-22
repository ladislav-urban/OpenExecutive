// Addresses of the chat home: a conversation lives at /chats/<session_id> and
// a new chat at /chats/new (with an optional ?draft= to pre-fill it), so a
// host that shows Home (`/`) natively can still open a conversation in its
// frame. Home's panels each have a page of their own at /briefing/<panel>.
//
// The old addresses, /?session=<id> and /?new=1, redirect here, so earlier
// links and bookmarks keep working.
//
// No imports, so `npm test` can exercise this (scripts/chatRoutes.test.mjs).

export const NEW_CHAT_PATH = "/chats/new";

// Words to find in a chat opened from a search, and a new chat's draft, are
// bounded: a link carries a hint, not a document.
const MAX_FIND_CHARS = 200;
export const MAX_DRAFT_CHARS = 2000;

/** The address of one conversation, with the words to find in it. */
export function chatPath(sessionId: string, find?: string): string {
  const q = find ? `?find=${encodeURIComponent(find.slice(0, MAX_FIND_CHARS))}` : "";
  return `/chats/${encodeURIComponent(sessionId)}${q}`;
}

/** A new chat, its input pre-filled with `draft` (never sent on its own). */
export function newChatPath(draft?: string): string {
  return draft ? `${NEW_CHAT_PATH}?draft=${encodeURIComponent(draft.slice(0, MAX_DRAFT_CHARS))}` : NEW_CHAT_PATH;
}

/** Where an old Home address (/?session=… or /?new=1) goes now, or null. */
export function legacyHomeRedirect(search: string): string | null {
  const params = new URLSearchParams(search);
  const embed = params.get("embed") === "1";
  let target: string | null = null;
  if (params.get("new") === "1") {
    target = newChatPath(params.get("draft") ?? undefined);
  } else {
    const session = params.get("session");
    if (session) target = chatPath(session, params.get("find") ?? undefined);
  }
  if (target === null) return null;
  // A host's frame keeps its embedded mode across the move.
  return embed ? `${target}${target.includes("?") ? "&" : "?"}embed=1` : target;
}

/**
 * What a chat home address shows: Home, a new chat, or one conversation.
 * Null for any other path, including /chats (the list) and /chats/shared/….
 */
export function chatHomeView(
  pathname: string,
): { kind: "briefing" } | { kind: "new" } | { kind: "chat"; sessionId: string } | null {
  if (pathname === "/") return { kind: "briefing" };
  if (pathname === NEW_CHAT_PATH) return { kind: "new" };
  const match = /^\/chats\/([^/]+)$/.exec(pathname);
  if (!match || match[1] === "shared") return null;
  try {
    const sessionId = decodeURIComponent(match[1]);
    return sessionId ? { kind: "chat", sessionId } : null;
  } catch {
    return null;
  }
}

/** Whether `pathname` is the chat home, which owns its whole layout. */
export function isChatHomePath(pathname: string): boolean {
  return chatHomeView(pathname) !== null;
}

// Home's panels, each a page of its own (/briefing/<panel>): the count tiles'
// panels and "What's going on" (the brief).
export const BRIEFING_PANELS = [
  "brief",
  "departments",
  "people",
  "team",
  "inFlight",
  "handled",
  "monitoring",
  "clients",
] as const;
export type BriefingPanel = (typeof BRIEFING_PANELS)[number];

export function isBriefingPanel(value: string): value is BriefingPanel {
  return (BRIEFING_PANELS as readonly string[]).includes(value);
}

export function briefingPanelPath(panel: BriefingPanel): string {
  return `/briefing/${panel}`;
}
