// OpenID Connect Back-Channel Logout 1.0: the provider tells this app that
// someone's session there ended, so theirs here ends too (docs/auth.md,
// "Signing out"). Its own routes: app/api/auth/backchannel-logout/route.ts
// takes the provider's `logout_token`, and lib/endedSessions.ts checks each
// request against what has ended.
//
// The token is checked as the spec says (section 2.6) and anything else is
// refused: the signature, against the issuer's keys; `iss`; `aud` holding
// this app's client id; a recent `iat`; the back-channel logout event; a
// `sid` or a `sub`; no `nonce`; a `jti` not seen before. Tested in
// scripts/backchannel.test.mjs.

import { jwtVerify, type JWTVerifyGetKey } from "jose";

export const BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout";

// How old a logout token may be, and how far ahead of this clock its `iat`
// may run (the provider's clock may be a little ahead).
export const MAX_TOKEN_AGE_S = 120;
export const CLOCK_SKEW_S = 60;

/** A provider session that ended: by its `sid`, or every session of `sub`
 * that began before `endedAt` (seconds since the epoch). */
export interface EndedSession {
  iss: string;
  sid?: string;
  sub?: string;
  endedAt: number;
  jti: string;
}

export type LogoutVerdict = { ok: true; ended: EndedSession } | { ok: false; reason: string };

export interface LogoutCheck {
  issuer: string;
  clientId: string;
  keys: JWTVerifyGetKey;
  /** Seconds since the epoch. */
  now: number;
  /** Whether this `jti` was taken already (and, if not, take it). */
  seen: (jti: string) => boolean;
}

const nonEmpty = (value: unknown): value is string => typeof value === "string" && value.length > 0;

export async function verifyLogoutToken(token: string, check: LogoutCheck): Promise<LogoutVerdict> {
  let payload: Record<string, unknown>;
  try {
    ({ payload } = await jwtVerify(token, check.keys, {
      issuer: check.issuer,
      audience: check.clientId,
      // jwtVerify checks `exp` when present; `iat` is ours to bound.
      currentDate: new Date(check.now * 1000),
      clockTolerance: CLOCK_SKEW_S,
      // Asymmetric signatures only: never "none", never a shared secret.
      algorithms: ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"],
    }));
  } catch {
    return { ok: false, reason: "invalid_token" };
  }
  const iat = payload.iat;
  if (typeof iat !== "number" || iat > check.now + CLOCK_SKEW_S || iat < check.now - MAX_TOKEN_AGE_S) {
    return { ok: false, reason: "stale_token" };
  }
  const events = payload.events;
  if (
    typeof events !== "object" ||
    events === null ||
    Array.isArray(events) ||
    !(BACKCHANNEL_LOGOUT_EVENT in events) ||
    typeof (events as Record<string, unknown>)[BACKCHANNEL_LOGOUT_EVENT] !== "object"
  ) {
    return { ok: false, reason: "not_a_logout_token" };
  }
  if ("nonce" in payload) return { ok: false, reason: "nonce_present" };
  const sid = nonEmpty(payload.sid) ? payload.sid : undefined;
  const sub = nonEmpty(payload.sub) ? payload.sub : undefined;
  if (!sid && !sub) return { ok: false, reason: "no_session" };
  const jti = payload.jti;
  if (!nonEmpty(jti)) return { ok: false, reason: "no_jti" };
  if (check.seen(jti)) return { ok: false, reason: "replayed" };
  return { ok: true, ended: { iss: check.issuer, sid, sub, endedAt: iat, jti } };
}

/** A replay guard for `jti`s, each kept as long as its token could still be
 * accepted. Per server; the backend's store refuses a repeat across servers.
 * `release` gives one back (its logout couldn't be recorded, so a retry must
 * be taken). */
export function createJtiGuard(nowS: () => number = () => Math.floor(Date.now() / 1000)) {
  const seen = new Map<string, number>();
  const guard = (jti: string): boolean => {
    const now = nowS();
    for (const [key, at] of seen) if (at < now - MAX_TOKEN_AGE_S - CLOCK_SKEW_S) seen.delete(key);
    if (seen.has(jti)) return true;
    seen.set(jti, now);
    return false;
  };
  guard.release = (jti: string): void => {
    seen.delete(jti);
  };
  return guard;
}
