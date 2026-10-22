import { getToken } from "next-auth/jwt";

import { OIDC, signOut } from "@/auth";
import { isCrossSiteWrite } from "@/lib/crossSite";
import { discover } from "@/lib/oidcDiscovery";

// Signing out of this app also signs out at the identity provider, so it
// isn't left signed in for the next person at the computer (OpenID Connect
// RP-Initiated Logout; docs/auth.md, "Signing out"). The account menu posts
// here for an SSO session: this app's session ends, then the browser goes to
// the provider's end-session endpoint with the ID token as the hint, and comes
// back to /signin (register that as a post-logout redirect URI). A session the
// provider can't end this way (another provider, a provider without the
// endpoint) is only signed out here, as before.

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const COOKIE_NAMES: { secureCookie: boolean }[] = [{ secureCookie: true }, { secureCookie: false }];

/** The ID token the session kept at sign-in (in the encrypted JWT only). */
async function idTokenOf(req: Request): Promise<string | null> {
  const secret = process.env.AUTH_SECRET;
  if (!secret) return null;
  for (const { secureCookie } of COOKIE_NAMES) {
    const token = await getToken({ req, secret, secureCookie }).catch(() => null);
    if (token) return typeof token.idToken === "string" ? token.idToken : null;
  }
  return null;
}

export async function POST(req: Request): Promise<Response> {
  // Only this app's own pages sign someone out.
  if (isCrossSiteWrite("POST", req.headers.get("sec-fetch-site"))) {
    return new Response(JSON.stringify({ error: "cross-site request refused" }), {
      status: 403,
      headers: { "content-type": "application/json" },
    });
  }
  const back = new URL("/signin", req.url);
  const idToken = await idTokenOf(req);
  let endSession: string | null = null;
  if (OIDC && idToken) {
    try {
      endSession = (await discover(OIDC.issuer)).endSessionEndpoint;
    } catch {
      endSession = null;
    }
  }
  // This app's session ends either way (its cookie cleared, its audit row).
  await signOut({ redirect: false });
  if (!OIDC || !idToken || !endSession) return Response.redirect(back, 303);
  const target = new URL(endSession);
  target.searchParams.set("id_token_hint", idToken);
  target.searchParams.set("client_id", OIDC.clientId);
  target.searchParams.set("post_logout_redirect_uri", back.toString());
  return Response.redirect(target, 303);
}
