// The identity provider's discovery document (OpenID Connect Discovery), for
// what the sign-out routes need from it: the keys that sign a logout token
// (`jwks_uri`, app/api/auth/backchannel-logout) and where to end its session
// (`end_session_endpoint`, app/api/auth/end-session). Read once per server;
// an address the issuer rule wouldn't allow (lib/oidc.ts) is left out.

import { issuerAllowed } from "@/lib/oidc";

export interface Discovery {
  jwksUri: string | null;
  endSessionEndpoint: string | null;
}

const TIMEOUT_MS = 5000;
let cached: { issuer: string; discovery: Discovery } | null = null;

export async function discover(issuer: string): Promise<Discovery> {
  if (cached && cached.issuer === issuer) return cached.discovery;
  const res = await fetch(`${issuer.replace(/\/+$/, "")}/.well-known/openid-configuration`, {
    cache: "no-store",
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`discovery: ${res.status}`);
  const meta = (await res.json()) as { jwks_uri?: unknown; end_session_endpoint?: unknown };
  const usable = (value: unknown) => (typeof value === "string" && issuerAllowed(value) ? value : null);
  const discovery = { jwksUri: usable(meta.jwks_uri), endSessionEndpoint: usable(meta.end_session_endpoint) };
  // Kept only when it has the keys: a provider that fixes its document is
  // read again on the next call.
  if (discovery.jwksUri) cached = { issuer, discovery };
  return discovery;
}
