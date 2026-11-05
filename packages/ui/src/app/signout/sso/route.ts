import { randomUUID } from "node:crypto";

import { auth, signOut } from "@/auth";
import { recordEndedSession, endedSessions } from "@/lib/endedSessionsServer";
import { HOST_ORIGINS } from "@/lib/hostConfig";
import { signOutTarget } from "@/lib/signinReturn";

// A host origin's page signs its user out of this app too, on its way to its
// own sign-out (54.a, Nov 5, 2026). This app's session can outlive the
// identity provider's session it came from, and when the next person signs in
// to the host, this app takes no part in their provider session, so no
// back-channel logout reaches the old one:
//
//   GET /signout/sso?to=<host page>
//
// - This app's session, if any, ends: its cookie is cleared, and its provider
//   session (`sid`) goes on the ended-sessions list for every server
//   (lib/endedSessions.ts) with its person and the time, so it ends every
//   session of this app on that sid that began by now, here or wherever its
//   cookie went, and nothing begun later: the provider session may still be
//   live, and the person must be able to sign straight back in.
// - Then the tab goes to `to`, a page on one of the host origins
//   (signOutTarget), else this app's /signin.
//
// A cross-site GET: anyone could link here, and following the link signs the
// person out of this app. That is all it can do: it reads nothing, changes no
// data, and the person signs straight back in (the next /signin/sso). It
// answers only a top-level navigation (Sec-Fetch-Mode), so an image, a frame's
// subresource or a fetch can't sign anyone out unseen, and it sends the tab
// only to a listed host. Session-free (lib/middlewarePaths.ts): a tab with no
// session, or an ended one, still goes on to the host's sign-out.

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const mode = request.headers.get("sec-fetch-mode");
  if (mode && mode !== "navigate") {
    return new Response(JSON.stringify({ error: "a sign-out is a page the browser opens" }), {
      status: 403,
      headers: { "content-type": "application/json", "cache-control": "no-store" },
    });
  }
  const to =
    signOutTarget(new URL(request.url).searchParams.get("to"), HOST_ORIGINS.origins) ??
    new URL("/signin", process.env.AUTH_URL?.trim() || request.url).toString();

  const session = await auth().catch(() => null);
  if (session) {
    const provider = session.providerSession;
    if (provider?.sid && provider.sub) {
      const record = { iss: provider.iss, sid: provider.sid, sub: provider.sub, ended_at: Math.floor(Date.now() / 1000) };
      endedSessions.add(record);
      if ((await recordEndedSession({ ...record, jti: `oe-signout:${randomUUID()}` })) !== "recorded") {
        // This server knows; the others learn when the provider's own
        // logout comes. The cookie below goes either way.
        console.error("[signout-sso] the ended session couldn't be stored");
      }
    }
    await signOut({ redirect: false });
  }
  return new Response(null, {
    status: 303,
    headers: { location: to, "cache-control": "no-store" },
  });
}
