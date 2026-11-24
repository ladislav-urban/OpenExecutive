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
// as Next's does. The provider is faked over fetch and answers as B.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { register } from "node:module";
import test from "node:test";

import { returnPath, returnTarget } from "../src/lib/signinReturn.ts";

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
const { encode } = await import("@auth/core/jwt");

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

/**
 * The browser's cookies for this app, as they appear on the wire: Next and
 * Auth.js send a value URL-encoded and Auth.js decodes what comes back, so a
 * value read here is decoded the same way.
 */
class Browser {
  constructor(cookies) {
    this.wire = new Map(Object.entries(cookies).map(([name, value]) => [name, encodeURIComponent(value)]));
  }
  get(name) {
    const raw = this.wire.get(name);
    return raw === undefined ? undefined : decodeURIComponent(raw);
  }
  header() {
    return [...this.wire].map(([name, raw]) => `${name}=${raw}`).join("; ");
  }
  /** A Set-Cookie as written on the wire (`raw` already encoded). */
  takeRaw(name, raw, maxAge) {
    if (maxAge === 0 || raw === "") this.wire.delete(name);
    else this.wire.set(name, raw);
  }
  /** What Next's cookies().set sends: the value encoded, as its ResponseCookies does. */
  take(name, value, options = {}) {
    const gone = options.maxAge === 0 || (options.expires && new Date(options.expires).getTime() <= Date.now());
    this.takeRaw(name, gone ? "" : encodeURIComponent(value), undefined);
  }
}

/** Next's cookies() in a route handler: what was set last for a name is what's sent. */
class Jar {
  constructor() {
    this.sent = new Map();
  }
  set(name, value, options) {
    this.sent.set(name, { value, options });
  }
}

/** The replace pass as /signin/sso runs it, `signOutTo` given to signOut. */
async function replacePass(browser, signOutTo) {
  const jar = new Jar();
  const headers = new Headers({
    host: "oe.example.com",
    "x-forwarded-proto": "https",
    cookie: browser.header(),
    // The desk page sent the tab here; a cross-origin Referer is its origin.
    referer: "https://erp.example.com/",
  });
  globalThis.__ssoTest = { headers, jar };
  const back = returnPath(DESK);
  await signOut({ redirect: false, ...(signOutTo === undefined ? {} : { redirectTo: signOutTo(back) }) }, config);
  let provider;
  try {
    await signIn("oidc", { redirectTo: back }, { login_hint: A }, config);
  } catch (error) {
    provider = new URL(error.url);
  }
  assert.ok(provider, "signIn redirects to the provider");
  assert.equal(provider.origin + provider.pathname, `${ISSUER}/protocol/openid-connect/auth`);
  assert.equal(provider.searchParams.get("login_hint"), A);
  for (const [name, { value, options }] of jar.sent) browser.take(name, value, options);
  return provider;
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

test("the cause: signOut's own callback URL stood when the browser already held the sign-in's", async () => {
  // B signed in earlier from the same desk page, so the browser kept that way back.
  const browser = await signedInAsB(BACK_URL);
  const provider = await replacePass(browser, undefined);
  assert.equal(browser.get(CALLBACK_URL), APP, "signOut saved Home; signIn saw nothing to change");
  assert.equal(await callback(browser, provider), APP, "so the tab landed on Home");
});

test("a replace pass that comes back as the same other person returns to the desk page", async () => {
  for (const left of [BACK_URL, undefined, `${APP}/somewhere-else`]) {
    const browser = await signedInAsB(left);
    const provider = await replacePass(browser, (back) => back);
    assert.equal(browser.get(CALLBACK_URL), BACK_URL, String(left));
    const next = await callback(browser, provider);
    assert.equal(next, BACK_URL, String(left));
    assert.ok(browser.get(SESSION), "signed in, as B");
    // /signin/return checks `to` again and sends the tab to the desk page itself,
    // unmarked: not failed, and not another /signin/sso, so nothing goes round
    // again here. The desk sees B beside A and shows its warning.
    const to = returnTarget(new URL(next).searchParams.get("to"), HOSTS);
    assert.equal(to, DESK, String(left));
  }
});

test("/signin/sso gives signOut and signIn the same way back", () => {
  const route = readFileSync(new URL("../src/app/signin/sso/route.ts", import.meta.url), "utf8");
  assert.match(route, /const back = returnPath\(to\);/);
  assert.match(route, /await signOut\(\{ redirect: false, redirectTo: back \}\);/);
  assert.match(route, /\{ redirectTo: back \},/);
  assert.doesNotMatch(route, /encodeURIComponent/, "the path is built in one place, lib/signinReturn.ts");
});

test("the way back carries `to` encoded, for /signin/return to check again", () => {
  const to = "https://erp.example.com/desk/x?full_view=1&b=2";
  const back = new URL(returnPath(to), APP);
  assert.equal(back.pathname, "/signin/return");
  assert.equal(back.searchParams.get("to"), to);
  assert.deepEqual([...back.searchParams.keys()], ["to"]);
});
