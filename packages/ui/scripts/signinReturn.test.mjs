// Signing in from a host's page returns only to that host's own desk page
// (lib/signinReturn.ts, app/signin/sso, app/signin/return).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  FAILED_PARAM,
  failedTarget,
  hintOf,
  loginHint,
  otherPerson,
  returnTarget,
  signOutTarget,
} from "../src/lib/signinReturn.ts";

const HOSTS = ["http://erp.dynocortex.localhost:8088", "https://erp.example.com"];
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("a host's own desk page is followed, rebuilt from its parts", () => {
  assert.equal(
    returnTarget("http://erp.dynocortex.localhost:8088/desk/dynocortex-home", HOSTS),
    "http://erp.dynocortex.localhost:8088/desk/dynocortex-home",
  );
  assert.equal(
    returnTarget("https://erp.example.com/desk/dynocortex/people?full_view=1", HOSTS),
    "https://erp.example.com/desk/dynocortex/people?full_view=1",
  );
  assert.equal(returnTarget("https://erp.example.com/app/todo#x", HOSTS), "https://erp.example.com/app/todo");
  // Rebuilt by the URL parser, so what goes back is always encoded.
  assert.equal(returnTarget("https://erp.example.com/desk/x y", HOSTS), "https://erp.example.com/desk/x%20y");
});

test("anything else goes nowhere", () => {
  for (const to of [
    "",
    null,
    undefined,
    "/desk/dynocortex-home",
    "//erp.example.com/desk/x",
    "https://evil.example/desk/x",
    "https://erp.example.com.evil.example/desk/x",
    "http://erp.example.com/desk/x",
    "https://user:pw@erp.example.com/desk/x",
    "https://erp.example.com/login",
    "https://erp.example.com/api/method/frappe.auth.get_logged_user",
    "https://erp.example.com/desk/../api/method/x",
    "https://erp.example.com/desk//evil",
    "https://erp.example.com/desk/%2E%2E%2Flogin",
    "https://erp.example.com/desk/%2e%2e%2F%2e%2e%2Flogin?redirect-to=%2F%2Fevil.com",
    "https://erp.example.com/desk/x%2F%2Fy",
    "https://erp.example.com/desk/x%5Cy",
    "javascript:alert(1)//https://erp.example.com/desk/x",
    `https://erp.example.com/desk/${"a".repeat(2100)}`,
  ]) {
    assert.equal(returnTarget(to, HOSTS), null, String(to));
  }
  assert.equal(returnTarget("https://erp.example.com/desk/x", []), null, "no host origins");
});

test("a failed sign-in says so on the host's page", () => {
  const url = new URL(failedTarget("https://erp.example.com/desk/dynocortex-home?a=1"));
  assert.equal(url.searchParams.get(FAILED_PARAM), "failed");
  assert.equal(url.searchParams.get("a"), "1");
});

test("login_hint is a plain email or nothing", () => {
  assert.equal(loginHint(" Owner@Example.com "), "owner@example.com");
  for (const raw of ["", "owner", "a@b c", "<a@b>", "a@b\"x", "x".repeat(320) + "@e.com", null, 7]) {
    assert.equal(loginHint(raw), undefined, String(raw));
  }
});

test("the desk-route rules are the same ones erpnextLinks keeps", () => {
  for (const name of ["DESK_ROUTE_RE", "ENCODED_SEPARATOR_RE"]) {
    const rule = (source) => source.match(new RegExp(`const ${name} = (\/.+\/i?);`))[1];
    assert.equal(rule(read("../src/lib/signinReturn.ts")), rule(read("../src/lib/erpnextLinks.ts")), name);
  }
});

test("the routes check `to` both ways and never trust the hop", () => {
  const sso = read("../src/app/signin/sso/route.ts");
  const back = read("../src/app/signin/return/route.ts");
  assert.match(sso, /const to = returnTarget\(params\.get\("to"\), HOST_ORIGINS\.origins\);/);
  assert.match(back, /returnTarget\(new URL\(request\.url\)\.searchParams\.get\("to"\), HOST_ORIGINS\.origins\)/);
  assert.match(sso, /httpOnly: true/);
  assert.match(read("../src/app/signin/page.tsx"), /redirect\("\/signin\/sso\?failed=1"\)/);
});

test("a card answered top level offers the way back to the host's own page for it", async () => {
  const { hostReturn, withReturn, returnFromQuery, RETURN_PARAM } = await import("../src/lib/signinReturn.ts");
  const base = "https://erp.example.com/desk/northwind";
  // Only the page's path goes back: its query is this app's own.
  assert.equal(hostReturn(base, "/cards/alert/58?embed=1", HOSTS), "https://erp.example.com/desk/northwind/cards/alert/58");
  assert.equal(hostReturn(base, "/", HOSTS), "https://erp.example.com/desk/northwind");
  // No base, a base on another origin, or a page that isn't a path: nothing.
  assert.equal(hostReturn(null, "/cards/1", HOSTS), null);
  assert.equal(hostReturn("https://evil.example/desk/x", "/cards/1", HOSTS), null);
  assert.equal(hostReturn(base, "//evil.example/x", HOSTS), null);
  assert.equal(hostReturn(base, "/cards/%2e%2e%2flogin", HOSTS), null);

  const page = withReturn("/cards/1?x=2", "https://erp.example.com/desk/northwind/cards/1");
  assert.equal(new URLSearchParams(page.split("?")[1]).get(RETURN_PARAM), "https://erp.example.com/desk/northwind/cards/1");
  assert.ok(page.startsWith("/cards/1?x=2&"));
  assert.equal(withReturn("/cards/1", null), "/cards/1");

  // Read back, the way back is checked again: a hand-made link to anywhere else gets none.
  assert.equal(returnFromQuery(`?${page.split("?")[1]}`, HOSTS), "https://erp.example.com/desk/northwind/cards/1");
  assert.equal(returnFromQuery("?return=https%3A%2F%2Fevil.example%2Fdesk%2Fx", HOSTS), null);
  assert.equal(returnFromQuery("?return=https%3A%2F%2Ferp.example.com%2Flogin", HOSTS), null);
  assert.equal(returnFromQuery("?return=javascript%3Aalert(1)", HOSTS), null);
  assert.equal(returnFromQuery("", HOSTS), null);
});

test("the frame takes a return base only from the very host that framed it", () => {
  const provider = read("../src/components/embed/EmbedProvider.tsx");
  assert.match(provider, /returnBase && new URL\(returnBase\)\.origin !== event\.origin\) return;/);
});

test("signing out goes on to any page of a host, and nowhere else", () => {
  assert.equal(
    signOutTarget("https://erp.example.com/api/method/logout?next=%2Flogin", HOSTS),
    "https://erp.example.com/api/method/logout?next=%2Flogin",
  );
  assert.equal(signOutTarget("https://erp.example.com/desk/x#y", HOSTS), "https://erp.example.com/desk/x");
  // The parser resolves dot segments first, so they stay on the host.
  assert.equal(signOutTarget("https://erp.example.com/a/../b", HOSTS), "https://erp.example.com/b");
  for (const to of [
    "",
    null,
    "/desk/x",
    "https://evil.example.com/logout",
    "https://erp.example.com.evil.example/logout",
    "https://user:pw@erp.example.com/logout",
    "javascript:alert(1)",
    "https://erp.example.com//evil.example.com",
    "https://erp.example.com/a%2fb",
    "https://erp.example.com/a%5Cb",
    "https://erp.example.com/a\\b",
  ]) {
    assert.equal(signOutTarget(to, HOSTS), null, String(to));
  }
});

test("the sign-in takes hint or login_hint, and replaces only someone else's session", () => {
  assert.equal(hintOf(new URLSearchParams("hint=B@Example.com")), "b@example.com");
  assert.equal(hintOf(new URLSearchParams("login_hint=b@example.com")), "b@example.com");
  assert.equal(hintOf(new URLSearchParams("hint=not-an-email")), undefined);
  assert.equal(otherPerson("A@example.com", "b@example.com"), true);
  assert.equal(otherPerson(null, "b@example.com"), true);
  assert.equal(otherPerson(" B@Example.com ", "b@example.com"), false);
  // No hint: whoever is signed in stays, as before.
  assert.equal(otherPerson("a@example.com", undefined), false);
  const sso = read("../src/app/signin/sso/route.ts");
  assert.match(sso, /if \(!otherPerson\(session\.user\?\.email, hint\)\) return Response\.redirect\(to\);\n\s*\/\/[^\n]*\n[^\n]*\n\s*await signOut\(\{ redirect: false \}\);/);
});

test("the sign-out answers only a navigation, ends the session with its time, and goes only to a host", () => {
  const out = read("../src/app/signout/sso/route.ts");
  assert.match(out, /if \(mode && mode !== "navigate"\)/);
  assert.match(out, /signOutTarget\(new URL\(request\.url\)\.searchParams\.get\("to"\), HOST_ORIGINS\.origins\)/);
  assert.match(out, /sid: provider\.sid, sub: provider\.sub, ended_at: Math\.floor\(Date\.now\(\) \/ 1000\)/);
  assert.match(out, /await signOut\(\{ redirect: false \}\);/);
});
