import assert from "node:assert/strict";
import test from "node:test";
import { SignJWT, createLocalJWKSet, exportJWK, generateKeyPair } from "jose";

import {
  BACKCHANNEL_LOGOUT_EVENT,
  MAX_TOKEN_AGE_S,
  createJtiGuard,
  verifyLogoutToken,
} from "../src/lib/backchannel.ts";

// OpenID Connect Back-Channel Logout 1.0, section 2.6: what a logout token
// must be, and that anything else is refused.

const ISSUER = "https://sso.example.com/realms/company";
const CLIENT = "openexecutive";
const NOW = 1_800_000_000;

const provider = await generateKeyPair("RS256");
const stranger = await generateKeyPair("RS256");
const jwk = { ...(await exportJWK(provider.publicKey)), kid: "k1", alg: "RS256" };
const keys = createLocalJWKSet({ keys: [jwk] });

let counter = 0;
async function token(claims = {}, { key = provider.privateKey, alg = "RS256", omit = [] } = {}) {
  const body = {
    iss: ISSUER,
    aud: CLIENT,
    iat: NOW,
    jti: `jti-${++counter}`,
    sid: "session-1",
    sub: "user-1",
    events: { [BACKCHANNEL_LOGOUT_EVENT]: {} },
    ...claims,
  };
  for (const name of omit) delete body[name];
  return new SignJWT(body).setProtectedHeader({ alg, kid: "k1", typ: "logout+jwt" }).sign(key);
}

const check = (overrides = {}) => ({
  issuer: ISSUER,
  clientId: CLIENT,
  keys,
  now: NOW,
  seen: createJtiGuard(() => NOW),
  ...overrides,
});

test("a good logout token ends its session", async () => {
  const verdict = await verifyLogoutToken(await token(), check());
  assert.equal(verdict.ok, true);
  assert.deepEqual(verdict.ended, { iss: ISSUER, sid: "session-1", sub: "user-1", endedAt: NOW, jti: `jti-${counter}` });
});

test("one with only a sub ends every session of that person", async () => {
  const verdict = await verifyLogoutToken(await token({}, { omit: ["sid"] }), check());
  assert.equal(verdict.ok, true);
  assert.equal(verdict.ended.sid, undefined);
  assert.equal(verdict.ended.sub, "user-1");
});

test("a token signed by anyone else is refused", async () => {
  const verdict = await verifyLogoutToken(await token({}, { key: stranger.privateKey }), check());
  assert.deepEqual(verdict, { ok: false, reason: "invalid_token" });
});

test("the wrong audience or issuer is refused", async () => {
  assert.equal((await verifyLogoutToken(await token({ aud: "someone-else" }), check())).ok, false);
  assert.equal((await verifyLogoutToken(await token({ iss: "https://evil.example.com" }), check())).ok, false);
});

test("a token without the logout event, or with a nonce, is refused", async () => {
  assert.deepEqual(await verifyLogoutToken(await token({}, { omit: ["events"] }), check()), {
    ok: false,
    reason: "not_a_logout_token",
  });
  assert.deepEqual(await verifyLogoutToken(await token({ events: { "http://other/event": {} } }), check()), {
    ok: false,
    reason: "not_a_logout_token",
  });
  assert.deepEqual(await verifyLogoutToken(await token({ nonce: "n" }), check()), { ok: false, reason: "nonce_present" });
});

test("an old token, or one from the future, is refused", async () => {
  assert.deepEqual(await verifyLogoutToken(await token({ iat: NOW - MAX_TOKEN_AGE_S - 1 }), check()), {
    ok: false,
    reason: "stale_token",
  });
  assert.deepEqual(await verifyLogoutToken(await token({ iat: NOW + 3600 }), check()), { ok: false, reason: "stale_token" });
});

test("a replayed token is refused, and a refused one never takes its jti", async () => {
  const seen = createJtiGuard(() => NOW);
  const good = await token({ jti: "once" });
  assert.equal((await verifyLogoutToken(good, check({ seen }))).ok, true);
  assert.deepEqual(await verifyLogoutToken(good, check({ seen })), { ok: false, reason: "replayed" });
  const bad = await token({ jti: "twice", nonce: "n" });
  assert.equal((await verifyLogoutToken(bad, check({ seen }))).ok, false);
  assert.equal((await verifyLogoutToken(await token({ jti: "twice" }), check({ seen }))).ok, true);
});

test("a token naming no session, or with no jti, is refused", async () => {
  assert.deepEqual(await verifyLogoutToken(await token({}, { omit: ["sid", "sub"] }), check()), {
    ok: false,
    reason: "no_session",
  });
  assert.deepEqual(await verifyLogoutToken(await token({}, { omit: ["jti"] }), check()), { ok: false, reason: "no_jti" });
});

test("a shared-secret or unsigned token is never taken", async () => {
  const secret = new TextEncoder().encode("x".repeat(32));
  const hs = await new SignJWT({ iss: ISSUER, aud: CLIENT, iat: NOW, jti: "hs", sid: "s", events: { [BACKCHANNEL_LOGOUT_EVENT]: {} } })
    .setProtectedHeader({ alg: "HS256" })
    .sign(secret);
  assert.equal((await verifyLogoutToken(hs, check())).ok, false);
  const [, payload] = hs.split(".");
  const none = `${Buffer.from(JSON.stringify({ alg: "none" })).toString("base64url")}.${payload}.`;
  assert.equal((await verifyLogoutToken(none, check())).ok, false);
});

test("a jti given back (its logout wasn't recorded) is taken on the retry", async () => {
  const seen = createJtiGuard(() => NOW);
  const t = await token({ jti: "retry" });
  assert.equal((await verifyLogoutToken(t, check({ seen }))).ok, true);
  seen.release("retry");
  assert.equal((await verifyLogoutToken(t, check({ seen }))).ok, true);
});
