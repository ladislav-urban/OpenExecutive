// A direct visit to a page goes to the same screen in the host's desk
// (lib/deskRedirect.ts, docs/desk-redirect-plan.md): only a top-level document
// GET, never a page that stands alone, and only screen-state query keys.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  FORWARDED_KEYS,
  HELD_BACK_KEYS,
  MAX_VALUE_CHARS,
  STANDALONE_PARAM,
  STANDALONE_PATH,
  deskTarget,
  heldBack,
  parseDeskBase,
  topLevelVisit,
} from "../src/lib/deskRedirect.ts";
import { parseReturnBase } from "../src/lib/embed.ts";

const BASE = "https://erp.example.com/desk/x";
const SRC = fileURLToPath(new URL("../src/", import.meta.url));
const APP = join(SRC, "app");

const DOCUMENT = { "sec-fetch-dest": "document", "sec-fetch-mode": "navigate", "sec-fetch-site": "none" };
const headers = (h) => ({ get: (name) => h[name.toLowerCase()] ?? null });
const visit = (pathname, search = "", h = DOCUMENT, method = "GET") =>
  deskTarget({ method, pathname, search, headers: headers(h) }, BASE);

test("a direct visit goes to the same screen in the desk", () => {
  assert.equal(visit("/"), BASE);
  assert.equal(visit("/jobs"), `${BASE}/jobs`);
  assert.equal(visit("/jobs/"), `${BASE}/jobs`);
  assert.equal(visit("/chats/12", "?find=late"), `${BASE}/chats/12?find=late`);
  assert.equal(visit("/knowledge", "view=review"), `${BASE}/knowledge?view=review`);
  assert.equal(visit("/people/7", "", DOCUMENT, "HEAD"), `${BASE}/people/7`);
  // Cross-site (a link in an email) is a visit like any other.
  assert.equal(visit("/jobs", "", { ...DOCUMENT, "sec-fetch-site": "cross-site" }), `${BASE}/jobs`);
});

test("off without a desk base", () => {
  assert.equal(deskTarget({ method: "GET", pathname: "/jobs", search: "", headers: headers(DOCUMENT) }, null), null);
});

test("only a top-level document GET the person asked for", () => {
  for (const dest of ["iframe", "frame", "empty", "image", "script", "embed", "object", null]) {
    assert.equal(visit("/jobs", "", { ...DOCUMENT, "sec-fetch-dest": dest ?? undefined }), null, String(dest));
  }
  for (const mode of ["cors", "no-cors", "same-origin", null]) {
    assert.equal(visit("/jobs", "", { ...DOCUMENT, "sec-fetch-mode": mode ?? undefined }), null, String(mode));
  }
  // No fetch metadata at all: as today.
  assert.equal(visit("/jobs", "", {}), null);
  assert.equal(visit("/jobs", "", { ...DOCUMENT, "sec-purpose": "prefetch;prerender" }), null);
  assert.equal(visit("/jobs", "", { ...DOCUMENT, "sec-purpose": "prefetch" }), null);
  assert.equal(visit("/jobs", "", { ...DOCUMENT, purpose: "Prefetch" }), null);
  for (const method of ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]) {
    assert.equal(visit("/jobs", "", DOCUMENT, method), null, method);
  }
  assert.equal(topLevelVisit(headers(DOCUMENT)), true);
});

test("each page that stands alone stays here", () => {
  const alone = [
    "/api/auth/signin",
    "/api/auth/error",
    "/api/auth/callback/keycloak",
    "/api/auth/backchannel-logout",
    "/api/auth/end-session",
    "/api/backend/today",
    "/api",
    "/signin",
    "/signin/",
    "/signin/done",
    "/signin/return",
    "/signin/sso",
    "/signin/sso/",
    "/signout/sso",
    "/cards/7",
    "/cards/alert/3",
    "/cards",
    "/embed/approve/3",
    "/chats/shared/abc",
    "/icon.svg",
    "/robots.txt",
  ];
  for (const path of alone) assert.equal(visit(path), null, path);
  // Near misses are pages like any other.
  for (const path of ["/signin-help", "/signin/other", "/signout", "/signout/other", "/cardsx", "/embedx", "/chats/sharedx", "/apix", "/chats/shared-not"]) {
    assert.ok(visit(path), path);
  }
  assert.ok(STANDALONE_PATH.test("/signin/return"));
});

test("standalone=1 keeps any page here", () => {
  assert.equal(visit("/chats/12", `?${STANDALONE_PARAM}=1`), null);
  assert.equal(visit("/", "?tab=2&standalone=1"), null);
  assert.equal(visit("/", "?standalone="), null);
});

test("only screen-state keys go; everything else is dropped", () => {
  assert.equal(visit("/knowledge", "?view=review&embed=1&utm_source=mail&x=1"), `${BASE}/knowledge?view=review`);
  assert.equal(visit("/", "?embed=1"), BASE);
  // Exact: case and near names don't count.
  assert.equal(visit("/jobs", "?View=a&tabs=2&tab=3"), `${BASE}/jobs?tab=3`);
  for (const key of FORWARDED_KEYS) assert.equal(visit("/jobs", `?${key}=v`), `${BASE}/jobs?${key}=v`, key);
  // Values stay short, or the link stays here.
  assert.equal(visit("/chats/1", `?find=${"a".repeat(MAX_VALUE_CHARS)}`), `${BASE}/chats/1?find=${"a".repeat(MAX_VALUE_CHARS)}`);
  assert.equal(visit("/chats/1", `?find=${"a".repeat(MAX_VALUE_CHARS + 1)}`), null);
});

test("a secret, a one-time value or someone's words keep the link here", () => {
  for (const key of HELD_BACK_KEYS) {
    assert.equal(visit("/jobs", `?tab=1&${key}=v`), null, key);
    assert.equal(visit("/jobs", `?${key.toUpperCase()}=v`), null, key.toUpperCase());
  }
  for (const key of [
    "token", "access_token", "id_token", "code", "state", "key", "api_key", "sig", "signature",
    "secret", "client_secret", "password", "otp", "nonce", "ticket", "auth", "Authorization",
    "session_token", "SessionToken", "callbackUrl", "invite_code", "verifyToken", "magicKey",
  ]) {
    assert.equal(visit("/jobs", `?${key}=v`), null, key);
    assert.equal(heldBack(key), true, key);
  }
  // On the allowlist, exactly: forwarded although it holds `sig`.
  assert.equal(heldBack("designer"), false);
  assert.equal(heldBack("session"), false);
  assert.equal(visit("/jobs/new", "?designer=3"), `${BASE}/jobs/new?designer=3`);
  // A workflow link a chat wrote, with its prefill: stays here.
  assert.equal(visit("/jobs/report", "?prefill=eyJhIjoxfQ"), null);
  assert.equal(visit("/chats/new", "?draft=hello"), null);
});

test("a path that isn't plain stays here", () => {
  for (const path of ["//evil.example/x", "/a//b", "/a/../b", "/a/./b", "/..", "/a/%2e%2e/b", "/a%2fb", "/a%5cb", "/a\\b", "/a b", "/a?b", `/${"a".repeat(600)}`]) {
    assert.equal(visit(path), null, path);
  }
  assert.equal(visit("/jobs", `?q=${"a".repeat(1100)}`), null);
});

test("OE_DESK_BASE: a desk route on a host origin, or off", () => {
  const origins = ["https://erp.example.com"];
  assert.deepEqual(parseDeskBase(undefined, origins, parseReturnBase), { base: null, error: null });
  assert.deepEqual(parseDeskBase(" ", origins, parseReturnBase), { base: null, error: null });
  assert.deepEqual(parseDeskBase("https://erp.example.com/desk/x", origins, parseReturnBase), { base: BASE, error: null });
  for (const bad of [
    "https://other.example.com/desk/x",
    "https://erp.example.com/login",
    "https://erp.example.com/desk/x?y=1",
    "https://erp.example.com/desk/x#y",
    "https://a:b@erp.example.com/desk/x",
    "https://erp.example.com/desk/%2fx",
    "javascript:alert(1)",
    "not a url",
  ]) {
    const parsed = parseDeskBase(bad, origins, parseReturnBase);
    assert.equal(parsed.base, null, bad);
    assert.ok(parsed.error, bad);
  }
  assert.equal(parseDeskBase(BASE, [], parseReturnBase).base, null);
});

// Every page and route handler under src/app, as the path it serves.
function routes() {
  const found = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (/^(page|route)\.(tsx?|jsx?)$/.test(name)) {
        const segments = relative(APP, dir).split(sep).filter((s) => s && !/^\(.*\)$/.test(s));
        found.push({ kind: name.startsWith("page") ? "page" : "route", path: `/${segments.join("/")}` });
      }
    }
  };
  walk(APP);
  return found;
}

// The pages a direct visit sends to the desk. A new page fails the test below
// until it's added here, or made to stand alone (the rule: reached before a
// desk session, or not to run inside the desk).
const REDIRECTED_ROUTES = new Set([
  "/",
  "/architecture",
  "/artifacts",
  "/artifacts/[id]",
  "/audit",
  "/audit/session/[id]",
  "/audit/usage",
  "/briefing/[panel]",
  "/chats",
  "/chats/[id]",
  "/chats/new",
  "/clients",
  "/company-profile",
  "/council",
  "/delegate/act-as-me",
  "/delegate/handle-it",
  "/delegate/take-the-lead",
  // The principal's fixtures page, behind a session: no visitor's entry.
  "/demo",
  "/departments",
  "/departments/[slug]",
  "/goals",
  "/guide",
  "/jobs",
  "/jobs/[name]",
  "/jobs/new",
  "/jobs/runs/[id]",
  "/knowledge",
  "/memories",
  // Behind a session and the principal's; runs bare in the desk's frame.
  "/onboard",
  "/people",
  "/people/[id]",
  "/settings",
  "/settings/about",
  "/settings/advanced",
  "/settings/executive",
  "/settings/memory",
  "/settings/notifications",
  "/settings/status",
  "/settings/tools",
  "/settings/workspace",
  "/watchlist",
  "/watchlist/[slug]",
]);

test("every page is redirected or stands alone, and every route handler stands alone", () => {
  const all = routes();
  assert.ok(all.length > 40, "the app's routes were found");
  for (const { kind, path } of all) {
    const sample = path.replace(/\[\.\.\.[^\]]+\]/g, "a/b").replace(/\[[^\]]+\]/g, "x");
    const alone = visit(sample) === null;
    if (kind === "route") {
      assert.ok(alone, `route handler ${path} must stand alone`);
      assert.ok(!REDIRECTED_ROUTES.has(path), path);
    } else if (REDIRECTED_ROUTES.has(path)) {
      assert.ok(!alone, `${path} is listed as redirected`);
    } else {
      assert.ok(alone, `${path}: add it to REDIRECTED_ROUTES or make it stand alone`);
    }
  }
  const pages = new Set(all.map((r) => r.path));
  for (const path of REDIRECTED_ROUTES) assert.ok(pages.has(path), `${path} is no page any more`);
});

// Every query key the app reads, from its source.
function sourceFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
    else if (/\.(tsx?|jsx?)$/.test(name)) out.push(full);
  }
  return out;
}

// Read and never forwarded or held: dropped on the way to the desk.
const DROPPED = new Map([
  ["embed", "the frame's own mode; the desk sets it"],
  ["failed", "read only by /signin/sso, which stands alone"],
]);

test("every query key a page reads is forwarded, held back or dropped on purpose", () => {
  const consts = new Map();
  const keys = new Set();
  const files = sourceFiles(SRC);
  for (const file of files) {
    const text = readFileSync(file, "utf8");
    for (const m of text.matchAll(/\b([A-Z_]+_PARAM)\s*=\s*"([^"]+)"/g)) consts.set(m[1], m[2]);
  }
  for (const file of files) {
    const text = readFileSync(file, "utf8");
    for (const m of text.matchAll(/(?:searchParams|[pP]arams|URLSearchParams\([^)]*\)|query|search)\??\.(?:get|getAll|has)\(\s*(?:"([^"]+)"|'([^']+)'|([A-Z_]+_PARAM))\s*\)/g)) {
      const key = m[1] ?? m[2] ?? consts.get(m[3]);
      if (key) keys.add(key);
    }
  }
  assert.ok(keys.size > 10, "the app's query keys were found");
  for (const key of keys) {
    assert.ok(
      FORWARDED_KEYS.has(key) || heldBack(key) || DROPPED.has(key) || key === STANDALONE_PARAM,
      `query key "${key}": forward it (FORWARDED_KEYS), hold it back (HELD_BACK_KEYS) or drop it (DROPPED)`,
    );
  }
});

test("standalone is read by the redirect alone", () => {
  for (const file of sourceFiles(SRC)) {
    if (file.endsWith(`${sep}deskRedirect.ts`)) continue;
    const text = readFileSync(file, "utf8");
    // Written by lib/cardActions.ts's ownPageFor; read by no one but the redirect.
    assert.ok(!/\.(?:get|has|getAll)\(\s*(?:["']standalone["']|STANDALONE_PARAM)\s*\)/.test(text), file);
  }
});
