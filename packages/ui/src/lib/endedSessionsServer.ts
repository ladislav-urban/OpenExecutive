// This server's copy of the sessions the provider ended (lib/endedSessions.ts),
// read from and written to the API's store (`/auth/ended-sessions`, which the
// proxy never forwards). Shared by the session check (auth.ts), the proxy and
// the back-channel logout route.

import { createEndedSessionsStore, type EndedRecord } from "@/lib/endedSessions";

const BACKEND_BASE = process.env.BACKEND_BASE_URL ?? "http://localhost:8000";
const BACKEND_SHARED_SECRET = process.env.BACKEND_SHARED_SECRET ?? "";
// Well under a request's patience: a slow store keeps the copy it has.
const TIMEOUT_MS = 2000;

function headers(): Record<string, string> {
  const out: Record<string, string> = { "content-type": "application/json" };
  if (BACKEND_SHARED_SECRET) out["x-api-key"] = BACKEND_SHARED_SECRET;
  return out;
}

export const endedSessions = createEndedSessionsStore({
  load: async () => {
    const res = await fetch(`${BACKEND_BASE}/auth/ended-sessions`, {
      headers: headers(),
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!res.ok) throw new Error(`ended sessions: ${res.status}`);
    return (await res.json()) as EndedRecord[];
  },
});

/** Record a session the provider ended, in the API's store. "replayed" when
 * the store took this token's `jti` already. */
export async function recordEndedSession(
  record: EndedRecord & { jti: string },
): Promise<"recorded" | "replayed" | "failed"> {
  try {
    const res = await fetch(`${BACKEND_BASE}/auth/ended-sessions`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(record),
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (res.status === 409) return "replayed";
    return res.ok ? "recorded" : "failed";
  } catch {
    return "failed";
  }
}
