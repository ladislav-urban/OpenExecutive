// The paths src/middleware.ts lets through without a session: Auth.js's own
// routes and exactly `/signin` (with optional trailing slash). Auth.js's
// routes end at a segment boundary and the sign-in page is an exact match, so
// a future `/api/authorizations` or `/signin-help` stays gated
// (scripts/middleware-matcher.test.mjs).
//
// No imports, so `npm test` can exercise it.
export const SESSION_FREE_PATH = /^\/(?:api\/auth(?:\/|$)|signin\/?$)/;

// The confirmation windows (/confirm and /confirm/alert/<id>): no page may
// frame them, not even a host origin's, since what they confirm must never be
// drawn under someone else's page.
export const NEVER_FRAMED_PATH = /^\/confirm(?:\/|$)/;
