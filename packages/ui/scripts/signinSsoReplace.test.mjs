// /signin/sso's replace pass (app/signin/sso/route.ts): the desk is signed in
// as A, this app's session is B's, and the identity provider still holds B's
// own session, so it answers the new sign-in as B again. The tab must still come
// back to the desk page (through /signin/return), where the desk's own check
// sees B beside A and says so. It used to land on this app's Home.
//
// The cause is in how Auth.js's server actions keep the callback URL, so this
// runs next-auth's real signOut and signIn (next-auth/lib/actions.js) and
// @auth/core's real callback, with next/headers and next/navigation replaced by
// a request's headers and a cookie jar that keeps the last value set per name,
// as Next's does. The provider is faked over fetch and answers as B. The steps
// /signin/sso takes are lib/signinReturn.ts's signInPass, run here as they are.
//
// A provider that keeps answering as B could send the tab round again: the
// desk marks a replace as tried in its own storage, and this app keeps its own
// mark for a minute (REPLACE_COOKIE), checked here on a clock the test moves.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { register } from "node:module";
import test from "node:test";

import {
  REPLACE_COOKIE,
  REPLACE_COOKIE_MAX_AGE_S,
  RETURN_COOKIE_PATH,
  otherPerson,
  returnPath,
  returnTarget,
  signInPass,
} from "../src/lib/signinReturn.ts";

// next-auth imports next/headers and next/navigation, which work only inside a
// Next request. These stand-ins read the request this test sets up.
const STUBS = {
  "next/headers":
    "export const headers = async () => globalThis.__ssoTest.headers;" +
    "export const cookies = async () => globalThis.__ssoTest.jar;",
  "next/navigation":
    "export function redirect(url) { const e = new Error('NEXT_REDIRECT'); e.url = url; throw e; }",
};
register(
  "data:text/javascript," +
    encodeURIComponent(`
      const STUBS = ${JSON.stringify(STUBS)};
      export async function resolve(specifier, context, next) {
        if (Object.hasOwn(STUBS, specifier)) {
          return { url: "data:text/javascript," + encodeURIComponent(STUBS[specifier]), shortCircuit: true };
        }
        return next(specifier, context);
      }`),
);
const { signIn, signOut } = await import(new URL("../node_modules/next-auth/lib/actions.js", import.meta.url).href);
const { Auth } = await import("@auth/core");
const { decode, encode } = await import("@auth/core/jwt");

const APP = "https://oe.example.com";
const ISSUER = "https://sso.example.com/realms/company";
const DESK = "https://erp.example.com/desk/dynocortex-home";
const HOSTS = ["https://erp.example.com"];
const SECRET = "test-secret-".padEnd(48, "x");
const A = "a@example.com";
const B = "b@example.com";
const SESSION = "__Secure-authjs.session-token";
const CALLBACK_URL = "__Secure-authjs.callback-url";

const config = {
  secret: SECRET,
  trustHost: true,
  basePath: "/api/auth",
  session: { strategy: "jwt" },
  pages: { signIn: "/signin", error: "/signin" },
  providers: [
    {
      id: "oidc",
      name: "SSO",
      type: "oidc",
      issuer: ISSUER,
      clientId: "openexecutive",
      clientSecret: "client-secret",
      checks: ["pkce", "state"],
    },
  ],
  logger: { error() {}, warn() {}, debug() {} },
};

// The identity provider: discovery, and a token endpoint whose ID token names
// B, whoever the login_hint asked for (B's provider session is still alive).
function b64url(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}
function idTokenFor(email) {
  const now = Math.floor(Date.now() / 1000);
  const claims = { iss: ISSUER, aud: "openexecutive", sub: email, email, email_verified: true, iat: now, exp: now + 300 };
  return `${b64url({ alg: "RS256", typ: "JWT" })}.${b64url(claims)}.c2ln`;
}
globalThis.fetch = async (input) => {
  const url = new URL(typeof input === "string" ? input : input.url);
  const json = (body) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
  if (url.href === `${ISSUER}/.well-known/openid-configuration`) {
    return json({
      issuer: ISSUER,
      authorization_endpoint: `${ISSUER}/protocol/openid-connect/auth`,
      token_endpoint: `${ISSUER}/protocol/openid-connect/token`,
      userinfo_endpoint: `${ISSUER}/protocol/openid-connect/userinfo`,
      jwks_uri: `${ISSUER}/protocol/openid-connect/certs`,
      id_token_signing_alg_values_supported: ["RS256"],
    });
  }
  if (url.href === `${ISSUER}/protocol/openid-connect/token`) {
    return json({ access_token: "at", token_type: "bearer", expires_in: 300, id_token: idTokenFor(B) });
  }
  throw new Error(`unexpected fetch ${url.href}`);
};

// The browser's clock, moved by the tests; cookies expire by it.
let now = Date.now();

/**
 * The browser's cookies for this app, as they appear on the wire: Next and
 * Auth.js send a value URL-encoded and Auth.js decodes what comes back, so a
 * value read here is decoded the same way. A cookie past its Max-Age is
 * neither sent nor read.
 */
class Browser {
  constructor(cookies) {
    this.wire = new Map(Object.entries(cookies).map(([name, value]) => [name, { raw: encodeURIComponent(value) }]));
  }
  live() {
    return [...this.wire].filter(([, c]) => c.expiresAt === undefined || c.expiresAt > now);
  }
  get(name) {
    const found = this.live().find(([n]) => n === name);
    return found ? decodeURIComponent(found[1].raw) : undefined;
  }
  header() {
    return this.live()
      .map(([name, c]) => `${name}=${c.raw}`)
      .join("; ");
  }
  /** A Set-Cookie as written on the wire (`raw` already encoded). */
  takeRaw(name, raw, maxAge) {
    if (maxAge === 0 || raw === "") this.wire.delete(name);
    else this.wire.set(name, { raw, expiresAt: maxAge === undefined ? undefined : now + maxAge * 1000 });
  }
  /** What Next's cookies().set sends: the value encoded, as its ResponseCookies does. */
  take(name, value, options = {}) {
    const gone = options.maxAge === 0 || (options.expires && new Date(options.expires).getTime() <= now);
    this.takeRaw(name, gone ? "" : encodeURIComponent(value), options.maxAge);
  }
}

/**
 * Next's cookies() in a route handler: get reads what this request set, else
 * the request's own cookies; what was set last for a name is what's sent.
 */
class Jar {
  constructor(browser) {
    this.browser = browser;
    this.sent = new Map();
  }
  get(name) {
    if (this.sent.has(name)) return { value: this.sent.get(name).value };
    const value = this.browser.get(name);
    return value === undefined ? undefined : { value };
  }
  set(name, value, options) {
    this.sent.set(name, { value, options });
  }
}

/**
 * One GET /signin/sso?to=<desk>&hint=<hint> from the desk tab, through
 * signInPass with next-auth's real signOut and signIn. `legacy` gives signOut
 * no redirectTo, as before the fix. Says where the tab goes, how many
 * sign-outs ran, and what was set on the cookies.
 */
async function ssoPass(browser, { legacy = false, hint = A } = {}) {
  const jar = new Jar(browser);
  const headers = new Headers({
    host: "oe.example.com",
    "x-forwarded-proto": "https",
    cookie: browser.header(),
    // The desk page sent the tab here; a cross-origin Referer is its origin.
    referer: "https://erp.example.com/",
  });
  globalThis.__ssoTest = { headers, jar };
  // auth(): the session cookie, decoded as Auth.js does.
  const token = browser.get(SESSION);
  const claims = token ? await decode({ token, secret: SECRET, salt: SESSION }) : null;
  let signOuts = 0;
  let provider;
  let straightBack;
  try {
    straightBack = await signInPass({
      to: DESK,
      hint,
      session: claims ? { email: claims.email } : null,
      ownTab: true,
      secure: true,
      cookies: jar,
      signOut: (back) => {
        signOuts += 1;
        return signOut({ redirect: false, ...(legacy ? {} : { redirectTo: back }) }, config);
      },
      signIn: (back) => signIn("oidc", { redirectTo: back }, { login_hint: hint }, config),
    });
  } catch (error) {
    provider = new URL(error.url);
    assert.equal(provider.origin + provider.pathname, `${ISSUER}/protocol/openid-connect/auth`);
    assert.equal(provider.searchParams.get("login_hint"), hint);
  }
  for (const [name, { value, options }] of jar.sent) browser.take(name, value, options);
  return { straightBack, provider, signOuts, sent: jar.sent };
}

/** The provider answers as B; Auth.js's callback says where the tab goes next. */
async function callback(browser, provider) {
  const url = `${APP}/api/auth/callback/oidc?code=code-1&state=${provider.searchParams.get("state")}`;
  const response = await Auth(new Request(url, { headers: { cookie: browser.header() } }), config);
  for (const cookie of response.headers.getSetCookie()) {
    const [pair, ...attrs] = cookie.split(";");
    const at = pair.indexOf("=");
    const maxAge = attrs.find((a) => a.trim().toLowerCase().startsWith("max-age="));
    browser.takeRaw(pair.slice(0, at), pair.slice(at + 1), maxAge ? Number(maxAge.split("=")[1]) : undefined);
  }
  return response.headers.get("location");
}

/** A browser with B's session, and Auth.js's callback-url cookie as `left`. */
async function signedInAsB(left) {
  const token = await encode({ token: { email: B, sub: B }, secret: SECRET, salt: SESSION });
  return new Browser({ [SESSION]: token, ...(left ? { [CALLBACK_URL]: left } : {}) });
}

const BACK_URL = `${APP}${returnPath(DESK)}`;

/** A replace pass that the provider answers as B: the page the tab ends on. */
async function replacedAsB(browser) {
  const { provider, signOuts } = await ssoPass(browser);
  assert.ok(provider, "the sign-in started");
  assert.equal(signOuts, 1);
  const next = await callback(browser, provider);
  assert.equal(next, BACK_URL);
  return returnTarget(new URL(next).searchParams.get("to"), HOSTS);
}

test("the cause: signOut's own callback URL stood when the browser already held the sign-in's", async () => {
  now = Date.now();
  // B signed in earlier from the same desk page, so the browser kept that way back.
  const browser = await signedInAsB(BACK_URL);
  const { provider } = await ssoPass(browser, { legacy: true });
  assert.equal(browser.get(CALLBACK_URL), APP, "signOut saved Home; signIn saw nothing to change");
  assert.equal(await callback(browser, provider), APP, "so the tab landed on Home");
});

test("a replace pass that comes back as the same other person returns to the desk page", async () => {
  now = Date.now();
  for (const left of [BACK_URL, undefined, `${APP}/somewhere-else`]) {
    const browser = await signedInAsB(left);
    const { provider } = await ssoPass(browser);
    assert.equal(browser.get(CALLBACK_URL), BACK_URL, String(left));
    const next = await callback(browser, provider);
    assert.equal(next, BACK_URL, String(left));
    assert.ok(browser.get(SESSION), "signed in, as B");
    // /signin/return checks `to` again and sends the tab to the desk page itself,
    // unmarked: not failed, and not another /signin/sso. The desk sees B beside
    // A and shows its warning.
    const to = returnTarget(new URL(next).searchParams.get("to"), HOSTS);
    assert.equal(to, DESK, String(left));
  }
});

test("the replace pass leaves its mark for a minute, HttpOnly, on /signin only", async () => {
  now = Date.now();
  const browser = await signedInAsB(BACK_URL);
  const { sent } = await ssoPass(browser);
  const mark = sent.get(REPLACE_COOKIE);
  assert.equal(mark.value, A, "for this host user");
  assert.deepEqual(mark.options, {
    httpOnly: true,
    sameSite: "lax",
    secure: true,
    path: RETURN_COOKIE_PATH,
    maxAge: REPLACE_COOKIE_MAX_AGE_S,
  });
  assert.equal(REPLACE_COOKIE_MAX_AGE_S, 60);
});

test("a second replace pass within the minute keeps the session and goes back to the desk, not Home", async () => {
  now = Date.now();
  const browser = await signedInAsB(BACK_URL);
  assert.equal(await replacedAsB(browser), DESK);
  const session = browser.get(SESSION);
  // The desk's own mark was lost (blocked storage), so it sends the tab again.
  now += 30_000;
  const again = await ssoPass(browser);
  assert.equal(again.signOuts, 0, "no second sign-out");
  assert.equal(again.provider, undefined, "no second trip to the provider");
  assert.equal(again.straightBack, DESK, "straight back to the desk page");
  assert.equal(browser.get(SESSION), session, "B's session kept as it was");
  // So the desk finds B signed in beside its own A, and shows its warning.
  const claims = await decode({ token: browser.get(SESSION), secret: SECRET, salt: SESSION });
  assert.equal(otherPerson(claims.email, A), true);
});

test("the mark expires: a pass after the minute signs out and starts the sign-in again", async () => {
  now = Date.now();
  const browser = await signedInAsB(BACK_URL);
  assert.equal(await replacedAsB(browser), DESK);
  now += (REPLACE_COOKIE_MAX_AGE_S - 1) * 1000;
  assert.equal(browser.get(REPLACE_COOKIE), A, "still there just inside the minute");
  now += 2000;
  assert.equal(browser.get(REPLACE_COOKIE), undefined, "gone after it");
  assert.doesNotMatch(browser.header(), new RegExp(REPLACE_COOKIE), "and no longer sent");
  assert.equal(await replacedAsB(browser), DESK, "a full replace pass again");
});

test("the mark is per host user: another desk user within the minute is still signed in for", async () => {
  now = Date.now();
  const browser = await signedInAsB(BACK_URL);
  assert.equal(await replacedAsB(browser), DESK);
  now += 10_000;
  const other = await ssoPass(browser, { hint: "c@example.com" });
  assert.equal(other.signOuts, 1);
  assert.ok(other.provider, "the sign-in started for c");
});

test("/signin/sso runs signInPass, giving signOut and signIn the same way back", () => {
  const route = readFileSync(new URL("../src/app/signin/sso/route.ts", import.meta.url), "utf8");
  assert.match(route, /const straightBack = await signInPass\(\{/);
  assert.match(route, /signOut: \(back\) => signOut\(\{ redirect: false, redirectTo: back \}\),/);
  assert.match(route, /signIn: \(back\) => signIn\(OIDC_PROVIDER_ID, \{ redirectTo: back \}, /);
  assert.match(route, /ownTab: ownNavigation\(request\.headers\),/);
  assert.match(route, /sessionStillAllowed\(session, /);
  assert.doesNotMatch(route, /encodeURIComponent/, "the path is built in one place, lib/signinReturn.ts");
});

test("the way back carries `to` encoded, for /signin/return to check again", () => {
  const to = "https://erp.example.com/desk/x?full_view=1&b=2";
  const back = new URL(returnPath(to), APP);
  assert.equal(back.pathname, "/signin/return");
  assert.equal(back.searchParams.get("to"), to);
  assert.deepEqual([...back.searchParams.keys()], ["to"]);
});
