# Plan: send a direct visit to the same screen in the host's desk

Status: plan only, for security review. Nothing here is built yet.

## Goal

When this app is shown inside a host's desk (ERPNext with oe_connector,
`docs/deployment.md`), people should use it there. A direct, top-level visit to
one of this app's pages goes to the same screen in the desk:

| Typed or linked here | Goes to |
|---|---|
| `/` (and its old address `/today`) | the desk's Home screen |
| `/jobs` (the desk's "Work" entry) | `<desk base>/jobs` |
| `/chats/12?find=x` | `<desk base>/chats/12?find=x` |
| any other page `<path>?<query>` | `<desk base><path>?<allowed query>` |

`<desk base>` is the desk route that frames this app's pages, for oe_connector
`https://erp.example.com/desk/<page>`. The desk already maps a path to a native
screen where it has one (Home, Conversations, Watch list), so this app needs to
know only the base.

These are unaffected:
- the framed page;
- the pages that have to stand alone (listed below).

Off unless configured, so a deployment with no host is unchanged.

## What counts as a direct visit

The redirect applies only when all of these hold:

1. **The feature is on:** `OE_DESK_BASE` is set and valid (see Configuration).
2. **It's a GET or HEAD.** Anything else passes through: a form post, an API call.
3. **It's a top-level document load:** `Sec-Fetch-Dest: document` and
   `Sec-Fetch-Mode: navigate`.
   - The desk's frame loads with `Sec-Fetch-Dest: iframe`, and so does every
     navigation inside the frame, so the framed page is never redirected.
   - Next's client navigations and prefetches are `fetch` requests (with
     `RSC: 1`), not documents, so they are never redirected.
   - A browser that sends no fetch metadata is not redirected, as today.
4. **It's not a prerender or prefetch:** neither `Sec-Purpose` nor `Purpose`
   names one.
5. **The path is a page, not a route handler, and isn't on the stand-alone
   list** (next section). The query doesn't carry `standalone=1`.
6. **The query holds no held-back key** (see Query parameters). A link that
   carries one opens here, as today.

## Pages that stand alone

**The rule:** a page stands alone if someone reaches it before they have a desk
session, or if it must not run inside the desk.

| Path | Why it stands alone |
|---|---|
| `/api/*` (Auth.js's routes, its error page, the backend proxy) | Auth.js's own pages, including its error page; the rest are not pages |
| `/signin`, `/signin/done`, `/signin/return`, `/signin/sso` | Sign-in: reached with no session, and the frame's popup and the desk's one-pass sign-in run top level |
| `/signout/sso` | Sign-out: run top level, with or without a session |
| `/cards/*` | A card's own page: where a card is answered top level, with the way back to the desk (`lib/signinReturn.ts`, `hostReturn`) |
| `/embed/*` | Pieces placed on the desk's own screens; framed anyway, listed so a top-level load never bounces |
| `/chats/shared/*` | A shared chat's read-only page, opened from a link by someone who may have no desk |

**Route handlers** (every `route.ts` under `src/app`) stand alone by default. A
top-level GET to a handler that returns a file (an export, a download) is a
document navigation, and sending it into the desk's frame would break the
download. Today every handler is under `/api/*`, `/signin/*` or `/signout/*`. The
parity check fails on a new handler outside that list until it's added, or
until it's marked to be redirected and the review says why.

**Checked against the rule** (code at the time of writing):

| Path or kind | Result | Why |
|---|---|---|
| `/onboard` | Redirected | The middleware already requires a session for it, and since ladislav-urban/OpenExecutive#279 it's the principal's. It's reached from Home's first-run redirect (`components/onboard/useFirstRunRedirect.ts`) and Settings, after sign-in. It runs in the frame as a bare page (`AppShell` `EXEMPT_PREFIXES`). oe_connector lists it with its own help (`navigation.py`), so it's meant to run in the desk. |
| `/demo` | Redirected | The principal's fixtures page (load, reset, generate), behind a session. Since ladislav-urban/OpenExecutive#279 it's served only with `OE_FIXTURES_ENABLED`, and never with `OE_PUBLIC_DEPLOYMENT`. It's no entry point for demo visitors. |
| Sign-up, registration, a demo visitor's first landing | Not in this app | There's no sign-up or registration route under `src/app`. Accounts are made and signed in at the identity provider, and demo visitors start in the desk. If this app ever adds one, the rule puts it on the list. |
| Invite, verify, magic-link, password or key setup, unsubscribe | None exist | No such route under `src/app`. `/signin/sso`'s `hint` and `failed` are on the list already. |
| Error pages | Covered | Auth.js's error page is under `/api/auth`, and its errors return to `/signin`. There's no custom `not-found` or `error` route. An unknown path is redirected and shows its 404 in the desk's frame, signed in or not. |
| Email and chat links | Covered | The links the backend writes are cards (`/cards/*`, on the list), shared chats (on the list), and pages behind a session, such as `/jobs/<name>` from a chat. A link to a page behind a session, opened signed out, goes to the desk, which signs the person in and frames the page. A workflow link with `prefill` isn't redirected (next section). |

**Kept honest:**
- The stand-alone list lives next to `SESSION_FREE_PATH` in
  `lib/middlewarePaths.ts`. It's anchored the same way: exact paths or whole
  segments, so `/signin-help` or `/cardsX` would still be redirected. A test pins
  every entry and its near misses.
- A parity test walks every `page.tsx` and `route.ts` under `src/app`. Each must
  be on the stand-alone list, or in a named list of redirected pages. A new
  route fails the test until someone puts it on one of the two.

`standalone=1` covers the one case that isn't a fixed path: the frame's confirm
fallback (`components/embed/FrameConfirm.tsx`, `lib/cardActions.ts`
`ownPageFor`). When the browser can't show that the frame is seen, the person
answers on this app's own page, top level, and that page may be the one that
asked, which is any path. `ownPageFor` adds `standalone=1` to the link it gives.

**`standalone=1` is read by the middleware alone.** No page, component or hook
reads it, so it can't become a second switch for embed mode, chrome or gates. A
test greps `src/` for any other reader. The middleware doesn't strip it from the
page's URL; the page ignores it.

## Query parameters

The desk is another host, with its own access logs, request log and route
history, and it puts the framed page's URL in its frame. So a query is never
forwarded whole.

- **Forwarded (allowlist, default deny):** only keys known to be screen state:
  `browse`, `designer`, `edit`, `event_type`, `find`, `for`, `mode`, `new`, `q`,
  `saved`, `section`, `session` (a chat's id), `status`, `tab`, `view`.
  - These are the keys this app's pages read from their query today, minus the
    held-back ones below.
  - Each value is capped in length.
  - Every other key is dropped, including `embed` and `standalone`.
- **Held back: the link isn't redirected at all.** It opens here, as today, so
  none of it reaches the desk. That applies when the query holds:
  - a key that carries a secret, a one-time value or someone's own words:
    `to`, `callbackUrl`, `hint`, `login_hint`, `prefill`, `draft`, `describe`;
  - any key off the allowlist whose name contains `token`, `code`, `state`,
    `key`, `sig`, `secret`, `password`, `otp`, `nonce`, `ticket` or `auth`, in
    any case. The allowlist is matched first and exactly: `designer` contains
    `sig` but is forwarded. Every other key off the allowlist is dropped.
- **Kept in step:** a test reads every query key this app's pages and
  components read (`searchParams.get`, `URLSearchParams(...).get`, and the
  like). Each must be on the allowlist or on the held-back list. A new key fails
  the test until someone decides which.

## Configuration

`OE_DESK_BASE` (UI, server-only, read once at startup like `OE_HOST_ORIGINS`):
the desk route that frames this app's pages, for example
`https://erp.example.com/desk/<page>`.

At startup it must pass `lib/embed.ts`'s `parseReturnBase`, the check a host's
`returnBase` already passes:
- https or http;
- no user or password;
- a plain `/desk/…` or `/app/…` path;
- no query, no fragment, no encoded separator.

Its origin must also be one of `OE_HOST_ORIGINS`. If it fails, the server logs
why and the feature stays off, as an invalid `OE_HOST_ORIGINS` already does.

The desk's Home screen is the base itself. The desk shows a framed `/` natively
as Home: the frame reports `/` as a native path, and the desk opens its Home
screen (`components/embed/EmbeddedShell.tsx`). Home appears after a short frame
load. That's accepted for this step.

## Where it runs

In `src/middleware.ts`, as a new first step for pages: after the backend
proxy's own branch and after `/api/*` returns, before the session check.

Before the session check, so someone who isn't signed in here goes straight to
the desk, which signs them in. They don't stop at this app's sign-in first.
That gives nothing away: the target is the same for everyone, and it depends
only on the path and the allowed query that were asked for.

The redirect itself:
- **Status and target:** `302` to `<desk base><path>?<allowed query>`.
- **`/` and `/today`:** `/` goes to `<desk base>`. `/today` needs nothing of
  its own: `next.config.ts` aliases run before the middleware, so it becomes
  `/` first, and the same goes for every old address in `lib/pageAliases.ts`.
- **Path checks:**
  - The path is the request's own pathname. It's checked as
    `lib/signinReturn.ts` checks a return target: it starts with one `/`, has no
    `//`, no `..` segment and no encoded `.`, `/` or `\`.
  - Anything else isn't redirected. The page loads as today.
- **Length limit:** path and query are capped, to fit the desk's own limits
  (oe_connector `embed.frame_src`). A longer one isn't redirected.
- **Headers:**
  - `Cache-Control: no-store`;
  - `Vary: Sec-Fetch-Dest, Sec-Fetch-Mode`, so no cache between the browser and
    this app hands a framed load the redirect, or a top-level load the page;
  - `Referrer-Policy: same-origin`.

## Security notes

- **Not an access control.** Anyone can still reach any page top level
  (`standalone=1`, a held-back key, or a browser without fetch metadata), as
  they can today. Each page keeps its own session check and route rules. The
  redirect only changes where a normal visit lands.
- **No open redirect.** The target's origin and base come only from
  `OE_DESK_BASE`, checked at startup against the host list. The request supplies
  only a path and allowed query keys, checked as above, so it can't name another
  host.
- **Nothing secret reaches the desk:**
  - only allowlisted screen-state keys are forwarded;
  - a link with a secret, a one-time value or someone's own words isn't
    redirected at all.
- **Clickjacking defence unchanged.** The frame's confirm fallback still reaches
  this app's own page top level through `standalone=1`. Cards keep their own
  page. Nothing here changes `frame-ancestors`.
- **No loop:**
  - The desk frames this app (`Sec-Fetch-Dest: iframe`), which is never
    redirected.
  - Top-level sign-in and sign-out pages are on the stand-alone list.
  - If the desk is misconfigured and loads a page top level, it gets one
    redirect to the desk. The desk then frames it, which isn't redirected again.
- **Sign-in flows:** the one-pass sign-in (`/signin/sso`), the popup
  (`/signin?callbackUrl=/signin/done`), the return (`/signin/return`) and
  Auth.js's callback (`/api/auth/*`) are all on the list. `returnTarget` and the
  replace mark are unchanged.

## Tests (when built)

- **Middleware helper**, a pure function in `lib/`, tested by `npm test`. It
  takes method, path, query and fetch-metadata headers, and returns the target
  or null. It covers:
  - every stand-alone entry and its near misses;
  - each `Sec-Fetch-Dest` value, with metadata missing, and with prefetch and
    prerender;
  - `standalone=1`;
  - the allowlist: a mix of allowed and unknown keys, so only the allowed ones
    reach the target;
  - each held-back key and pattern, in mixed case, so the link isn't
    redirected;
  - dropping `embed=1`;
  - `//`, `..` and encoded separators;
  - the length caps;
  - non-GET methods.
- **Parity tests:**
  - every `page.tsx` and `route.ts` is stand-alone or named as redirected;
  - every query key a page reads is allowed or held back;
  - nothing but the middleware reads `standalone`.
- **Startup parsing of `OE_DESK_BASE`:** valid; invalid; an origin not on the host
  list; unset.
- **`ownPageFor`** adds `standalone=1`. The existing confirm tests are updated.

## Rollout

- Off by default. An operator turns it on by setting `OE_DESK_BASE` in a deploy
  window.
- To roll back, unset it and restart.
- **Day-of check:**
  - a typed address of a page opens it in the desk;
  - the desk's own screens still load;
  - signing in from a fresh browser ends in the desk;
  - a card's "open this page" link still opens the card top level.

## Later, optional

- **No frame load before Home:** oe_connector could open a native screen (Home,
  Conversations, Watch list) directly when its hosted page is asked for that
  path, without framing it first. That's an oe_connector change, not part of
  this plan. Until then, the frame reports the native path and the desk opens
  the screen, as it does today.
