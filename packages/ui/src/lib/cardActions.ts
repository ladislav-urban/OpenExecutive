// Approving, rejecting or cancelling a card asks twice in a host's frame
// (embedded mode, lib/embed.ts). The host's page can't read or click into
// the frame, but it can lay something misleading over it and catch a click
// meant for something else; so in a frame every card action first shows a
// confirmation inside the frame that names what will happen, and only a
// second click there goes ahead. Outside a frame nothing changes.
//
// The embedded shell (components/embed/EmbedProvider.tsx) checks every
// request the page sends against `cardActionFor`, so no call site can forget
// to ask: whichever component approves a card, by whatever function in
// lib/api.ts. scripts/cardActions.test.mjs fails when lib/api.ts calls a card
// action this doesn't recognise.
//
// No imports, so `npm test` can exercise this.

export type CardAction = {
  verb: "approve" | "reject" | "cancel";
  // The card's decision id, when it has one (its details are shown too).
  decisionId: number | null;
  // What will happen, in a sentence.
  what: string;
};

const BACKEND = "/api/backend";

function verbFromBody(body: unknown): "approve" | "reject" | null {
  if (typeof body !== "string") return null;
  try {
    const parsed = JSON.parse(body) as { decision?: unknown };
    return parsed.decision === "approve" || parsed.decision === "reject" ? parsed.decision : null;
  } catch {
    return null;
  }
}

/**
 * The card action a request is, or null for anything else. `url` is what
 * the page passed to fetch (a path, or a URL on this origin); `body` its
 * body when it is text.
 */
export function cardActionFor(method: string, url: string, body?: unknown): CardAction | null {
  if (method.toUpperCase() !== "POST") return null;
  let path: string;
  try {
    path = new URL(url, "http://this.app").pathname;
  } catch {
    return null;
  }
  if (!path.startsWith(`${BACKEND}/`)) return null;
  path = path.slice(BACKEND.length);

  let m = /^\/decisions\/(\d+)\/(approve|reject|cancel)$/.exec(path);
  if (m) {
    const id = Number(m[1]);
    const verb = m[2] as CardAction["verb"];
    const what =
      verb === "approve"
        ? `Approve card ${id}: what it shows is carried out now, as you.`
        : verb === "reject"
          ? `Dismiss card ${id}: nothing on it will happen.`
          : `Cancel card ${id}: what it started stops, and nothing more on it will happen.`;
    return { verb, decisionId: id, what };
  }
  m = /^\/alerts\/(\d+)\/approve$/.exec(path);
  if (m) {
    return {
      verb: "approve",
      decisionId: null,
      what: `Approve the proposal on alert ${m[1]}: what it proposes is carried out now, as you.`,
    };
  }
  m = /^\/workflows\/runs\/([^/]+)\/decision$/.exec(path);
  if (m) {
    const verb = verbFromBody(body) ?? "approve";
    const run = decodeURIComponent(m[1]);
    return {
      verb,
      decisionId: null,
      what:
        verb === "approve"
          ? `Sign off workflow run ${run}: it carries on with what it was waiting to do.`
          : `Turn down workflow run ${run}: it stops where it is waiting.`,
    };
  }
  m = /^\/people\/requests\/(\d+)\/(approve|decline)$/.exec(path);
  if (m) {
    return m[2] === "approve"
      ? { verb: "approve", decisionId: null, what: `Add the person in request ${m[1]} to People, as you answered it.` }
      : { verb: "reject", decisionId: null, what: `Decline request ${m[1]}: they are not added to People.` };
  }
  return null;
}

/** A line naming the card, from its decision (GET /decisions/{id}): its kind and what it says. */
export function describeDecision(instance: { decision_class?: unknown; proposed_payload_json?: unknown }): string {
  const kind = typeof instance.decision_class === "string" ? instance.decision_class.replace(/[_-]+/g, " ") : "card";
  let detail = "";
  if (typeof instance.proposed_payload_json === "string") {
    try {
      const payload = JSON.parse(instance.proposed_payload_json) as Record<string, unknown>;
      for (const key of ["summary", "title", "subject", "why", "card"]) {
        const value = payload[key];
        if (typeof value === "string" && value.trim()) {
          detail = value.trim().replace(/\s+/g, " ").slice(0, 200);
          break;
        }
      }
    } catch {
      // No detail: the kind alone.
    }
  }
  return detail ? `${kind}: ${detail}` : kind;
}

export class CardActionNotConfirmed extends Error {
  constructor() {
    super("Not confirmed, so nothing was done.");
    this.name = "CardActionNotConfirmed";
  }
}
