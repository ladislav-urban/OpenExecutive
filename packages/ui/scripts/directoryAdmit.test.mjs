import assert from "node:assert/strict";
import test from "node:test";
import {
  WAITING_REDIRECT,
  admitAfterDenial,
  admitEligible,
  directorySyncOn,
  requestAdmit,
} from "../src/lib/directoryAdmit.ts";
import { createRosterLoader, decideAllowed, resolveAllowed } from "../src/lib/allowlist.ts";
import { directoryRefusal, directorySubjects } from "../src/lib/directory.ts";
import { serverOnlyPath } from "../src/lib/proxySegments.ts";

const SSO = "oidc";
const SUB = "0b6f9f0e-1c7a-4d1e-9a51-000000000001";
const OTHER_SUB = "0b6f9f0e-1c7a-4d1e-9a51-000000000002";
const NEW = "new.member@example.com";
const ENV = new Set(["operator@example.com"]);

const eligible = (over = {}) => ({
  provider: SSO,
  ssoProviderId: SSO,
  syncOn: true,
  allowed: false,
  source: "no_match",
  sub: SUB,
  ...over,
});

// --- when sign-in asks -------------------------------------------------------

test("only a refused SSO sign-in with the sync on and a subject asks", () => {
  assert.equal(admitEligible(eligible()), true);
  assert.equal(admitEligible(eligible({ source: "env_only_roster_unavailable" })), true);
});

test("Google and local login never ask", () => {
  assert.equal(admitEligible(eligible({ provider: "google" })), false);
  assert.equal(admitEligible(eligible({ provider: "local" })), false);
  assert.equal(admitEligible(eligible({ provider: "unknown" })), false);
});

test("nothing is asked while the sync is off", () => {
  assert.equal(admitEligible(eligible({ syncOn: false })), false);
});

test("an allowed sign-in or an ALLOWED_EMAILS address never asks", () => {
  assert.equal(admitEligible(eligible({ allowed: true, source: "roster" })), false);
  assert.equal(admitEligible(eligible({ allowed: true, source: "env" })), false);
  // An env hit is never a denial, but even a denial marked env never asks.
  assert.equal(admitEligible(eligible({ source: "env" })), false);
  const decision = decideAllowed("operator@example.com", ENV, null);
  assert.equal(admitEligible(eligible({ allowed: decision.allowed, source: decision.source })), false);
});

test("a sign-in without a string subject never asks", () => {
  for (const sub of [undefined, null, "", 42, { id: SUB }]) {
    assert.equal(admitEligible(eligible({ sub })), false, String(sub));
  }
});

test("the sync flag reads like the API's boolean", () => {
  for (const on of ["true", "TRUE", " 1 ", "yes", "on"]) assert.equal(directorySyncOn(on), true, on);
  for (const off of [undefined, null, "", "false", "0", "no", "off", "maybe"]) assert.equal(directorySyncOn(off), false, String(off));
});

// --- the request -------------------------------------------------------------

const answering = (status, body, record = []) => {
  const impl = async (url, init) => {
    record.push({ url, init });
    return { status, ok: status >= 200 && status < 300, json: async () => body };
  };
  impl.calls = record;
  return impl;
};

const opts = (fetchImpl, over = {}) => ({ baseUrl: "http://api:8000", sharedSecret: "s3cret", timeoutMs: 4000, fetchImpl, ...over });

test("the request posts the subject and email with the shared secret, uncached", async () => {
  const fetchImpl = answering(200, { admitted: true, code: "admitted" });
  assert.deepEqual(await requestAdmit(SUB, NEW, opts(fetchImpl)), { admitted: true, code: "admitted" });
  const [{ url, init }] = fetchImpl.calls;
  assert.equal(url, "http://api:8000/auth/directory-admit");
  assert.equal(init.method, "POST");
  assert.equal(init.cache, "no-store");
  assert.equal(init.headers["x-api-key"], "s3cret");
  assert.equal(init.headers["content-type"], "application/json");
  assert.deepEqual(JSON.parse(init.body), { subject: SUB, email: NEW });
  assert.ok(init.signal instanceof AbortSignal);
});

test("no shared secret, no x-api-key", async () => {
  const fetchImpl = answering(200, { admitted: false, code: "not_member" });
  await requestAdmit(SUB, NEW, opts(fetchImpl, { sharedSecret: "" }));
  assert.equal("x-api-key" in fetchImpl.calls[0].init.headers, false);
});

test("a refusal comes back with its code", async () => {
  for (const code of ["exists", "address_held", "not_member", "busy", "rate_limited", "error", "disabled"]) {
    assert.deepEqual(await requestAdmit(SUB, NEW, opts(answering(200, { admitted: false, code }))), { admitted: false, code });
  }
});

test("a non-200, a throw, a timeout or an odd body is a refusal", async () => {
  assert.deepEqual(await requestAdmit(SUB, NEW, opts(answering(500, { admitted: true, code: "admitted" }))), { admitted: false, code: "http_error" });
  assert.deepEqual(await requestAdmit(SUB, NEW, opts(answering(201, { admitted: true, code: "admitted" }))), { admitted: false, code: "http_error" });
  assert.deepEqual(await requestAdmit(SUB, NEW, opts(answering(404, null))), { admitted: false, code: "http_error" });
  const throwing = async () => { throw new TypeError("fetch failed"); };
  assert.deepEqual(await requestAdmit(SUB, NEW, opts(throwing)), { admitted: false, code: "unreachable" });
  const timingOut = async () => { throw new DOMException("timed out", "TimeoutError"); };
  assert.deepEqual(await requestAdmit(SUB, NEW, opts(timingOut)), { admitted: false, code: "timeout" });
  const badJson = async () => ({ status: 200, ok: true, json: async () => { throw new SyntaxError("x"); } });
  assert.deepEqual(await requestAdmit(SUB, NEW, opts(badJson)), { admitted: false, code: "unreachable" });
  for (const body of [null, [], {}, { admitted: true }, { admitted: true, code: "yes" }, { code: "<script>" }]) {
    assert.deepEqual(await requestAdmit(SUB, NEW, opts(answering(200, body))), { admitted: false, code: "bad_answer" }, JSON.stringify(body));
  }
});

test("admitted must be both true and the admitted code", async () => {
  assert.deepEqual(await requestAdmit(SUB, NEW, opts(answering(200, { admitted: true, code: "exists" }))), { admitted: false, code: "exists" });
  assert.deepEqual(await requestAdmit(SUB, NEW, opts(answering(200, { admitted: "true", code: "admitted" }))), { admitted: false, code: "admitted" });
});

test("a hung API is given up on at the timeout", async () => {
  const hanging = (_url, init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason));
    });
  // AbortSignal.timeout's timer doesn't hold the event loop open (a real
  // request's socket does), so without this Node can exit mid-test.
  const keepAlive = setTimeout(() => {}, 5000);
  const started = Date.now();
  try {
    assert.deepEqual(await requestAdmit(SUB, NEW, opts(hanging, { timeoutMs: 30 })), { admitted: false, code: "timeout" });
  } finally {
    clearTimeout(keepAlive);
  }
  assert.ok(Date.now() - started < 2000);
});

// --- after the admit: the fresh read and the same checks ---------------------

/** A sign-in's tail as auth.ts runs it, over a real roster loader. */
function harness({ admitAnswer, rosterRows, rosterStatus = 200, sub = SUB, email = NEW }) {
  const fetches = [];
  let rows = rosterRows;
  const loader = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 5 * 60 * 1000,
    timeoutMs: 1000,
    now: () => 0,
    fetchImpl: async (url) => {
      fetches.push(url);
      return { ok: rosterStatus === 200, status: rosterStatus, json: async () => rows };
    },
  });
  const admitCalls = [];
  const deps = {
    admit: async (subject, mail) => {
      admitCalls.push([subject, mail]);
      if (admitAnswer instanceof Error) throw admitAnswer;
      return admitAnswer;
    },
    refresh: () => loader.refresh(),
    recheck: async (fresh) => {
      const again = await resolveAllowed(email, ENV, async () => fresh.emails);
      if (!again.allowed) return "not_in_allowlist";
      return directoryRefusal({ provider: SSO, email, sub }, directorySubjects(fresh.rows), SSO);
    },
  };
  return {
    run: () => admitAfterDenial({ subject: sub, email }, deps),
    loader,
    fetches,
    admitCalls,
    setRows: (next) => { rows = next; },
  };
}

const row = (email, subject, id = 7) => ({ email, person_id: id, directory_subject: subject });

test("admitted: one fresh read lets them in, and the cache holds it for authorized", async () => {
  const h = harness({ admitAnswer: { admitted: true, code: "admitted" }, rosterRows: [row(NEW, SUB)] });
  assert.deepEqual(await h.run(), { allow: true, code: "admitted" });
  assert.deepEqual(h.admitCalls, [[SUB, NEW]]);
  assert.equal(h.fetches.length, 1);
  // `authorized`, on the next request, is served the refreshed roster.
  assert.ok((await h.loader()).has(NEW));
  assert.equal(h.fetches.length, 1, "no second fetch: the refresh filled the cache");
});

test("exists: their Person is there already, so a fresh read may let them in", async () => {
  const h = harness({ admitAnswer: { admitted: false, code: "exists" }, rosterRows: [row(NEW, SUB)] });
  assert.deepEqual(await h.run(), { allow: true, code: "exists" });
});

test("exists for a suspended Person still refuses: the roster doesn't list them", async () => {
  const h = harness({ admitAnswer: { admitted: false, code: "exists" }, rosterRows: [row("someone@example.com", OTHER_SUB)] });
  assert.deepEqual(await h.run(), { allow: false, code: "exists", reason: "not_in_allowlist", waiting: false });
});

test("a refusal, a throw, a timeout or an HTTP error still refuses, without a read", async () => {
  for (const answer of [
    { admitted: false, code: "not_member" },
    { admitted: false, code: "address_held" },
    { admitted: false, code: "inactive" },
    { admitted: false, code: "error" },
    { admitted: false, code: "http_error" },
    { admitted: false, code: "timeout" },
    new Error("boom"),
  ]) {
    const h = harness({ admitAnswer: answer, rosterRows: [row(NEW, SUB)] });
    const outcome = await h.run();
    assert.equal(outcome.allow, false, JSON.stringify(answer));
    assert.equal(h.fetches.length, 0, "no roster read after a refusal");
  }
});

test("only not_member, busy and rate_limited say to try again shortly", async () => {
  const waiting = {};
  for (const code of ["not_member", "busy", "rate_limited", "address_held", "inactive", "email_mismatch", "unknown", "error", "timeout", "http_error", "disabled"]) {
    const h = harness({ admitAnswer: { admitted: false, code }, rosterRows: [] });
    waiting[code] = (await h.run()).waiting;
  }
  assert.deepEqual(
    Object.keys(waiting).filter((c) => waiting[c]),
    ["not_member", "busy", "rate_limited"],
  );
  assert.equal(WAITING_REDIRECT, "/signin?error=AccessDenied&reason=directory_waiting");
  assert.ok(WAITING_REDIRECT.startsWith("/") && !WAITING_REDIRECT.startsWith("//"));
});

test("admitted but the fresh read fails: refused, and the old cache is kept", async () => {
  const h = harness({ admitAnswer: { admitted: true, code: "admitted" }, rosterRows: [row("old@example.com", null)] });
  assert.ok((await h.loader()).has("old@example.com"));
  h.setRows("not a list");
  assert.deepEqual(await h.run(), { allow: false, code: "admitted", reason: "roster_unavailable", waiting: false });
  assert.ok((await h.loader()).has("old@example.com"), "the old roster still serves within its TTL");
});

test("admitted but the fresh roster doesn't list them: refused", async () => {
  const h = harness({ admitAnswer: { admitted: true, code: "admitted" }, rosterRows: [] });
  assert.deepEqual(await h.run(), { allow: false, code: "admitted", reason: "not_in_allowlist", waiting: false });
});

test("after an admit, a managed email signing in as another identity is still refused", async () => {
  // The fresh roster says the email is the Person linked to OTHER_SUB.
  const h = harness({
    admitAnswer: { admitted: true, code: "admitted" },
    rosterRows: [row(NEW, OTHER_SUB)],
  });
  assert.deepEqual(await h.run(), { allow: false, code: "admitted", reason: "directory_subject_mismatch", waiting: false });
});

test("a recheck that throws refuses", async () => {
  const outcome = await admitAfterDenial(
    { subject: SUB, email: NEW },
    {
      admit: async () => ({ admitted: true, code: "admitted" }),
      refresh: async () => ({ emails: new Set([NEW]), rows: [] }),
      recheck: async () => { throw new Error("x"); },
    },
  );
  assert.deepEqual(outcome, { allow: false, code: "admitted", reason: "recheck_failed", waiting: false });
});

test("a refresh that throws refuses", async () => {
  const outcome = await admitAfterDenial(
    { subject: SUB, email: NEW },
    {
      admit: async () => ({ admitted: true, code: "admitted" }),
      refresh: async () => { throw new Error("x"); },
      recheck: async () => null,
    },
  );
  assert.equal(outcome.allow, false);
});

// --- the proxy -----------------------------------------------------------------

test("the browser proxy never forwards the admit route", () => {
  assert.equal(serverOnlyPath(["auth", "directory-admit"]), true);
  assert.equal(serverOnlyPath(["auth", "Directory-Admit"]), true);
  assert.equal(serverOnlyPath(["auth", "directory-admit", ""]), true);
  assert.equal(serverOnlyPath(["auth", "directory-admit", "x"]), true);
  assert.equal(serverOnlyPath(["auth", "x", "..", "directory-admit"]), true);
  // The other server-only paths stay so.
  assert.equal(serverOnlyPath(["auth", "allowed-emails"]), true);
  assert.equal(serverOnlyPath(["auth", "ended-sessions"]), true);
  assert.equal(serverOnlyPath(["auth", "me"]), false);
});
