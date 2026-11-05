// Signing in from a host's page and returning to it in one pass
// (app/signin/sso, app/signin/return; oe_connector's docs/single-sign-in-plan.md,
// step 2). A host origin's page (OE_HOST_ORIGINS) sends the tab to
// /signin/sso?to=<its own page>; this app signs in through the identity
// provider and sends the tab back there, and nowhere else.
//
// `to` is the only thing a page chooses, so it is checked here, on the way in
// and again on the way back: an absolute URL on exactly one of the host
// origins, with no user info, whose path is a host desk route as the host
// itself takes them (lib/erpnextLinks.ts's DESK_ROUTE_RE, oe_connector's
// oe_embed.bundle.js). Anything else returns null and the tab goes to this
// app's Home.
//
// The same check guards the way back from a card answered on this app's own
// page, top level (components/embed/FrameConfirm.tsx, the card pages): the
// host's return base (lib/embed.ts) plus that page's path, carried in
// `?return=`, offered as a link and never followed on its own.
//
// No imports, so `npm test` can exercise this.

// The host's desk routes, kept in step with lib/erpnextLinks.ts (its test reads both).
const DESK_ROUTE_RE = /^\/(?:desk|app)\/[A-Za-z0-9_\-./%]*(?:\?[^#\s]*)?$/;

// The cookie /signin/sso leaves for a sign-in that fails: the provider's error
// lands on /signin, which then sends the tab back to `to` saying so.
export const RETURN_COOKIE = "oe_signin_return";
export const RETURN_COOKIE_MAX_AGE_S = 600;
// Sent only to /signin and below: the sign-in page and these two routes.
export const RETURN_COOKIE_PATH = "/signin";

// What the host's page finds on its own address when signing in failed.
export const FAILED_PARAM = "oe_signin";
export const FAILED_VALUE = "failed";

// An encoded dot, slash or backslash in a path: kept in step with lib/erpnextLinks.ts.
const ENCODED_SEPARATOR_RE = /%(?:2e|2f|5c)/i;

const MAX_TO_CHARS = 2048;
const MAX_HINT_CHARS = 320;
const EMAIL_RE = /^[^\s@<>"']+@[^\s@<>"']+$/;

/** The host page to return to, rebuilt from its parts, or null when `to` isn't one. */
export function returnTarget(to: string | null | undefined, origins: readonly string[]): string | null {
  if (typeof to !== "string" || !to || to.length > MAX_TO_CHARS) return null;
  let url: URL;
  try {
    url = new URL(to);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  if (url.username || url.password || !origins.includes(url.origin)) return null;
  const route = `${url.pathname}${url.search}`;
  if (!DESK_ROUTE_RE.test(route) || route.includes("..") || route.includes("//")) return null;
  // The parser leaves `%2e`, `%2f` and `%5c` encoded, so `/desk/%2e%2e%2flogin`
  // would pass the checks above and reach the host's `/login`. Desk routes
  // never need them.
  if (ENCODED_SEPARATOR_RE.test(url.pathname)) return null;
  return `${url.origin}${route}`;
}

/**
 * Where /signout/sso sends the tab: any page on one of the host origins (its
 * own sign-out, which goes on to the identity provider's), rebuilt from its
 * parts, or null. Not only desk routes, but never user info, an encoded
 * separator, a backslash, `..` or `//`.
 */
export function signOutTarget(to: string | null | undefined, origins: readonly string[]): string | null {
  if (typeof to !== "string" || !to || to.length > MAX_TO_CHARS || to.includes("\\")) return null;
  let url: URL;
  try {
    url = new URL(to);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  if (url.username || url.password || !origins.includes(url.origin)) return null;
  const route = `${url.pathname}${url.search}`;
  if (!route.startsWith("/") || route.includes("..") || route.includes("//")) return null;
  if (ENCODED_SEPARATOR_RE.test(url.pathname)) return null;
  return `${url.origin}${route}`;
}

/** The desk user /signin/sso was asked for: `hint`, or OpenID's `login_hint`. */
export function hintOf(params: URLSearchParams): string | undefined {
  return loginHint(params.get("hint") ?? params.get("login_hint"));
}

/** Whether a session for `email` belongs to someone other than the desk user. */
export function otherPerson(email: string | null | undefined, hint: string | undefined): boolean {
  return Boolean(hint) && (typeof email !== "string" || email.trim().toLowerCase() !== hint);
}

/** `to` with the failed mark added, for the host's page to show why. */
export function failedTarget(to: string): string {
  const url = new URL(to);
  url.searchParams.set(FAILED_PARAM, FAILED_VALUE);
  return url.toString();
}

/** The provider's login_hint: a plain email address, lowercased, or undefined. */
export function loginHint(raw: string | null | undefined): string | undefined {
  if (typeof raw !== "string") return undefined;
  const hint = raw.trim().toLowerCase();
  return hint.length <= MAX_HINT_CHARS && EMAIL_RE.test(hint) ? hint : undefined;
}

// A card page's way back to the host page it was opened from.
export const RETURN_PARAM = "return";

/** The host page for this app's `page` (its path only), or null when it isn't one. */
export function hostReturn(base: string | null | undefined, page: string, origins: readonly string[]): string | null {
  if (!base) return null;
  const path = page.split(/[?#]/, 1)[0];
  if (!path.startsWith("/") || path.startsWith("//")) return null;
  return returnTarget(`${base}${path === "/" ? "" : path}`, origins);
}

/** `page` (a path on this app, maybe with a query) with the way back added. */
export function withReturn(page: string, to: string | null): string {
  if (!to) return page;
  const [path, query = ""] = page.split("?", 2);
  const params = new URLSearchParams(query);
  params.set(RETURN_PARAM, to);
  return `${path}?${params.toString()}`;
}

/** The way back a page's query carries, checked again, or null. */
export function returnFromQuery(search: string, origins: readonly string[]): string | null {
  return returnTarget(new URLSearchParams(search).get(RETURN_PARAM), origins);
}
