// Sign-in for people the identity provider's directory manages
// (integrations/directory_sync.py on the API).
//
// While the directory sync is on, GET /auth/allowed-emails names each managed
// Person's `directory_subject`: the provider's id for them. Such a person
// signs in only through that provider, and only as that identity:
//
// - a Google sign-in with their email is refused (decided Oct 23, 2026 in
//   oe_connector's docs/directory-sync-plan.md, step 7): removing someone in
//   the provider must not leave Google as a way back in;
// - an SSO sign-in whose `sub` isn't theirs is refused: an email the provider
//   reassigned to someone else before the next sync never signs in as the
//   person who had it.
//
// Everyone else, the break-glass ALLOWED_EMAILS address among them, signs in
// as before. No imports, so `npm test` runs it directly
// (scripts/directory.test.mjs).

/** Why a sign-in is refused; recorded as the audit `reason`. */
export type DirectoryRefusal = "directory_managed" | "directory_subject_mismatch";

/** `email → subject` for every Person the directory manages, from the body
 *  of GET /auth/allowed-emails. Rows without a subject are left out. */
export function directorySubjects(body: unknown): ReadonlyMap<string, string> {
  const out = new Map<string, string>();
  if (!Array.isArray(body)) return out;
  for (const row of body) {
    const email = typeof row?.email === "string" ? row.email.trim().toLowerCase() : "";
    const subject = typeof row?.directory_subject === "string" ? row.directory_subject : "";
    if (email && subject) out.set(email, subject);
  }
  return out;
}

/** Why this sign-in must be refused, or null. `ssoProviderId` is the id of the
 *  SSO provider the directory belongs to. */
export function directoryRefusal(
  sign: { provider: string; email: string; sub: string | null | undefined },
  subjects: ReadonlyMap<string, string>,
  ssoProviderId: string,
): DirectoryRefusal | null {
  const subject = subjects.get(sign.email.trim().toLowerCase());
  if (subject === undefined) return null;
  if (sign.provider !== ssoProviderId) return "directory_managed";
  return sign.sub === subject ? null : "directory_subject_mismatch";
}
