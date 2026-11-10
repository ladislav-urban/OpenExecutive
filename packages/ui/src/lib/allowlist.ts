// Who may sign in. Two ADDITIVE sources, never one overriding the other
// (issue #132): the operator's ALLOWED_EMAILS env list is always honored, and
// the backend People roster grants access on top of it.
//
// The previous rule made the roster authoritative the moment it held a single
// email, so anything that wrote a Person row — including a fixture load, which
// wipes `people` and inserts its own @example.com addresses — silently evicted
// the operator from their own instance, with recovery requiring direct DB
// access. Under the union that cannot happen: an env entry is revocable only
// by editing the env.
//
// No imports — not React, not next-auth — so `npm test` can exercise this
// directly under `node --experimental-strip-types` (see
// scripts/allowlist.test.mjs).
//
// The roster loader lives here rather than in its own module even though the
// decision logic below is pure and the loader is the one piece with a socket,
// a clock and mutable state. Splitting them would need the loader to import
// `normalizeEmail` at runtime, and Node's ESM loader requires an explicit
// `.ts` on that relative import while TypeScript rejects it without
// `allowImportingTsExtensions`. That flag does work (it is compatible with
// this project's `noEmit`), so this is a preference, not a hard block: one
// cohesive 260-line module beat adding a compiler option and the only
// `.ts`-suffixed import in the codebase. Revisit if this file grows again.

/** Where an allow/deny decision came from. Recorded in audit-log `details`. */
export type AllowSource =
  /** Matched ALLOWED_EMAILS. The roster was not consulted. */
  | "env"
  /** Missed ALLOWED_EMAILS, matched the People roster. */
  | "roster"
  /** Definite miss: both lists were readable and neither held the email. */
  | "no_match"
  /** Missed ALLOWED_EMAILS; the roster was unreadable, so membership is unknown. */
  | "env_only_roster_unavailable";

// `readonly` + the frozen ENV_HIT below are load-bearing, not decoration:
// both decision functions return that one shared object by reference, so an
// accidental `decision.allowed = false` in a caller would otherwise poison
// every later env hit in the process — the exact #132 lockout class this
// module exists to prevent. Frozen, the stray write throws instead.
export type AllowDecision = {
  readonly allowed: boolean;
  readonly source: AllowSource;
  /** True only when the answer is "no" AND the roster could not be read. */
  readonly rosterUnknown: boolean;
};

export function normalizeEmail(email: string | null | undefined): string {
  return (email ?? "").trim().toLowerCase();
}

/**
 * Parse a comma-separated ALLOWED_EMAILS value. Trims, lowercases, drops
 * blanks — so whitespace around entries and trailing commas are harmless,
 * as docs/auth.md promises.
 */
export function parseAllowedEmails(
  raw: string | null | undefined,
): ReadonlySet<string> {
  return new Set(
    (raw ?? "")
      .split(",")
      .map(normalizeEmail)
      .filter((e) => e.length > 0),
  );
}

/**
 * The one env-match rule, shared by `decideAllowed` and `resolveAllowed` so the
 * two can never drift. The emptiness guard matters because this is exported
 * surface: a caller can hand in a set built by hand rather than by
 * `parseAllowedEmails`, and `""` must never match `""`.
 */
function matchesEnv(normalized: string, envAllowed: ReadonlySet<string>): boolean {
  return normalized.length > 0 && envAllowed.has(normalized);
}

/** The allow decision for an env hit. One object literal, one place, frozen. */
const ENV_HIT: AllowDecision = Object.freeze({
  allowed: true,
  source: "env",
  rosterUnknown: false,
} as const);

/**
 * The union rule, as a pure function. `roster === null` means the roster could
 * not be read; an empty set means it was read and is genuinely empty.
 *
 * Note that an env hit yields `rosterUnknown: false` even when the roster is
 * unreadable — the decision never consulted the roster, so there is nothing
 * unknown about it.
 */
export function decideAllowed(
  email: string,
  envAllowed: ReadonlySet<string>,
  roster: ReadonlySet<string> | null,
): AllowDecision {
  const normalized = normalizeEmail(email);
  if (matchesEnv(normalized, envAllowed)) return ENV_HIT;
  if (roster === null) {
    return {
      allowed: false,
      source: "env_only_roster_unavailable",
      rosterUnknown: true,
    };
  }
  if (normalized.length > 0 && roster.has(normalized)) {
    return { allowed: true, source: "roster", rosterUnknown: false };
  }
  // Both lists readable and neither matched — a definite no. An empty roster
  // lands here too, so it revokes rather than failing open.
  return { allowed: false, source: "no_match", rosterUnknown: false };
}

/**
 * `decideAllowed` with the roster fetched lazily: an env hit short-circuits
 * before `loadRoster` is ever called, so an operator listed in ALLOWED_EMAILS
 * signs in with the backend completely down and pays no fetch latency.
 *
 * The flip side is that env-listed users never warm the roster cache, so the
 * first roster-only request after a restart pays the fetch. That is the right
 * trade: it buys the guarantee that the env list works when nothing else does.
 */
export async function resolveAllowed(
  email: string,
  envAllowed: ReadonlySet<string>,
  loadRoster: () => Promise<ReadonlySet<string> | null>,
): Promise<AllowDecision> {
  const normalized = normalizeEmail(email);
  if (matchesEnv(normalized, envAllowed)) return ENV_HIT;
  return decideAllowed(normalized, envAllowed, await loadRoster());
}

/**
 * What the `authorized` callback should do with a decision about an existing
 * session. Split out from auth.ts because auth.ts calls `NextAuth()` at module
 * scope and cannot be imported by the test harness — and this ordering is the
 * security-relevant part: `allowed` is consulted BEFORE `rosterUnknown`, so
 * env and roster members never route through the fail-open branch.
 */
export type SessionAction =
  /** On the allowlist right now. */
  | "allow"
  /** Not on it, but the roster is unreadable — keep the already-vetted session. */
  | "allow_roster_unknown"
  /** Definite miss. Bounce them on this request. */
  | "revoke";

export function decideSessionAction(decision: AllowDecision): SessionAction {
  if (decision.allowed) return "allow";
  if (decision.rosterUnknown) return "allow_roster_unknown";
  return "revoke";
}

/**
 * The allow-list decision for an existing session. Like `resolveAllowed`,
 * except that a session signed in after this loader's roster was read is not
 * revoked on that older roster: the roster is read once more first.
 *
 * Each Next.js bundle (the middleware's, the server's) has its own loader, so
 * the read that sign-in made after admitting someone doesn't reach the one
 * `authorized` uses. Without this, a person admitted at sign-in would be
 * revoked by the middleware's older roster until it expired, bouncing between
 * `/` and `/signin`. After the re-read the cache is newer than the sign-in,
 * so a session causes at most one (sign-in times are whole seconds, so a
 * re-read in the same second may repeat within that second).
 */
export async function resolveSessionAllowed(
  email: string,
  envAllowed: ReadonlySet<string>,
  loadRoster: RosterLoader,
  signedInAtS: number | null,
): Promise<AllowDecision> {
  const decision = await resolveAllowed(email, envAllowed, loadRoster);
  if (decideSessionAction(decision) !== "revoke" || signedInAtS === null) return decision;
  const readAt = loadRoster.fetchedAt();
  if (readAt !== null && readAt >= (signedInAtS + 1) * 1000) return decision;
  const fresh = await loadRoster.refresh();
  return decideAllowed(email, envAllowed, fresh?.emails ?? null);
}

/** Human clause for audit summaries. Mirrored in docs/auth.md's Debugging table. */
export function describeDenial(source: AllowSource): string {
  switch (source) {
    case "no_match":
      return "not in ALLOWED_EMAILS or the people roster";
    case "env_only_roster_unavailable":
      return "not in ALLOWED_EMAILS; people roster unavailable";
    case "env":
    case "roster":
      // Unreachable from either callback: these are allow outcomes. The
      // `never` assignment makes adding an AllowSource a build error here
      // rather than a silent fall-through to a generic clause.
      return "not allowed";
    default: {
      const unhandled: never = source;
      return unhandled;
    }
  }
}

// --------------------------------------------------------------------------
// Roster fetching: the one impure part of this module.
// --------------------------------------------------------------------------

export type RosterLoaderOptions = {
  baseUrl: string;
  sharedSecret: string;
  ttlMs: number;
  /**
   * Abandon a roster fetch after this long. Required because callers share one
   * in-flight request: without it, undici only gives up at its default
   * ~300s header timeout, so a hung backend would pin every joiner to the same
   * doomed attempt and keep denying new roster-only sign-ins long after the
   * backend recovered — the opposite of "a failure is not cached".
   */
  timeoutMs: number;
  fetchImpl: typeof fetch;
  now?: () => number;
  onWarn?: (message: string) => void;
};

/** One read of GET /auth/allowed-emails: the emails, and the rows as the API
 *  sent them (sign-in reads each managed person's identity from them). */
export type RosterRead = {
  readonly emails: ReadonlySet<string>;
  readonly rows: readonly unknown[];
};

/** The roster, cached for its TTL; `refresh()` reads it anew. */
export type RosterLoader = {
  (): Promise<ReadonlySet<string> | null>;
  /**
   * Read the roster now, after any fetch already in flight, and replace the
   * cache with it: the sign-in that just had someone admitted calls it, so
   * that sign-in sees the new Person. (`authorized` runs in the middleware's
   * bundle, with a loader of its own: `resolveSessionAllowed` covers it.)
   * Null on failure, which leaves the cache as it was (and, as ever, never
   * served past its TTL).
   */
  refresh(): Promise<RosterRead | null>;
  /** When the cached roster's read started (ms, the loader's clock), or null
   *  before any read succeeded. */
  fetchedAt(): number | null;
};

/**
 * Build a cached loader for GET /auth/allowed-emails. The cache and the
 * in-flight promise live in the returned closure, so there is one per Next.js
 * server instance.
 *
 * This is called from the `authorized` callback, which the middleware runs on
 * essentially every non-asset request — not just at sign-in. So the cache is
 * what keeps a page load from becoming N backend calls, and concurrent callers
 * share a single in-flight fetch rather than each opening their own.
 *
 * Returns `null` on any failure, which callers read as "roster unknown". A
 * failure is deliberately NOT cached, so the next attempt retries rather than
 * pinning a roster-only user out for a TTL after a blip; and a stale success is
 * deliberately NOT served past its TTL, because "serve stale on refresh
 * failure" is a different revocation contract than the one docs/auth.md states.
 */
export function createRosterLoader(opts: RosterLoaderOptions): RosterLoader {
  const { baseUrl, sharedSecret, ttlMs, timeoutMs, fetchImpl } = opts;
  const now = opts.now ?? (() => Date.now());
  const warn = opts.onWarn ?? (() => {});
  let cache: { fetchedAt: number; emails: ReadonlySet<string> } | null = null;
  let inFlight: Promise<RosterRead | null> | null = null;

  async function fetchRoster(at: number): Promise<RosterRead | null> {
    try {
      const headers: Record<string, string> = {};
      if (sharedSecret) headers["x-api-key"] = sharedSecret;
      const res = await fetchImpl(`${baseUrl}/auth/allowed-emails`, {
        headers,
        // Don't let a stale Next.js fetch cache gate access.
        cache: "no-store",
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!res.ok) {
        warn(
          `[auth] roster fetch failed (HTTP ${res.status}); ALLOWED_EMAILS still applies, ` +
            `roster-only sign-ins are denied until the backend answers`,
        );
        return null;
      }
      // Parsing stays inside the try: a malformed body degrades to "unknown"
      // rather than throwing into NextAuth's callback.
      const body = (await res.json()) as unknown;
      if (!Array.isArray(body)) {
        warn(
          `[auth] roster fetch returned a non-array body; treating the roster as unavailable`,
        );
        return null;
      }
      const emails: ReadonlySet<string> = new Set(
        body
          // One row with a null email must not null out the whole roster.
          .filter((r): r is { email: string } => typeof r?.email === "string")
          .map((r) => normalizeEmail(r.email))
          .filter((e) => e.length > 0),
      );
      // `at` is the PRE-fetch timestamp, so the effective TTL is shortened by
      // the fetch duration — conservative, and deliberate. Writing it
      // unconditionally is safe only because `loadRoster` keeps at most one
      // fetch in flight: without that, an earlier-started but slower fetch
      // could resolve last, clobber a fresher roster and stamp it with the
      // older `fetchedAt`, keeping an archived Person admitted for up to an
      // extra TTL window. `refresh()` keeps that: it starts only after the
      // fetch in flight has settled.
      cache = { fetchedAt: at, emails };
      return { emails, rows: body };
    } catch (err) {
      warn(
        `[auth] roster fetch error; ALLOWED_EMAILS still applies, ` +
          `roster-only sign-ins are denied until the backend answers: ${String(err)}`,
      );
      return null;
    }
  }

  const emailsOf = (read: RosterRead | null) => read?.emails ?? null;

  function loadRoster(): Promise<ReadonlySet<string> | null> {
    const at = now();
    // `age >= 0` matters: a backwards clock step makes the delta negative,
    // which would otherwise read as "fresh" and serve an expired roster for
    // up to another TTL.
    const age = cache ? at - cache.fetchedAt : Infinity;
    if (cache && age >= 0 && age < ttlMs) return Promise.resolve(cache.emails);
    // Coalesce concurrent misses onto one request. This is what keeps a page
    // load — `authorized` runs per gated request — from fanning out into N
    // backend calls, and it is why at most one fetch is ever in flight.
    // Cleared in `finally`, so a failure is not retained and the next caller
    // retries rather than being pinned out for a TTL.
    if (inFlight) return inFlight.then(emailsOf);
    const pending = fetchRoster(at).finally(() => {
      if (inFlight === pending) inFlight = null;
    });
    inFlight = pending;
    return pending.then(emailsOf);
  }

  // `inFlight` is assigned before any await, so a caller arriving next joins
  // this read rather than starting its own: still at most one fetch in
  // flight, and the fresher result always lands last. Its time is taken when
  // its fetch starts, not when it was asked for. The promise compared in
  // `finally` is the one stored, so it is cleared once settled.
  function refresh(): Promise<RosterRead | null> {
    const next = (inFlight ?? Promise.resolve(null)).then(() => fetchRoster(now()));
    const tracked: Promise<RosterRead | null> = next.finally(() => {
      if (inFlight === tracked) inFlight = null;
    });
    inFlight = tracked;
    return tracked;
  }

  return Object.assign(loadRoster, { refresh, fetchedAt: () => cache?.fetchedAt ?? null });
}
