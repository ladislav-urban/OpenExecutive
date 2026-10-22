import assert from "node:assert/strict";
import test from "node:test";

import { createEndedSessionsStore, sessionEnded } from "../src/lib/endedSessions.ts";
import { serverOnlyPath } from "../src/lib/proxySegments.ts";

const ISS = "https://sso.example.com/realms/company";
const session = { iss: ISS, sid: "s1", sub: "u1", signedInAt: 1000 };

test("a session ends by its sid", () => {
  assert.equal(sessionEnded(session, [{ iss: ISS, sid: "s1", ended_at: 500 }]), true);
  assert.equal(sessionEnded(session, [{ iss: ISS, sid: "s2", ended_at: 5000 }]), false);
  // Another provider's sid is another session.
  assert.equal(sessionEnded(session, [{ iss: "https://other.example.com", sid: "s1", ended_at: 5000 }]), false);
});

test("a person's sessions end by sub, only those begun before", () => {
  assert.equal(sessionEnded(session, [{ iss: ISS, sub: "u1", ended_at: 2000 }]), true);
  // Signed in again after the provider ended them: that new session stands.
  assert.equal(sessionEnded(session, [{ iss: ISS, sub: "u1", ended_at: 900 }]), false);
  assert.equal(sessionEnded(session, [{ iss: ISS, sub: "u2", ended_at: 2000 }]), false);
});

test("a session with no sid (another sign-in) is untouched by a sid", () => {
  assert.equal(sessionEnded({ iss: ISS, sid: null, sub: "u1", signedInAt: 1000 }, [{ iss: ISS, sid: "s1", ended_at: 5000 }]), false);
});

test("the store reads the API at most every refresh, and keeps its copy when it can't", async () => {
  let now = 0;
  let calls = 0;
  let answer = [{ iss: ISS, sid: "s1", ended_at: 500 }];
  let failing = false;
  const store = createEndedSessionsStore({
    load: async () => {
      calls++;
      if (failing) throw new Error("down");
      return answer;
    },
    refreshMs: 5000,
    nowMs: () => now,
  });
  assert.equal(await store.isEnded(session), true);
  assert.equal(await store.isEnded(session), true);
  assert.equal(calls, 1);
  now = 6000;
  failing = true;
  answer = [];
  // Unreachable: the session the provider ended stays ended.
  assert.equal(await store.isEnded(session), true);
  assert.equal(calls, 2);
});

test("one ended here counts at once, before the next read", async () => {
  const store = createEndedSessionsStore({ load: async () => [], refreshMs: 60_000, nowMs: () => 0 });
  assert.equal(await store.isEnded(session), false);
  store.add({ iss: ISS, sid: "s1", ended_at: 500 });
  assert.equal(await store.isEnded(session), true);
});

test("the proxy never forwards the store's own paths", () => {
  assert.equal(serverOnlyPath(["auth", "ended-sessions"]), true);
  assert.equal(serverOnlyPath(["auth", "ended-sessions", ""]), true);
  assert.equal(serverOnlyPath(["Auth", "Ended-Sessions"]), true);
  assert.equal(serverOnlyPath(["auth", "me"]), false);
  assert.equal(serverOnlyPath(["sessions"]), false);
  // A dot segment would resolve onto it upstream.
  assert.equal(serverOnlyPath(["x", "..", "auth", "ended-sessions"]), true);
  assert.equal(serverOnlyPath(["."]), true);
});
