// Which provider sessions have ended (lib/backchannel.ts), as every request
// checks them: a session whose `sid` ended, or whose person (`sub`) was
// signed out after it began, is signed out here too (docs/auth.md, "Signing
// out"). The ended sessions live in the API's store (`/auth/ended-sessions`)
// so every server of this app sees them; each server keeps a copy and asks
// for what is new at most every REFRESH_MS, so the check stays a lookup.
// Tested in scripts/endedSessions.test.mjs.
//
// No imports, so `npm test` can exercise this.

export interface EndedRecord {
  iss: string;
  sid?: string | null;
  sub?: string | null;
  /** When it ended, in seconds since the epoch. */
  ended_at: number;
}

/** The provider session a session of this app stands on. */
export interface ProviderSession {
  iss: string;
  sid?: string | null;
  sub?: string | null;
  /** When this app's session began, in seconds since the epoch. */
  signedInAt: number;
}

export const REFRESH_MS = 5_000;
// How long an ended session matters: longer than any session here lasts (24h).
export const KEEP_S = 2 * 24 * 60 * 60;

/** Whether `session` has ended, by the records given. */
export function sessionEnded(session: ProviderSession, ended: readonly EndedRecord[]): boolean {
  return ended.some(
    (e) =>
      e.iss === session.iss &&
      // A provider's logout ends its sid outright. A sign-out from a host's
      // page (app/signout/sso) records the sid with its person, and ends only
      // the sessions on it that began by then: the provider session may live
      // on, and signing in again on it must work.
      ((e.sid && session.sid && e.sid === session.sid && (!e.sub || session.signedInAt <= e.ended_at)) ||
        // A whole person signed out: every session of theirs that began
        // before the provider said so.
        (!e.sid && e.sub && session.sub && e.sub === session.sub && session.signedInAt <= e.ended_at)),
  );
}

export interface EndedSessionsStore {
  /** Whether this session has ended, as far as the last refresh knows. */
  isEnded(session: ProviderSession): Promise<boolean>;
  /** Note one that ended here, before the next refresh brings it. */
  add(record: EndedRecord): void;
}

/**
 * A copy of the ended sessions, refreshed through `load` (the API's list,
 * newest first is not needed) at most every `refreshMs`. A refresh that fails
 * keeps the copy it had: a session the provider ended stays ended, and an
 * unreachable store never signs everyone out.
 */
export function createEndedSessionsStore(opts: {
  load: () => Promise<EndedRecord[]>;
  refreshMs?: number;
  nowMs?: () => number;
}): EndedSessionsStore {
  const refreshMs = opts.refreshMs ?? REFRESH_MS;
  const nowMs = opts.nowMs ?? (() => Date.now());
  let records: EndedRecord[] = [];
  const local: EndedRecord[] = [];
  let loadedAt = -Infinity;
  let inflight: Promise<void> | null = null;

  const refresh = (): Promise<void> => {
    if (!inflight) {
      inflight = opts
        .load()
        .then((fresh) => {
          records = fresh;
          loadedAt = nowMs();
        })
        .catch(() => {
          // Keep what we had; try again on the next request after a while.
          loadedAt = nowMs();
        })
        .finally(() => {
          inflight = null;
        });
    }
    return inflight;
  };

  return {
    async isEnded(session) {
      if (nowMs() - loadedAt >= refreshMs) await refresh();
      return sessionEnded(session, records) || sessionEnded(session, local);
    },
    add(record) {
      const cutoff = nowMs() / 1000 - KEEP_S;
      for (let i = local.length - 1; i >= 0; i--) if (local[i].ended_at < cutoff) local.splice(i, 1);
      local.push(record);
    },
  };
}
