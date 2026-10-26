// Signing in from a host's page returns only to that host's own desk page
// (lib/signinReturn.ts, app/signin/sso, app/signin/return).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { FAILED_PARAM, failedTarget, loginHint, returnTarget } from "../src/lib/signinReturn.ts";

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

test("the desk-route rule is the same one erpnextLinks keeps", () => {
  const rule = (source) => source.match(/const DESK_ROUTE_RE = (\/.+\/);/)[1];
  assert.equal(rule(read("../src/lib/signinReturn.ts")), rule(read("../src/lib/erpnextLinks.ts")));
});

test("the routes check `to` both ways and never trust the hop", () => {
  const sso = read("../src/app/signin/sso/route.ts");
  const back = read("../src/app/signin/return/route.ts");
  assert.match(sso, /const to = returnTarget\(params\.get\("to"\), HOST_ORIGINS\.origins\);/);
  assert.match(back, /returnTarget\(new URL\(request\.url\)\.searchParams\.get\("to"\), HOST_ORIGINS\.origins\)/);
  assert.match(sso, /httpOnly: true/);
  assert.match(read("../src/app/signin/page.tsx"), /redirect\("\/signin\/sso\?failed=1"\)/);
});
