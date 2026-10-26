// The paths src/middleware.ts lets through without a session: Auth.js's own
// routes, exactly `/signin`, and exactly `/signin/sso`, where a host's page
// sends someone with no session to get one (app/signin/sso), each with an
// optional trailing slash. Auth.js's routes end at a segment boundary and the
// sign-in pages are exact matches, so a future `/api/authorizations`,
// `/signin-help` or `/signin/return` stays gated
// (scripts/middleware-matcher.test.mjs).
//
// No imports, so `npm test` can exercise it.
export const SESSION_FREE_PATH = /^\/(?:api\/auth(?:\/|$)|signin(?:\/sso)?\/?$)/;
