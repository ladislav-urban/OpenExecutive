// Letting in someone the identity provider's directory has just approved
// (integrations/directory_sync.py's `admit_one` on the API; docs/auth.md,
// "Following your identity provider's directory").
//
// The directory sync creates a Person for each new member of its group every
// few minutes, and sign-in admits only People. So a member approved a minute
// ago would be refused until the next run. When the roster refuses an SSO
// sign-in while the sync is on, sign-in asks the API to look that one person
// up at the provider (POST /auth/directory-admit). The API creates their
// Person only if the provider says they are an active member, exactly as the
// next run would; sign-in then reads the roster fresh and decides again with
// the same rules as before. Anyone else is still refused.
//
// No imports, so `npm test` runs it directly (scripts/directoryAdmit.test.mjs).

/** What the API's admit answered (directory_sync.ADMIT_CODES), or why sign-in
 *  got no answer from it. */
export type AdmitCode =
  | "admitted"
  | "exists"
  | "address_held"
  | "unknown"
  | "inactive"
  | "email_mismatch"
  | "not_member"
  | "invalid"
  | "disabled"
  | "busy"
  | "rate_limited"
  | "error"
  // No usable answer: these never come from the API.
  | "http_error"
  | "timeout"
  | "unreachable"
  | "bad_answer";

const API_CODES: ReadonlySet<string> = new Set([
  "admitted",
  "exists",
  "address_held",
  "unknown",
  "inactive",
  "email_mismatch",
  "not_member",
  "invalid",
  "disabled",
  "busy",
  "rate_limited",
  "error",
]);

export type AdmitAnswer = { readonly admitted: boolean; readonly code: AdmitCode };

/** The reason /signin is sent for "we're still setting up your access". */
export const WAITING_REASON = "directory_waiting";
/** Where a refused sign-in that may work in a few minutes goes: relative, and
 *  nothing in it comes from the request. */
export const WAITING_REDIRECT = `/signin?error=AccessDenied&reason=${WAITING_REASON}`;

// Answers that may turn into a yes in a few minutes: not in the group yet
// (they may be approved shortly, or a new subgroup isn't known yet), or the
// API was busy or rate limited.
const WAITING: ReadonlySet<AdmitCode> = new Set(["not_member", "busy", "rate_limited"]);

/** Whether `DIRECTORY_SYNC_ENABLED` (the API's setting, mirrored to the web
 *  app) is on. The API's own check of it stays the one that counts. */
export function directorySyncOn(raw: string | null | undefined): boolean {
  return ["1", "true", "yes", "on"].includes((raw ?? "").trim().toLowerCase());
}

/**
 * Whether a refused sign-in may ask the API to admit them: an SSO sign-in
 * (never Google or local login), with the directory sync on, refused by the
 * roster rather than matched by ALLOWED_EMAILS, and naming the provider's
 * id for them. The email was verified before any of this (that check comes
 * first in `signIn`).
 */
export function admitEligible(sign: {
  provider: string;
  ssoProviderId: string;
  syncOn: boolean;
  allowed: boolean;
  source: string;
  sub: unknown;
}): boolean {
  return (
    sign.syncOn &&
    sign.provider === sign.ssoProviderId &&
    !sign.allowed &&
    sign.source !== "env" &&
    typeof sign.sub === "string" &&
    sign.sub.length > 0
  );
}

export type AdmitRequestOptions = {
  baseUrl: string;
  sharedSecret: string;
  timeoutMs: number;
  fetchImpl: typeof fetch;
};

/** Ask the API to admit this one person. Never throws: no usable answer is
 *  a refusal with a code of its own. */
export async function requestAdmit(
  subject: string,
  email: string,
  opts: AdmitRequestOptions,
): Promise<AdmitAnswer> {
  try {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (opts.sharedSecret) headers["x-api-key"] = opts.sharedSecret;
    const res = await opts.fetchImpl(`${opts.baseUrl}/auth/directory-admit`, {
      method: "POST",
      headers,
      body: JSON.stringify({ subject, email }),
      cache: "no-store",
      signal: AbortSignal.timeout(opts.timeoutMs),
    });
    if (res.status !== 200) return { admitted: false, code: "http_error" };
    const body = (await res.json()) as { admitted?: unknown; code?: unknown } | null;
    const code = typeof body?.code === "string" && API_CODES.has(body.code) ? (body.code as AdmitCode) : null;
    if (code === null) return { admitted: false, code: "bad_answer" };
    // Both must say so: a body that says admitted with another code isn't one.
    return { admitted: body?.admitted === true && code === "admitted", code };
  } catch (err) {
    const name = (err as { name?: unknown } | null)?.name;
    return { admitted: false, code: name === "TimeoutError" || name === "AbortError" ? "timeout" : "unreachable" };
  }
}

export type AdmitOutcome =
  | { readonly allow: true; readonly code: AdmitCode }
  | {
      readonly allow: false;
      readonly code: AdmitCode;
      /** The audit reason: why sign-in still refused them. */
      readonly reason: string;
      /** Whether to tell them it may work in a few minutes. */
      readonly waiting: boolean;
    };

/**
 * After the roster refused an eligible sign-in: ask the API to admit them,
 * and when it did (or their Person already exists, and the roster this
 * server holds may be older), read the roster fresh and decide again.
 * `recheck` applies sign-in's own rules to the fresh read (the allow-list,
 * then the directory's identity check) and answers null to let them in, or
 * the refusal's reason. Anything else refuses, as before.
 */
export async function admitAfterDenial<R>(
  sign: { subject: string; email: string },
  deps: {
    admit: (subject: string, email: string) => Promise<AdmitAnswer>;
    refresh: () => Promise<R | null>;
    recheck: (fresh: R) => Promise<string | null>;
  },
): Promise<AdmitOutcome> {
  let answer: AdmitAnswer;
  try {
    answer = await deps.admit(sign.subject, sign.email);
  } catch {
    answer = { admitted: false, code: "unreachable" };
  }
  if (!answer.admitted && answer.code !== "exists") {
    return { allow: false, code: answer.code, reason: "not_in_allowlist", waiting: WAITING.has(answer.code) };
  }
  let fresh: R | null;
  try {
    fresh = await deps.refresh();
  } catch {
    fresh = null;
  }
  if (fresh === null) {
    return { allow: false, code: answer.code, reason: "roster_unavailable", waiting: false };
  }
  let refusal: string | null;
  try {
    refusal = await deps.recheck(fresh);
  } catch {
    refusal = "recheck_failed";
  }
  if (refusal !== null) return { allow: false, code: answer.code, reason: refusal, waiting: false };
  return { allow: true, code: answer.code };
}
