# Plan: send a direct visit to the same screen in the host's desk

Status: plan only, for security review. Nothing here is built yet.

## Goal

When this app is shown inside a host's desk (ERPNext with oe_connector,
`docs/deployment.md`), people should use it there. A direct, top-level visit to
one of this app's pages goes to the same screen in the desk:

| Typed or linked here | Goes to |
|---|---|
| `/` (and its old address `/today`) | the desk's Home screen |
| `/jobs` | `<desk base>/jobs` |
| `/chats/12?x=1` | `<desk base>/chats/12?x=1` |
| any other page `<path>?<query>` | `<desk base><path>?<query>` |

`<desk base>` is the desk route that frames this app's pages, for oe_connector
`https://erp.example.com/desk/<page>`. The desk already maps a path to a native
screen where it has one (Home, Conversations, Watch list), so this app needs to
know only the base.

These are unaffected:
- the framed page;
- this app's own sign-in, sign-out and card pages;
- any other page that has to stand alone (listed below).

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
5. **The path isn't on the stand-alone list** (next section), and the query
   doesn't carry `standalone=1`.

## Pages that stay reachable top level

| Path | Why it stands alone |
|---|---|
| `/api/*` (including `/api/auth/*` and the backend proxy) | Not pages, plus Auth.js's own pages |
| `/signin`, `/signin/done`, `/signin/return`, `/signin/sso` | Sign-in, including the frame's sign-in popup and the desk's one-pass sign-in |
| `/signout/sso` | Sign-out |
| `/cards/*` | A card's own page: where a card is answered top level, with the way back to the desk (`lib/signinReturn.ts`, `hostReturn`) |
| `/embed/*` | Pieces placed on the desk's own screens; framed anyway, listed so a top-level load never bounces |
| `/chats/shared/*` | A shared chat's read-only page, which may be opened by someone with no desk |

`standalone=1` covers the one case that isn't a fixed path: the frame's confirm
fallback (`components/embed/FrameConfirm.tsx`, `lib/cardActions.ts`
`ownPageFor`). When the browser can't show that the frame is seen, the person
answers on this app's own page, top level, and that page may be the one that
asked, which is any path. `ownPageFor` adds `standalone=1` to the link it gives.

The stand-alone list lives next to `SESSION_FREE_PATH` in
`lib/middlewarePaths.ts`. It's anchored the same way: exact paths or whole
segments, so `/signin-help` or `/cardsX` would still be redirected. A test pins
every entry and its near misses.

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
screen (`components/embed/EmbeddedShell.tsx`).

## Where it runs

In `src/middleware.ts`, as a new first step for pages: after the backend
proxy's own branch and after `/api/*` returns, before the session check.

Before the session check, so someone who isn't signed in here goes straight to
the desk, which signs them in. They don't stop at this app's sign-in first.
That gives nothing away: the target is the same for everyone, and it depends
only on the path and query that were asked for.

The redirect itself:
- **Status and target:** `302` to `<desk base><path>?<query>`, with
  `embed=1` and `standalone` removed from the query.
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
  (`standalone=1`, or a browser without fetch metadata), as they can today.
  Each page keeps its own session check and route rules. The redirect only
  changes where a normal visit lands.
- **No open redirect.** The target's origin and base come only from
  `OE_DESK_BASE`, checked at startup against the host list. The request supplies
  only a path and query, checked as above, so it can't name another host.
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
  - dropping `embed=1`;
  - `//`, `..` and encoded separators;
  - the length caps;
  - non-GET methods.
- **Startup parsing of `OE_DESK_BASE`:** valid; invalid; an origin not on the host
  list; unset.
- **`ownPageFor`** adds `standalone=1`. The existing confirm tests are updated.
- **A parity check** that the stand-alone list covers every route under
  `src/app` that a session-free path or a top-level link opens.

## Rollout

- Ships in W6 or later, off by default. The operator sets `OE_DESK_BASE` on the
  pilot in a deploy window.
- To roll back, unset it and restart.
- **Day-of check:**
  - a typed address of a page opens it in the desk;
  - the desk's own screens still load;
  - signing in from a fresh browser ends in the desk;
  - a card's "open this page" link still opens the card top level.

## Open questions

1. **Home flash:** the desk frames `/` first and only then opens its native Home.
   Should oe_connector open a native screen directly when its hosted page is
   asked for a native path, before framing? That's an oe_connector change, and
   optional. Without it, Home shows after a short frame load.
2. **Other stand-alone pages:** are there more than those listed? For example
   `/onboard` before a desk exists, or `/demo`.
