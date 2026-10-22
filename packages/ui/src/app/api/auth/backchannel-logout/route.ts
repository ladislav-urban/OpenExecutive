import { createRemoteJWKSet, type JWTVerifyGetKey } from "jose";

import { OIDC } from "@/auth";
import { createJtiGuard, verifyLogoutToken } from "@/lib/backchannel";
import { endedSessions, recordEndedSession } from "@/lib/endedSessionsServer";
import { discover } from "@/lib/oidcDiscovery";

// OpenID Connect Back-Channel Logout 1.0 (lib/backchannel.ts): the identity
// provider posts `logout_token` here when someone's session there ends, and
// their session in this app ends on its next request (lib/endedSessions.ts).
// Register this URL as the client's Back-Channel Logout URL, with "session
// required" on (docs/auth.md, "Signing out").
//
// The provider calls it server to server: no session, no cookie. The
// middleware lets `/api/auth/*` through for that; it is on no host list, and a
// browser has no reason to call it (a valid token is the provider's to sign).

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// A logout token is a few hundred bytes; a body far larger is refused before
// it is read.
const MAX_TOKEN_CHARS = 16_384;
const MAX_BODY_BYTES = 64 * 1024;

const seen = createJtiGuard();
let keys: JWTVerifyGetKey | null = null;

/** The issuer's signing keys, through its discovery document (fetched once;
 * the key set refreshes itself when the provider rotates its keys). */
async function issuerKeys(issuer: string): Promise<JWTVerifyGetKey> {
  if (keys) return keys;
  const { jwksUri } = await discover(issuer);
  if (!jwksUri) throw new Error("discovery: no usable jwks_uri");
  keys = createRemoteJWKSet(new URL(jwksUri));
  return keys;
}

const answer = (status: number, body?: Record<string, string>) =>
  new Response(body ? JSON.stringify(body) : null, {
    status,
    headers: { "cache-control": "no-store", ...(body ? { "content-type": "application/json" } : {}) },
  });

export async function POST(req: Request): Promise<Response> {
  if (!OIDC) return answer(404, { error: "not_found" });
  const length = Number(req.headers.get("content-length") ?? "");
  const type = (req.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
  if (!Number.isFinite(length) || length <= 0 || length > MAX_BODY_BYTES || type !== "application/x-www-form-urlencoded") {
    return answer(400, { error: "invalid_request" });
  }
  let token: unknown = null;
  try {
    token = (await req.formData()).get("logout_token");
  } catch {
    token = null;
  }
  if (typeof token !== "string" || !token || token.length > MAX_TOKEN_CHARS) {
    return answer(400, { error: "invalid_request" });
  }
  let issuerKeySet: JWTVerifyGetKey;
  try {
    issuerKeySet = await issuerKeys(OIDC.issuer);
  } catch (err) {
    console.error(`[backchannel-logout] ${err instanceof Error ? err.message : "the provider's keys can't be read"}`);
    return answer(400, { error: "invalid_request" });
  }
  const verdict = await verifyLogoutToken(token, {
    issuer: OIDC.issuer,
    clientId: OIDC.clientId,
    keys: issuerKeySet,
    now: Math.floor(Date.now() / 1000),
    seen,
  });
  if (!verdict.ok) return answer(400, { error: "invalid_request" });

  const { iss, sid, sub, endedAt, jti } = verdict.ended;
  const record = { iss, sid: sid ?? null, sub: sid ? null : (sub ?? null), ended_at: endedAt };
  // This server stops the session at once; the others read the store.
  endedSessions.add(record);
  const stored = await recordEndedSession({ ...record, jti });
  if (stored === "replayed") return answer(400, { error: "invalid_request" });
  if (stored === "failed") {
    // Only this server knows: tell the provider it didn't take (the spec's
    // 501), and give the jti back so its retry is taken.
    console.error("[backchannel-logout] the ended session couldn't be stored");
    seen.release(jti);
    return answer(501, { error: "logout_failed" });
  }
  return answer(200);
}
