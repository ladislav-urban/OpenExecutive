import { cookies, headers } from "next/headers";

import { OIDC, auth, sessionStillAllowed, signIn, signOut } from "@/auth";
import { HOST_ORIGINS } from "@/lib/hostConfig";
import { OIDC_PROVIDER_ID } from "@/lib/oidc";
import {
  RETURN_COOKIE,
  RETURN_COOKIE_PATH,
  failedTarget,
  hintOf,
  ownNavigation,
  returnTarget,
  signInPass,
} from "@/lib/signinReturn";

// A host origin's page signs in to this app and comes straight back, top
// level and without a popup (lib/signinReturn.ts; oe_connector's
// docs/single-sign-in-plan.md, step 2):
//
//   GET /signin/sso?to=<host page>[&hint=<email>]   (or login_hint=<email>)
//
// - `to` that isn't a host desk page (returnTarget) goes to this app's Home.
// - Signed in already as the host's user (or with no hint), with a session the
//   middleware still accepts: straight back to `to`.
// - Signed in as someone else (54.a, Nov 5, 2026: the session outlived the
//   provider session it came from, and the desk showed A's assistant beside
//   B's desk): that session is ended here, and the sign-in below runs. At most
//   once a minute for the same host user (REPLACE_COOKIE): a provider that
//   still answers as the other person sends the tab back to the host's page,
//   signed in as that person, rather than round again.
// - Otherwise the identity provider's sign-in starts at once (OIDC only), with
//   the host's user as `login_hint`. It returns through /signin/return, which
//   checks `to` again. A cookie remembers `to` for ten minutes, so that a
//   provider's refusal, which Auth.js sends to /signin, can come back here as
//   `?failed=1` and return to the host's page with `oe_signin=failed`.
// The steps after `to` is checked are signInPass (lib/signinReturn.ts).
//
// Session-free (lib/middlewarePaths.ts): it is how someone without a session
// gets one. It starts a sign-in for whoever is at the browser, and returns
// only to a listed host's own page.

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function home(request: Request): Response {
  return Response.redirect(new URL("/", process.env.AUTH_URL?.trim() || request.url));
}

export async function GET(request: Request): Promise<Response> {
  const params = new URL(request.url).searchParams;
  const jar = await cookies();

  if (params.get("failed") === "1") {
    const to = returnTarget(jar.get(RETURN_COOKIE)?.value, HOST_ORIGINS.origins);
    jar.delete({ name: RETURN_COOKIE, path: RETURN_COOKIE_PATH });
    return to ? Response.redirect(failedTarget(to)) : home(request);
  }

  const to = returnTarget(params.get("to"), HOST_ORIGINS.origins);
  if (!to || !OIDC) return home(request);

  const hint = hintOf(params);
  const session = await auth();
  const live = session && (await sessionStillAllowed(session, (await headers()).get("host"))) ? session : null;
  const straightBack = await signInPass({
    to,
    hint,
    session: live ? { email: live.user?.email } : null,
    ownTab: ownNavigation(request.headers),
    secure: new URL(request.url).protocol === "https:",
    cookies: jar,
    // The replace mark's key: the secret Auth.js signs sessions with (any of
    // the rotation slots will do for a mark that lives a minute).
    secret: [
      process.env.AUTH_SECRET,
      process.env.AUTH_SECRET_1,
      process.env.AUTH_SECRET_2,
      process.env.AUTH_SECRET_3,
      process.env.NEXTAUTH_SECRET,
    ]
      .map((s) => s?.trim())
      .find((s) => Boolean(s)),
    now: Date.now(),
    signOut: (back) => signOut({ redirect: false, redirectTo: back }),
    // Throws Next's redirect to the provider, with Auth.js's state and PKCE cookies.
    signIn: (back) => signIn(OIDC_PROVIDER_ID, { redirectTo: back }, hint ? { login_hint: hint } : undefined),
  });
  if (straightBack) return Response.redirect(straightBack);
  return home(request);
}
