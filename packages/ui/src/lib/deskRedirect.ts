// Sending a direct visit to the same screen in the host's desk
// (docs/desk-redirect-plan.md). With OE_DESK_BASE set, a top-level document
// GET of one of this app's pages is sent to <desk base><path>?<allowed query>,
// the desk route that frames that page. The framed page, Next's own fetches,
// a browser that sends no fetch metadata, the pages that stand alone, and a
// link whose query holds something that must not leave this app are left as
// they are.
//
// It isn't an access control: every page keeps its own session check, and any
// page is still reachable top level (`standalone=1`). It only changes where an
// ordinary visit lands.
//
// No imports, so `npm test` can exercise it (scripts/deskRedirect.test.mjs).

/**
 * The paths that stand alone: reached before someone has a desk session, or
 * not to be run inside the desk. Anchored like SESSION_FREE_PATH: whole
 * segments or exact paths, so `/signin-help` or `/cardsX` are redirected.
 * - `/api/*`: Auth.js's routes and its error page, and the backend proxy;
 * - `/signin`, `/signin/done`, `/signin/return`, `/signin/sso`, `/signout/sso`:
 *   signing in and out, top level;
 * - `/cards/*`: a card's own page, answered top level with the way back;
 * - `/embed/*`: pieces placed on the desk's own screens;
 * - `/chats/shared/*`: a shared chat, opened by someone who may have no desk.
 * Every page and route handler under src/app is either here or in
 * REDIRECTED_ROUTES (scripts/deskRedirect.test.mjs).
 */
export const STANDALONE_PATH =
  /^\/(?:api(?:\/|$)|signin(?:\/(?:done|return|sso))?\/?$|signout\/sso\/?$|cards(?:\/|$)|embed(?:\/|$)|chats\/shared(?:\/|$))/;

/** The query flag that keeps a page top level: read here and nowhere else. */
export const STANDALONE_PARAM = "standalone";

/**
 * The query keys forwarded to the desk: screen state only, matched exactly.
 * Every other key is dropped. scripts/deskRedirect.test.mjs checks that every
 * key a page reads is here or held back.
 */
export const FORWARDED_KEYS: ReadonlySet<string> = new Set([
  "add",
  "browse",
  "designer",
  "edit",
  "event_type",
  "find",
  "for",
  "mode",
  "new",
  "q",
  "saved",
  "section",
  "session",
  "status",
  "tab",
  "view",
]);

/**
 * Keys that keep a link here: a secret, a one-time value, a way back, or
 * someone's own words. A link carrying one isn't redirected at all.
 */
export const HELD_BACK_KEYS: ReadonlySet<string> = new Set([
  "callbackurl",
  "describe",
  "draft",
  "hint",
  "login_hint",
  "oe_signin",
  "prefill",
  "return",
  "to",
]);

/** A key off FORWARDED_KEYS whose name holds one of these is held back too. */
export const SECRET_KEY_RE = /token|code|state|key|sig|secret|password|otp|nonce|ticket|auth/i;

// Each forwarded value is short: `q` and `find` are free text, and they reach
// the desk's logs.
export const MAX_VALUE_CHARS = 200;
const MAX_PATH_CHARS = 512;
const MAX_QUERY_CHARS = 1024;

const PAGE_PATH_RE = /^\/(?:[A-Za-z0-9_.~%-]+(?:\/[A-Za-z0-9_.~%-]+)*)?\/?$/;
const ENCODED_SEPARATOR_RE = /%(?:2e|2f|5c)/i;
// A file this app serves at its root (`/icon.svg`, `/robots.txt`): never a screen.
const FILE_RE = /\/[^/]*\.[A-Za-z0-9]{1,8}$/;

/** OE_DESK_BASE checked: a return base (lib/embed.ts) on one of the host origins, or null. */
export function parseDeskBase(
  value: string | undefined,
  hostOrigins: readonly string[],
  parseReturnBase: (value: unknown) => string | null,
): { base: string | null; error: string | null } {
  if (value === undefined || value.trim() === "") return { base: null, error: null };
  const base = parseReturnBase(value.trim());
  const url = base ? new URL(base) : null;
  // A page of the desk, not the desk's whole route space (`/app` alone would
  // send `/user/new` here to the desk's own `/app/user/new`); https, except on
  // this machine.
  const local = url !== null && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (!base || !url || !/^\/(?:desk|app)\/[^/]+/.test(url.pathname) || (url.protocol !== "https:" && !local)) {
    return { base: null, error: "OE_DESK_BASE isn't a desk page's route (https://host/desk/<page>)" };
  }
  if (!hostOrigins.includes(url.origin)) {
    return { base: null, error: "OE_DESK_BASE's origin isn't one of OE_HOST_ORIGINS" };
  }
  return { base, error: null };
}

export interface VisitHeaders {
  get(name: string): string | null;
}

/**
 * Whether the request is a top-level document load the person asked for:
 * fetch metadata present and saying so, and no prefetch or prerender. A
 * browser that sends none isn't redirected.
 */
export function topLevelVisit(headers: VisitHeaders): boolean {
  if (headers.get("sec-fetch-dest") !== "document" || headers.get("sec-fetch-mode") !== "navigate") return false;
  const purpose = `${headers.get("sec-purpose") ?? ""} ${headers.get("purpose") ?? ""}`.toLowerCase();
  return !purpose.includes("prefetch") && !purpose.includes("prerender");
}

/** Whether `key` keeps the link here. */
export function heldBack(key: string): boolean {
  if (FORWARDED_KEYS.has(key)) return false;
  return HELD_BACK_KEYS.has(key.toLowerCase()) || SECRET_KEY_RE.test(key);
}

/**
 * Where a request goes in the desk, or null when it stays here. `pathname`
 * and `search` are the request's own (`search` with or without its `?`).
 */
export function deskTarget(
  request: { method: string; pathname: string; search: string; headers: VisitHeaders },
  base: string | null,
): string | null {
  if (!base) return null;
  if (request.method !== "GET" && request.method !== "HEAD") return null;
  if (!topLevelVisit(request.headers)) return null;
  const path = request.pathname;
  if (path.length > MAX_PATH_CHARS || !PAGE_PATH_RE.test(path)) return null;
  if (path.includes("//") || /(?:^|\/)\.\.?(?:\/|$)/.test(path) || ENCODED_SEPARATOR_RE.test(path)) return null;
  // Decoded once and case-folded before the stand-alone test, so `/sign%69n`
  // or `/Cards/7` stays here like the page it may be. A path that still holds
  // an escape after that, or decodes to a query, fragment, NUL or backslash,
  // isn't plain: it stays here too.
  let decoded: string;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return null;
  }
  if (/[%?#\\\0]/.test(decoded) || decoded.includes("//")) return null;
  const folded = decoded.toLowerCase();
  if (STANDALONE_PATH.test(path) || STANDALONE_PATH.test(folded) || FILE_RE.test(decoded)) return null;
  const search = request.search.startsWith("?") ? request.search.slice(1) : request.search;
  if (search.length > MAX_QUERY_CHARS) return null;
  const params = new URLSearchParams(search);
  const kept = new URLSearchParams();
  for (const [key, value] of params) {
    if (key === STANDALONE_PARAM) return null;
    if (heldBack(key)) return null;
    if (!FORWARDED_KEYS.has(key)) continue;
    if (value.length > MAX_VALUE_CHARS) return null;
    kept.append(key, value);
  }
  const trimmed = path.length > 1 ? path.replace(/\/$/, "") : path;
  const query = kept.toString();
  return `${base}${trimmed === "/" ? "" : trimmed}${query ? `?${query}` : ""}`;
}
