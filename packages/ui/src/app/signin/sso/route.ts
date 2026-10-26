import { cookies, headers } from "next/headers";

import { OIDC, auth, sessionStillAllowed, signIn } from "@/auth";
import { HOST_ORIGINS } from "@/lib/hostConfig";
import { OIDC_PROVIDER_ID } from "@/lib/oidc";
import {
  RETURN_COOKIE,
  RETURN_COOKIE_MAX_AGE_S,
  RETURN_COOKIE_PATH,
  failedTarget,
  loginHint,
  returnTarget,
} from "@/lib/signinReturn";

// A host origin's page signs in to this app and comes straight back, top
// level and without a popup (lib/signinReturn.ts; oe_connector's
// docs/single-sign-in-plan.md, step 2):
//
//   GET /signin/sso?to=<host page>[&hint=<email>]
//
// - `to` that isn't a host desk page (returnTarget) goes to this app's Home.
// - Signed in already, with a session the middleware still accepts: straight
//   back to `to`.
// - Otherwise the identity provider's sign-in starts at once (OIDC only), with
//   the host's user as `login_hint`. It returns through /signin/return, which
//   checks `to` again. A cookie remembers `to` for ten minutes, so that a
//   provider's refusal, which Auth.js sends to /signin, can come back here as
//   `?failed=1` and return to the host's page with `oe_signin=failed`.
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

  const session = await auth();
  if (session && (await sessionStillAllowed(session, (await headers()).get("host")))) {
    return Response.redirect(to);
  }

  jar.set(RETURN_COOKIE, to, {
    httpOnly: true,
    sameSite: "lax",
    secure: new URL(request.url).protocol === "https:",
    path: RETURN_COOKIE_PATH,
    maxAge: RETURN_COOKIE_MAX_AGE_S,
  });
  const hint = loginHint(params.get("hint"));
  // Throws Next's redirect to the provider, with Auth.js's state and PKCE cookies.
  await signIn(
    OIDC_PROVIDER_ID,
    { redirectTo: `/signin/return?to=${encodeURIComponent(to)}` },
    hint ? { login_hint: hint } : undefined,
  );
  return home(request);
}
