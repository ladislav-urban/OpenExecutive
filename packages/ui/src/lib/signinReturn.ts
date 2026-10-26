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
  return `${url.origin}${route}`;
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
