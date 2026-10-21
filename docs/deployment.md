# Deployment

Open Executive ships as two containers — the FastAPI backend and the Next.js UI —
plus one persistent volume. Anything that can run a Docker image and attach a
disk will host it: `docker compose` on a single box, a container platform, or a
Kubernetes deployment.

[docker/docker-compose.yml](../docker/docker-compose.yml) is the reference
topology. It is also what `make docker` runs locally, with one difference: the
compose UI is a `next dev` server on a bind mount, not the production image
from `Dockerfile.ui`.

---

## ⚠️ Single instance only

**The API must run exactly one replica.** The scheduler claims due rows with
`UPDATE … RETURNING`, which is safe against concurrent claims *within* a process
but not across processes: a second API container fires every scheduled action a
second time — duplicate emails, duplicate Slack messages, duplicate briefings.

There is no leader election. Whatever you deploy on, pin the API to one instance
and use a replace-in-place rollout rather than one that briefly runs two
containers. The UI is stateless and scales freely.

---

## Topology

| Component | Dockerfile | Published image | State |
|---|---|---|---|
| API | [docker/Dockerfile](../docker/Dockerfile) | `ghcr.io/sentelabsai/openexecutive-api` | One persistent volume at `/data` |
| UI | [docker/Dockerfile.ui](../docker/Dockerfile.ui) | `ghcr.io/sentelabsai/openexecutive-ui` | Stateless |

The UI never talks to the API directly from the browser. It proxies through its
own server (`/api/backend/*`), stamping the shared secret on each upstream call,
so the UI origin is the only one that *needs* to be public. See [auth.md](auth.md).

> **The compose file binds the API to `127.0.0.1` on purpose.** The UI reaches it
> over the compose network, so nothing needs it on `0.0.0.0`. If you change that
> binding or put the API behind a proxy on its own hostname, you have made it
> internet-reachable — set `BACKEND_SHARED_SECRET` **and** `OE_PUBLIC_DEPLOYMENT=1`
> before you do. Neither is set by default, and without them the API serves every
> route unauthenticated with only a log line to say so.

## Images

[.github/workflows/release-images.yml](../.github/workflows/release-images.yml)
builds both images from the Dockerfiles above, with the repo root as build
context, and pushes them to GitHub Container Registry. Nothing is added or
configured in CI. Note that `make docker` builds only the API image — the
compose file runs the UI as a `next dev` server — so this workflow is the only
thing that builds `Dockerfile.ui`.

| Tag | Set by | Meaning |
|---|---|---|
| `X.Y.Z`, `X.Y` | pushing git tag `vX.Y.Z` | A release. Pin deployments to one of these. |
| `latest` | pushing git tag `vX.Y.Z` | The most recently published release, by push order, not the highest version. Pushing a `v0.2.1` patch after `v0.3.0` moves `latest` back to `0.2.1`. |
| `main` | every push to `main` | Current head of `main`; not a release. |
| `sha-<short>` | every push | The commit the image was built from. |
| `buildcache` | every push | BuildKit layer cache. Not an image; ignore it. |

**Cutting a release** is merging the release PR.
[.github/workflows/release-please.yml](../.github/workflows/release-please.yml)
runs release-please on every push to `main` and keeps one open PR,
"chore(main): release X.Y.Z", up to date. PRs are squash-merged, so each one
lands as a single commit whose subject is the PR title, and the version comes
from those titles' conventional-commit types since the last release. Before 1.0,
`feat` and `fix` both bump the patch version and a breaking change (`!` or a
`BREAKING CHANGE:` footer) bumps the minor; minor versions are kept for
milestones. From 1.0 the usual rule applies: `feat` → minor, `fix` → patch,
breaking → major. The PR bumps
every place the version is written (listed in `release-please-config.json`)
and adds the `CHANGELOG.md` entry, which can be edited in the PR before
merging. Merges that are only `chore`/`docs`/`test`/`refactor` wait for the
next `feat` or `fix`. Merging the release PR tags the merge commit `vX.Y.Z`
and creates the GitHub Release; the tag push then runs the image workflow.
It needs the `RELEASE_PLEASE_TOKEN` repository secret (a fine-grained token
for this repository with Contents and Pull requests read/write), because
events made with the default `GITHUB_TOKEN` start no workflows: without it
the release PR would get no CI and the tag would publish no images.

Pushing a `vX.Y.Z` tag by hand still works (`git tag v0.3.0 && git push
origin v0.3.0`) but bypasses the version bump and changelog, so the release
PR is the normal path.

The image workflow runs on the tag push and publishes the versioned tags. It
does not check CI: it publishes whatever commit the tag points at, and the
`main` tag is published in parallel with CI on every push, so the release PR
should only be merged once its CI is green. The two images are separate jobs,
so a release is not atomic — if one fails, check the package pages and re-run
the failed job from the Actions UI.

Once both images are published, the same run creates the GitHub Release for
the tag if it does not exist yet (release-please normally already made it),
with that version's `CHANGELOG.md` section as the notes. A hand-pushed tag
with no `## [X.Y.Z]` section fails that job instead of publishing an empty
release. If the images fail, no release is
created; re-running the failed job from the Actions UI creates it once the
images succeed. A release that already exists for the tag is left alone.

The images are `linux/amd64` only. The API image bakes the embedding models at
build time (see the Dockerfile), which makes an emulated arm64 build
impractically slow.

> **First publish:** GitHub creates each package as private. An org admin makes
> `openexecutive-api` and `openexecutive-ui` public once under the org's
> Packages settings; until then `docker pull` needs a token with `read:packages`.

---

## Persistent state

One volume, mounted at `/data`:

- `/data/chroma_db/` — ChromaDB vector index (built-in knowledge + uploaded company docs)
- `/data/episodic_memory.db` — SQLite: episodic memory, people, alerts, scheduled actions, audit log
- `/data/company/profile.yaml` + `/data/company/docs/` — onboarding output + uploaded docs
- `/data/company/mcp_servers.json` — MCP gateway config. Placing this file is what **enables** MCP when `MCP_ENABLED` is unset; set `MCP_ENABLED=false` to keep MCP off with the file in place. A config defining no servers under `mcpServers`, or a gateway that fails to start, is logged and skipped — the API boots without MCP tools (and without the email poller) rather than failing to boot.
- `/data/company/tokens` — Bearer tokens for URL MCP servers, one `name=token` per line (the ERPNext service user's key and secret), if any is configured. It sits next to `mcp_servers.json` because the gateway reads it from there
- `/data/google_credentials/` — Google Workspace OAuth token, if that integration is enabled
- `/data/ms365_credentials/` — Microsoft 365 MSAL token cache (+ its `.cache-key`), if that integration is enabled

Nothing hardcodes those paths. Each is an env var, and the defaults are
repo-relative so a local checkout works with no configuration:

```
VECTOR_STORE_PATH             = /data/chroma_db
EPISODIC_DB_PATH              = /data/episodic_memory.db
COMPANY_PROFILE_PATH          = /data/company/profile.yaml
MCP_SERVERS_CONFIG_PATH       = /data/company/mcp_servers.json
WORKSPACE_MCP_CREDENTIALS_DIR = /data/google_credentials
MS365_MCP_CREDENTIALS_DIR     = /data/ms365_credentials
```

On first boot the volume is empty. ChromaDB rebuilds the built-in knowledge index
from files shipped inside the Python package (`openexecutive/knowledge/builtin/`),
and the SQLite database is created on demand. The company profile stays empty
until you run the onboarding wizard against the deployed URL — the wizard failing
with "no company profile" on a fresh volume is expected, not a fault.

---

## Required configuration

| Variable | Why |
|---|---|
| `ANTHROPIC_API_KEY` | Every agent call. The app will not start without it. |
| `BACKEND_SHARED_SECRET` | Gates every API route via `x-api-key`. Generate with `openssl rand -hex 32`; the UI needs the same value. |
| `CALLER_ASSERTION_PUBLIC_KEYS` (API), `CALLER_ASSERTION_PRIVATE_KEY` (UI) | Signed callers: the UI signs who is signed in and the API checks it, so the shared secret alone can't act as the owner. Recommended. Make the pair with `scripts/make-caller-keys.py`; see [auth.md](auth.md#signed-callers). |
| `OE_PUBLIC_DEPLOYMENT=1` | **Set this on every internet-reachable instance.** See below. |
| `BACKEND_ALLOWED_ORIGINS` | Comma-separated UI origins allowed through CORS, e.g. `https://exec.example.com`. With `OE_PUBLIC_DEPLOYMENT` set these are the only ones: the `localhost:3000` origins `make dev` uses are dropped. |
| `OE_HOST_ORIGINS` (UI) | Optional. Origins of other apps that may frame the UI and call its short host list from their pages, e.g. `https://erp.example.com`. Unset, only the UI frames itself. See [below](#showing-open-executive-inside-another-app-oe_host_origins). |
| `AUTH_SECRET`, `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET`, `AUTH_OIDC_ISSUER`, `AUTH_OIDC_ID`, `AUTH_OIDC_SECRET`, `AUTH_URL`, `ALLOWED_EMAILS` | UI sign-in (Google and/or SSO). See [auth.md](auth.md). |

Integrations (Slack, Discord, email, Google Workspace, Microsoft 365) are all
optional and off unless their variables are set. [.env.example](../.env.example) is the full list.

### `OE_PUBLIC_DEPLOYMENT`

If `BACKEND_SHARED_SECRET` is unset the API serves every route unauthenticated.
That is the intended default for local development and a serious incident
anywhere else, so a deployment declares itself public:

```
OE_PUBLIC_DEPLOYMENT=1
```

With it set and no shared secret, `create_app()` raises at startup instead of
booting an open API. The check is deliberately fail-safe — any value other than
`0`/`false`/`no`/`off`/empty arms it, so a typo requires the secret rather than
skipping the check.

### Showing Open Executive inside another app (`OE_HOST_ORIGINS`)

A host app (another web app that frames Open Executive's pages and calls a short list of
its routes, [auth.md](auth.md#host-origins-showing-open-executive-inside-another-app)) is
listed on the **UI** by its exact origin:

```
OE_HOST_ORIGINS=https://erp.example.com
```

Host it like this:

- **A sibling host, one site.** Serve the UI at the root of its own host under the host
  app's registrable domain (`exec.example.com` beside `erp.example.com`), never under a
  path of the host app and never on an unrelated domain: the session cookie is
  `SameSite=Lax`, so a host on another site would get no cookie in its frame or with its
  calls (Safari and Firefox block third-party cookies).
- **Its own server block** (or its own proxy), not a location inside the host app's, with
  its own TLS, buffering off for `/api/backend/` (streamed chat), and a body limit of
  55 MB (the largest upload the API takes, with room). A header added inside a location
  can drop the server's other headers; keep the UI's own `Content-Security-Policy` as it
  sends it.
- **A fixed sign-in address.** Set `AUTH_URL=https://exec.example.com` and leave
  `AUTH_TRUST_HOST` unset, so no request's `Host` header steers where sign-in returns.
  The sign-in provider's client returns to that host.
- **Internal ports stay internal.** The API, the UI's own port and the host app's
  application port are reachable only from their proxies.
- **Nothing else on the domain serves outsiders' content.** Every host under the same
  registrable domain can make same-site requests that carry both apps' cookies. Keep a
  public website, a customer portal or uploaded files off this domain, or accept that a
  script on one of them can make same-site requests to both apps. Each app's own checks
  still stand: the UI's proxy refuses every same-site write except the host list from the
  exact host origin.

Removing an origin from `OE_HOST_ORIGINS` (and restarting the UI) stops that host's
framing and every call from its pages at once. `make embed-outputs` writes the files a
host app checks itself against (`embed-manifest.json`, `host-list.json`,
`openapi.json`).

---

## Health checks

`GET /health` is exempt from the shared-secret gate so a platform health checker
can reach it unauthenticated. It returns:

```json
{"status": "ok", "builtin_knowledge_chunks": 1234, "google_tools_missing": [], "version": "0.1.0"}
```

`google_tools_missing` is the pinned Google tools startup priming could not
find. `[]` means it checked and they are all there — the only value that says
so. A list of names means a `workspace-mcp` update renamed one, or the
configured services and tool tier never loaded it; Settings → Setup status
names them too. `null` means no answer: Google Workspace is not configured, or
the probe has not finished, or it failed. Expect `null` during the startup
grace period below, so alert on a non-empty list rather than on "not `[]`".

**Give it a startup grace period of about 5 minutes.** A cold container builds
the MCP tool-discovery vector index and loads Chroma before it serves. The
embedding model is baked into the image, so no network fetch is involved, but
the work is real — a short grace period will kill the container mid-boot in a
crash loop that looks like a deploy failure.

## Resources

**2 GB of memory for the API.** 1 GB out-of-memories during ingest, where the
ONNX embedder and Chroma writes run concurrently. One shared CPU is sufficient;
the workload is I/O-bound on the Anthropic API.

---

## Google Workspace credentials

The Gmail/Calendar/Drive tools come from `workspace-mcp`, which runs co-located
inside the API as a stdio child of the MCP gateway rather than as its own
service — the product is single-tenant, so the server is inherently one per
install. It is baked into the API image and launched by
[docker/workspace-mcp-launch.sh](../docker/workspace-mcp-launch.sh) from the
`google_workspace` entry in `/data/company/mcp_servers.json` (see
[packages/core/mcp_servers.json.example](../packages/core/mcp_servers.json.example)
for the exact block).

`GWORKSPACE_AUTH_MODE` picks the auth mode; it defaults to `oauth`.

**Option A — `oauth` (single user).** Set `GOOGLE_OAUTH_CLIENT_ID` and
`GOOGLE_OAUTH_CLIENT_SECRET`. The API serves no OAuth callback, so the token has
to be seeded: complete the flow once locally with `WORKSPACE_MCP_CREDENTIALS_DIR`
pointed at a local folder, then copy the credential file onto the volume.

```bash
docker compose exec api mkdir -p /data/google_credentials
docker compose cp <local-credentials-dir>/<token-file> api:/data/google_credentials/
docker compose restart api        # the gateway reads config and credentials at startup
```

**Option B — `service_account` (domain-wide delegation).** No browser flow, but a
Workspace admin must authorize the service account's client ID for the
Gmail/Calendar/Drive scopes:

```
GWORKSPACE_AUTH_MODE=service_account
USER_GOOGLE_EMAIL=exec@yourcompany.com
GOOGLE_SERVICE_ACCOUNT_KEY_JSON=<contents of service-account.json>
```

(`GOOGLE_SERVICE_ACCOUNT_KEY_FILE`, a path to a key already on the volume, works
too. The launcher fails fast if neither the key nor `USER_GOOGLE_EMAIL` is set.)

Outbound egress is gated either way: the Executive can only email, invite, or
share with People on the roster.

Every Google Workspace call acts as the Executive's own account: the gateway
refuses a call whose `user_google_email` names any other address.

#### Narrowing the Google tool surface (optional)

Two variables decide how much of Google Workspace the co-located child loads.
Both default to everything; leave them unset unless you want less.

| Variable | Default | Values |
|---|---|---|
| `WORKSPACE_MCP_TOOLS` | `all` | `all`, or comma-separated service names: `gmail`, `calendar`, `drive`, `docs`, `sheets` (also `chat`, `forms`, `slides`, `tasks`, `contacts`, `search`, `appscript`) |
| `WORKSPACE_MCP_TOOL_TIER` | `complete` | `core`, `extended`, `complete` |

`WORKSPACE_MCP_TOOLS` reaches workspace-mcp as `--tools`, which decides which
service **modules** it imports. A service you leave out is never imported, so
none of its tools exist — not in the index, and not callable. The tier decides
how many tools per service are registered; tiers are cumulative, and `core` is
the smallest.

Both are prompt-visible. The Connected Systems block in the cached system
prompt names only the services actually loaded and pins only the tool names
that exist at the active tier, and startup priming probes the same set — so the
Executive is never told a tool is "already available" when it is not. If you
narrow these, expect the block to name fewer services; that is the intended
effect, not a fault.

Two things worth knowing before narrowing:

- `core` leaves out ten of the pinned tools — Gmail drafts, threads and
  attachments, Drive item listing, calendar freebusy, and the Docs/Sheets
  listing and info tools. `extended` and `complete` both cover all of them.
- `all` means every service only as the **whole** value (surrounding commas or
  spaces are fine). `gmail,all` is passed through as the two names `gmail` and
  `all`, and since `all` is not a service name, workspace-mcp's argument parser
  rejects it and **the child does not start at all** — you lose every Google
  tool, not just the misspelled one. Write `all` on its own, or list only real
  service names.
- Both variables reach the child through the `env` block of the
  `google_workspace` entry in `mcp_servers.json`, which must forward them
  (`"WORKSPACE_MCP_TOOLS": "$WORKSPACE_MCP_TOOLS"` and the same for
  `WORKSPACE_MCP_TOOL_TIER`, as
  [mcp_servers.json.example](../packages/core/mcp_servers.json.example) does).
  A config that forwards only one of them leaves the API filtering the prompt
  on a value the child never saw, which hides tools that are in fact callable.

A pinned tool that priming cannot find is logged once at startup, naming both
possible causes — the service list or tier excluding it, or workspace-mcp having
renamed it since the pin in `docker/Dockerfile`.

### Your own Gmail or Outlook (Act as me, optional)

Act as me lets the Executive draft replies **as the owner**, in the owner's
own Drafts (Gmail or Outlook), when they ask it to, and (with **Draft replies to my
inbox** on) for mail that needs them. Nothing goes out unless the owner sends
it: from their mailbox, or with **Send** on a reply card on Today, which needs signed
sign-ins on a server (see `docs/auth.md`). It uses a separate
credential for the owner's mailbox, read by the API directly and never handed
to workspace-mcp, so it must not live in the credentials directory above.

```bash
# On a machine with a browser, with the same OAuth client exported:
uv run --with google-auth-oauthlib python scripts/connect-own-gmail.py --email you@example.com
docker compose exec api mkdir -p /data/delegation_google
docker compose cp delegation-credentials/<file>.json api:/data/delegation_google/
```

For an Outlook mailbox (work, school or personal), run
`scripts/connect-own-outlook.py` instead, with the Microsoft 365 app from
`.env.example` exported (`MS365_MCP_CLIENT_ID`, and `MS365_MCP_TENANT_ID` if
it is single-tenant). It prints a code to enter at Microsoft's sign-in page,
from any browser, and writes the same kind of file, which goes in the same
directory. The app needs public client flows on and the delegated Graph
permissions `User.Read`, `Mail.ReadWrite` and `Mail.Send`. A person has one
mailbox for Act as me: connecting the other kind replaces the file.
The address is checked against Microsoft's directory, never the mail itself:
a personal account that signs in with another address (a Gmail one, say) and
has no Outlook address of its own on file is matched by that sign-in address,
so use it on the People page and with `--email`.

**Read my calendar (optional).** Run either script with `--calendar` to also
grant read-only access to the person's own calendar (`calendar.readonly` on
Google, which the OAuth client's consent screen must list; the delegated
`Calendars.Read` permission on the Microsoft app). Then turn on **Read my
calendar** in Delegate → Act as me. The Executive reads events fresh each time
and keeps nothing; a credential minted without `--calendar` keeps working for
mail, and the switch says to reconnect with the flag.

```bash
MS365_MCP_CLIENT_ID=<app id> uv run python scripts/connect-own-outlook.py --email you@example.com
```

No restart is needed. The address must be the owner's email on the People
page, and it must not be the Executive's own `EXEC_EMAIL_ADDRESS`. Then turn
it on in Delegate → Act as me; the setup status page shows the connection.

**Team members (optional).** Set `DELEGATION_TEAM_MEMBERS=true` and the owner
gets **Let team members use it** in Delegate → Act as me. Once they turn it
on, each team member connects their own Gmail or Outlook the same way (the script with
their own address, one file each in the same directory) and turns it on for
themselves. A team member's mail, drafts, reply cards and audit rows are
theirs alone: the owner sees only who uses it and how much.

---

## Upgrading

**Settings → About** shows the version you are running. When a newer release
is out it says so and links the release notes and this section. The API asks
GitHub's public releases API for the latest release at most every six hours,
and sends nothing about your install. Set `UPDATE_CHECK_ENABLED=false` to turn
the check off (for an air-gapped host, say); the running version is still shown.
`GET /version` returns the same facts as JSON.

There is no in-app update button: updating means replacing the containers,
which the app cannot do from inside one without being given control of the
host's Docker. Upgrade from the host instead.

1. **Read the release notes.** Each release's entry in
   [CHANGELOG.md](../CHANGELOG.md) (also on the GitHub Release) lists what
   changed. Before 1.0 a minor bump (`0.4.x` → `0.5.0`) marks a breaking
   change, such as a new required env var; do what its entry says first.
2. **Back up the database.** `/data/episodic_memory.db` is the part you cannot
   rebuild; see Backups under [Operations](#operations).
3. **Replace the containers.** Your data lives in the `executive_data` volume,
   which survives both of these.

   From a checkout, with `make docker`:

   ```bash
   git fetch --tags
   git checkout vX.Y.Z     # or `git pull` on main to follow unreleased changes
   make docker             # rebuilds the API image and restarts the stack
   ```

   From the published images: change the image tags to the new release
   (`ghcr.io/sentelabsai/openexecutive-api:X.Y.Z` and
   `ghcr.io/sentelabsai/openexecutive-ui:X.Y.Z`), then

   ```bash
   docker compose pull
   docker compose up -d
   ```

   Never run `docker compose down -v` to upgrade: `-v` deletes the volume.
4. **Check it came back.** `curl -s http://localhost:8000/health` reports the
   new `version`, and Settings → About shows it.

To go back, see **Rollback** under [Operations](#operations).

---

## Microsoft 365 credentials

Outlook mail and calendar tools come from `@softeria/ms-365-mcp-server`, a
Node MCP server that — like `workspace-mcp` — runs co-located inside the API as
a stdio child of the MCP gateway. It is baked into the API image (a Node build
stage copies the pinned package and the `node` binary in; there is no npm in
the runtime image) and launched by
[docker/ms365-mcp-launch.sh](../docker/ms365-mcp-launch.sh) from the
`microsoft_365` entry in `/data/company/mcp_servers.json` (see
[packages/core/mcp_servers.json.example](../packages/core/mcp_servers.json.example)
for the exact block). Both servers can be configured at once; the Executive
discovers whichever tools are present.

Auth is the MSAL **device-code** flow against your own Entra app registration
(the server's built-in public client id is refused). Register the app (Entra
admin center → App registrations → New; *Allow public client flows: Yes*;
delegated Graph permissions `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`,
`Files.ReadWrite` and `Files.Read.All` (only with `MS365_MCP_ONEDRIVE=true`, below),
plus `User.Read` and `offline_access` for the sign-in, with admin consent —
`ms365-mcp-launch.sh --list-permissions` prints exactly what the configured
tool list requests), then set:

```
MS365_MCP_CLIENT_ID=<application (client) id>
MS365_MCP_TENANT_ID=<directory (tenant) id>          # or "common" / "consumers"
MS365_MCP_EXPECTED_USERNAME=exec@yourcompany.com     # pins the signed-in account
MS365_MCP_CLIENT_SECRET=<only for a confidential app registration>
```

The API serves no OAuth callback and needs none: sign in once from inside the
container and the refresh token lands on the volume.

```bash
docker compose exec api /usr/local/bin/ms365-mcp-launch.sh --login          # prints a URL + code; sign in as the exec mailbox
docker compose exec api /usr/local/bin/ms365-mcp-launch.sh --verify-login
docker compose restart api        # the gateway reads config and credentials at startup
```

Setup is the launcher's `--login`, never chat: `MS365_MCP_EXPECTED_USERNAME`
is what stops a different account being bound. Run `--login` again if the
refresh token is ever revoked — until then every Microsoft 365 call answers
with an error payload and the API keeps serving. The server registers its
account tools whatever `--enabled-tools` says, so the gateway refuses
`microsoft_365__login`, `logout`, `select-account` and `remove-account`
itself and leaves them out of `search_tools` results — a chat turn cannot
sign the mailbox out, wipe the seeded token or switch accounts, whatever
`mcp_servers.json` says. The example config denies them too (defense in
depth). The read-only `verify-login` and `list-accounts` stay callable.

The launcher scrubs `$VAR` placeholders that extensible-mcp leaves literal for
unset variables, refuses to start without `MS365_MCP_CLIENT_ID`, keeps the
token cache under `MS365_MCP_CREDENTIALS_DIR` (0700), and starts the server
with an **explicit allow-list** of tools (`--enabled-tools`, an anchored regex
in the launcher) rather than the upstream `mail,calendar` preset: the preset
also registers inbox forwarding rules, mailbox settings (external auto-reply),
calendar sharing and event forwarding, which can address people the gateway
never sees. Exactly the Outlook mail + calendar tools and the OneDrive tools
(plus the login tools) are exposed. Egress is gated like Google: `send-mail`, forward and every draft
tool have their recipients roster-checked from the arguments; reply,
reply-all and send-draft — whose recipient is whoever the referenced message
names — have the gateway read that message first and check the sender /
original recipients / draft recipients (reply-all checks `replyTo` and the
sender together); `create-/update-calendar-event` attendees must be People
on the roster. Every mail write's `from`, `sender` and `replyTo` may name
only `EXEC_EMAIL_ADDRESS` (the Outlook twin of the Gmail acting-account pin),
and `move-mail-message` into Deleted Items, Junk Email or Recoverable Items
is refused, so the model cannot trash mail through a move. OneDrive gets the
surface Google Drive has (search, list, upload and edit, folders, move,
rename, copy, share; no delete), but only with `MS365_MCP_ONEDRIVE=true`
and a fresh `--login`: the server asks for its tools' permissions on every
token refresh, so the file tools are off until the sign-in grants them. `download-bytes` is pinned to mail
attachments and OneDrive file content, and a OneDrive file comes back as its
text rather than base64. `share-drive-item` invitees must be on the roster
and named by email, and `create-drive-item-share-link` must be scoped to
specific people (`scope: "users"`); an invite must require sign-in and grant
only view or edit. Writes (upload, new folder, move, copy) land only in the
Executive's own drives, so nothing is written into a folder someone else
shared, and `search-onedrive-files` takes plain words only (its query goes
into the request path unencoded). The same sign-in feeds the optional
OneDrive folder sync ([onedrive_sync_setup.md](onedrive_sync_setup.md)),
which asks the launcher for a token (`--access-token`) rather than keeping a
credential of its own. Widening the allow-list
(`MS365_MCP_ENABLED_TOOLS` / `MS365_MCP_PRESET`) is an operator decision that
must come with a matching gate change in `orchestrator/mcp_gateway.py`.

### Switching the mailbox and calendar to Microsoft 365

Adding the server only gives the Executive Outlook *tools*. Three code paths
call a fixed backend without the model choosing — the inbound mailbox poller,
alert email dispatch and the scheduler's email hint, and the typed
`create_calendar_event` / `create_instant_meeting` tools with their approval
ledger. They follow two switches, both defaulting to `google`:

```
EMAIL_PROVIDER=microsoft      # poll the Outlook inbox; reply via microsoft_365__reply-mail-message
CALENDAR_PROVIDER=microsoft   # book on the Outlook calendar with a Teams link
```

The switches are independent. A chosen backend whose MCP server is missing
from `mcp_servers.json` is logged at startup and the poller skips its cycle;
boot never fails. On Microsoft the approval-time conflict check reads only the
Executive's own calendar (delegated permissions cannot read attendees'
free/busy), so it is advisory in a narrower sense than Google's.

---

## ERPNext (oe_connector)

ERPNext's data and reports come from the
[oe_connector](https://github.com/ladislav-urban/oe_connector) Frappe app,
installed in the ERPNext site. Unlike the two servers above it is not a child
of the gateway: the app serves MCP over HTTP inside ERPNext, and the gateway
reaches it by URL. It offers six read tools (`erpnext__search_catalog`,
`erpnext__run_report`, `erpnext__list_records`, …), all read-only, under a
service user that holds only the "OE Reader" role.

1. In the ERPNext bench, install the app and create the service user:

   ```bash
   bench get-app https://github.com/ladislav-urban/oe_connector
   bench --site <site> install-app oe_connector
   bench --site <site> oe-connector-setup-reader [--company "Acme Ltd"]
   ```

   The last command prints the API key and secret once (`--rotate-keys`
   issues new ones). `--company` limits the user to that company.

2. Add the server to `/data/company/mcp_servers.json`:

   ```json
   "erpnext": {"url": "https://<site>/api/method/oe_connector.mcp.endpoint.handle"}
   ```

3. Put the credential in `/data/company/tokens`, next to it:

   ```
   erpnext=<api_key>:<api_secret>
   ```

   The gateway sends it as `Authorization: Bearer <api_key>:<api_secret>`, and
   oe_connector accepts that form on its MCP endpoint only. The file holds a
   live credential, so create it readable by the API's user alone before
   writing the line, and never put the key in `mcp_servers.json`:

   ```bash
   install -m 600 -o <api-user> /dev/null /data/company/tokens
   ```

   Client slots save and restore this file with `mcp_servers.json`, so each
   client's key stays with that client's config.

4. Restart the API. The Executive's Connected Systems section then names
   ERPNext and its tools, and the first call to each tool discovers it.

Use HTTPS for any site not on the same machine: the token is the
credential, and over plain `http://` on a network it travels in clear text.
`http://localhost` is fine for local development (the `http://*` deny in
`load_control` applies to `load_mcp_server` only, not to configured
servers, so nothing stops a plain-HTTP site URL). Keep `erpnext` in
`load_control.deny_names` as the example does; the gateway also refuses a
runtime server under any configured or `tokens` name, which would otherwise
be sent that name's credential.

**Who sees ERPNext data.** Out of the box every call runs as the one service
user, which can see more than most people can in ERPNext itself. So the
gateway answers `erpnext__*` calls only on the principal's own verified turn
(the web chat as the principal, their own Slack, Discord or secret-verified
Telegram, or their own Gmail-authenticated email). A teammate, an unverified
sender, Google Chat, the scheduler, workflows and other unattended runs are
refused, and the refusal is audited. A site the gateway cannot reach at
startup is skipped with a warning, but is still named as connected, so its
calls fail until the next restart after the site is back.

**Each person as themselves (phase 2).** With a signing key on both sides,
every call runs as the asking person's own ERPNext user, found by the email on
their People entry, with their roles and user permissions:

```bash
uv run --with cryptography python scripts/make-caller-keys.py --erpnext
```

It prints `ERPNEXT_CALLER_PRIVATE_KEY`, for the API's environment only, and a
`bench set-config` line that gives oe_connector the public key. Make a pair of
its own; never reuse the web app's. Then restart both. From then on:

- The gateway signs who is asking into each call (Ed25519, one call, those
  exact arguments, 60 seconds, once), and oe_connector refuses any call
  without a valid assertion, or presented by any user but the service user.
  A site without the public key refuses a signed call rather than ignore it.
- The principal is asked as on any verified surface, as before. A verified
  teammate is asked only where nobody else reads the reply (the web chat, a
  Slack or Discord DM, a Telegram chat), so their view never lands in a
  shared thread; in the web chat only with signed callers on
  (`CALLER_ASSERTION_PUBLIC_KEYS`, `docs/auth.md`), since without them the
  signed-in email is whatever the holder of the shared secret sends.
- Someone with no enabled ERPNext System User under that email (or whose
  email is the service user's) gets nothing, not the service user's view.
- Runs nobody is watching (the scheduler, scheduled follow-ups, workflows)
  are still refused: whoever scheduled one wrote its text and chose its
  recipient. The one exception is the morning brief's own ERP numbers
  (below), whose audience is fixed: they are signed as a `service` caller
  that oe_connector holds to the reports you list.

To rotate, add the new public key to `oe_connector_caller_public_keys`
(comma-separated), move the API to the new private key, then drop the old one.
A malformed key on either side refuses every call rather than falling back.

**Ask the Dynocortex inside ERPNext (phase 2b).** oe_connector docks an "Ask
the Dynocortex" chat panel on the right of every desk page, always open (along
the bottom on narrow screens), and adds a full page at
`/desk/ask-the-executive`. It needs per-person access (above), and a
second pair of its own that signs the other way:

```bash
uv run --with cryptography python scripts/make-caller-keys.py --erpnext-panel
```

It prints a `bench set-config` line for the private key, which stays in
ERPNext, and `ERPNEXT_PANEL_PUBLIC_KEYS` for the API. Then tell the site where
the API is, and restart both:

```bash
bench --site <site> set-config oe_connector_openexecutive_url https://<api-host>
```

- The panel posts to the site, never to OpenExecutive: oe_connector forwards
  each question to `POST /webhook/erpnext/chat`, signed (Ed25519, that exact
  body, 60 seconds, once) as the signed-in ERPNext user's email. The route
  is exempt from `BACKEND_SHARED_SECRET` because it checks that signature,
  and answers 503 without the keys or per-person access.
- Only someone on the People page as a team member gets answers; anyone else
  sees the panel disabled. ERPNext's word is all that names them, and an
  ERPNext administrator can change a user's email, so a panel turn is never
  treated as the principal or a verified teammate: it can only read ERPNext
  (as that same ERPNext user) and consult the specialists. With ERPNext
  drafts on (below), it can also leave that person a draft card, under the
  same rules as in chat (the owner, and teammates only with
  `ERPNEXT_DRAFTS_TEAMMATES`). The card says it was asked for in the panel,
  and it is approved only in the OpenExecutive web app, signed in, so
  ERPNext's word alone never creates anything. It cannot send, schedule,
  save, or reach any other system. It reads no one's memory,
  standing facts or department memory, and nothing it says is mirrored
  anywhere; it does read the company knowledge base, as a teammate on any
  channel does.
- The site URL must be HTTPS (plain HTTP only to the same machine); replies
  are shown with no images or live links.
- One question at a time per person, thirty an hour; conversations last an
  idle hour and end when the API restarts.

**ERP numbers in the morning brief (phase 3b).** Name the company and the
principal's morning brief ends with a few totals from ERPNext's own reports:
cash this month, receivables and payables by days past due, and yesterday's
sales. Only totals, never customer or supplier names, and only in a brief for
the principal:

```bash
ERPNEXT_BRIEF_COMPANY="Acme Ltd"   # the API's environment
```

With per-person access on, these are the only ERPNext calls a scheduled run
may make, signed as the service caller, so list the four reports in the bench:

```bash
bench --site <site> set-config -p oe_connector_unattended_reports '["Cash Flow", "Accounts Receivable Summary", "Accounts Payable Summary", "Sales Register"]'
```

A report ERPNext refuses is simply left out of the brief.

**Scheduled ERP metrics.** With the same setting, the owner can ask in chat
for one ERPNext figure on a schedule, e.g. "send me DSO every Monday at 8".
The figures are a fixed set computed from the same four reports: days sales
outstanding (receivables today against the last 90 days of sales), cash this
month, receivables, payables, and sales last week. Each goes to the owner's
own channel the way the brief does, daily or weekly at a time in their zone.
The owner can have five at most, and asks again to change or stop one.
A metric whose report ERPNext refuses is skipped for that run. The chat tools
for this, like the drafts below, are an extension the API loads only when
listed:

```bash
OPENEXECUTIVE_EXTENSIONS=openexecutive.integrations.erpnext_extension   # the API's environment
```

**ERPNext alerts (phase 3).** oe_connector can raise alerts on the
principal's board: a receivable overdue past a threshold, stock below its
reorder level on a best-selling item (both checked daily, as the service
user), and a purchase invoice far above the supplier's usual size (on
submit). It signs them with the panel's site key (above) and posts them to
`POST /webhook/erpnext/events`, so set those up first, then turn the rules on
in the bench:

```bash
bench --site <site> set-config -p oe_connector_alerts '{"enabled": 1, "overdue_days": 60, "overdue_min_amount": 1000, "top_items": 20, "large_invoice_factor": 3}'
```

Every ERPNext alert is private to the principal, because the checks see the
service user's view; each goes through the usual triage, and a rule keeps
one open alert per document. OpenExecutive takes 20 ERPNext events a minute
(the alert pipeline's budget is shared with mail and chat); oe_connector
keeps to that pace and retries the rest on its next run.

Two more rules use ERPNext's own report logic: a budget line whose actual
this fiscal year is above its budget (daily, by the Budget Variance Report),
and last week's gross margin well below the weeks before it (weekly, by Gross
Profit). Each stays off until its own key is set in `oe_connector_alerts`
(`budget_variance`, `weekly_margin`; the options are in oe_connector's
`alerts.py`). OpenExecutive accepts both kinds from this release on.

**ERPNext drafts (phase 4).** The owner can ask the Executive to prepare a
ToDo (a follow-up assigned to someone) or a Material Request (a restock) in
ERPNext, and, once listed (below), a Quotation for a customer or a Purchase
Order to a supplier. The Executive checks the draft with ERPNext and leaves a card on
Today and in the chat showing exactly what will be written; only when the
owner taps Approve is it created, unsubmitted, as their own ERPNext user, to
review and submit in ERPNext. Nothing is ever submitted, cancelled or deleted
from OpenExecutive, and nothing is drafted in a run nobody is watching. It
needs per-person access (above) and signed sign-ins
(`CALLER_ASSERTION_PUBLIC_KEYS`, or local login), and both sides turn it on:

```bash
ERPNEXT_DRAFTS_ENABLED=true   # the API's environment
# Which DocTypes (default: ToDo,Material Request); list the same on the site.
ERPNEXT_DRAFT_DOCTYPES=ToDo,Material Request
# The draft tool is an extension, as the metrics' are (above).
OPENEXECUTIVE_EXTENSIONS=openexecutive.integrations.erpnext_extension
```

```bash
bench --site <site> set-config -p oe_connector_draft_doctypes '["ToDo", "Material Request"]'
bench --site <site> set-config -p oe_connector_draft_max_value '{"Material Request": 5000}'
bench --site <site> set-config -p oe_connector_draft_daily_limit 20
```

The value cap is in company currency (quantity times the item's valuation
rate, else its last purchase rate); with a cap set, an item with neither is
refused.

Quotation and Purchase Order (phase 4b) face a customer or commit money once
submitted, so turn them on after running ToDo and Material Request for a
while, by listing them on both sides:

```bash
ERPNEXT_DRAFT_DOCTYPES=ToDo,Material Request,Quotation,Purchase Order
```

```bash
bench --site <site> set-config -p oe_connector_draft_doctypes '["ToDo", "Material Request", "Quotation", "Purchase Order"]'
bench --site <site> set-config -p oe_connector_draft_max_value '{"Material Request": 5000, "Quotation": 20000, "Purchase Order": 10000}'
```

Their cap is on ERPNext's own grand total, in company currency. ERPNext
prices them: the Executive gives a line's rate only when the owner states
one, and ERPNext's price list sets the rest; discounts, taxes and terms are
never taken from it. A Purchase Order can also come from a submitted
Material Request or Supplier Quotation, whose lines ERPNext's own "Create >
Purchase Order" brings over. The card shows each line's rate and amount and
the grand total as ERPNext worked them out. Approving makes only the draft,
and OpenExecutive sends nothing to the customer or supplier. ERPNext's own
Notifications and hooks on a new Quotation or Purchase Order still run when
the draft is inserted, so check that none of yours emails the customer or
supplier on "New" before listing these DocTypes. Leaving a DocType off
`ERPNEXT_DRAFT_DOCTYPES` later also stops its cards already waiting from being
approved. Each draft is tagged "OpenExecutive", commented with who asked and
approved, and logged in ERPNext's "OE Draft" list; OpenExecutive's audit log
has a row for each card proposed, created, failed or dismissed.

Teammates (phase 4b) can have drafts prepared too, each for themselves, once
the API's environment says so:

```bash
ERPNEXT_DRAFTS_TEAMMATES=true
```

A teammate is offered drafts where ERPNext already answers them as their own
user: verified, in a conversation nobody else reads (the web chat with
signed sign-ins, or their own Slack, Discord or Telegram DM). The card is
theirs alone, on their Today; only they can approve it, signed in, and the
draft is made as their ERPNext user, so their own ERPNext roles decide what
they can create. oe_connector's daily limit counts per person. Turned off
again, teammates' waiting cards can no longer be approved.

ERPNext alerts can suggest a draft (phase 4b; oe_connector from #11). A
low-stock alert carries the restock ERPNext's own reorder would raise, and an
overdue-invoice alert the invoice to follow up. On Today such an alert shows
**Prepare restock** or **Prepare follow-up** to the owner. Nothing is
checked or written when the alert arrives. The tap, signed in, has ERPNext
check the draft as the owner and leaves the usual card to approve. The
follow-up ToDo is worded from the invoice's figures and assigned to the
owner. The DocType must be listed in `ERPNEXT_DRAFT_DOCTYPES` (both are by
default); nothing else needs setting.

**Suppliers' agents over A2A.** A supplier's own AI agent can ask, over A2A
(Agent2Agent, JSON-RPC `message/send`), about that supplier's own submitted
purchase orders and invoices: status, dates, quantities and amounts. A narrow
agent answers with no one approving the reply. It is offered only
oe_connector's three supplier tools, and has no memory, specialists,
knowledge base, mail or other tools. Every exchange is in the audit log.
It needs per-person access (above). For each supplier, make a key pair:

```bash
uv run --with cryptography python scripts/make-caller-keys.py --erpnext-a2a-supplier "Northwind Cables Ltd"
```

The name must be the Supplier's name in ERPNext. The script prints the
private key, which goes to the supplier privately, and one entry for
`ERPNEXT_A2A_SUPPLIERS` on the API. A supplier can also make its own pair and
send you only the public key. Merge the entries into one JSON object, set the
public HTTPS URL suppliers reach the endpoint at, and restart the API:

```bash
ERPNEXT_A2A_SUPPLIERS='{"nw1": {"supplier": "Northwind Cables Ltd", "key": "<public key>"}}'
ERPNEXT_A2A_URL=https://<api-host>/a2a/suppliers
```

Every request is signed for that URL, so one a supplier sent another of its
customers can't be replayed here. A2A stays off until it is set (plain HTTP
only to this machine).

Then list the same Suppliers in the bench, which answers no supplier it
doesn't list:

```bash
bench --site <site> set-config -p oe_connector_a2a_suppliers '["Northwind Cables Ltd"]'
```

- Give the supplier the agent card's URL,
  `https://<api-host>/.well-known/agent-card.json`. It names two skills,
  orders and invoices, and the endpoint, `POST /a2a/suppliers`. Both are
  exempt from `BACKEND_SHARED_SECRET`, and the card is 404 until A2A is set
  up.
- Every request carries `x-oe-a2a-assertion`: the panel's format, with claims
  `{aud, body, exp, iat, jti, kid}` signed by the supplier's key: `aud` is
  `ERPNEXT_A2A_URL`, `body` the SHA-256 of the exact body, 60 seconds, each
  `jti` once. An unknown key, a bad signature or a replay gets nothing (401)
  and is audited.
- Each supplier may ask 30 times an hour, one question at a time. Each
  message is answered on its own: nothing is kept between messages, and
  there is no streaming, push notification or task. The used `jti`s, the
  rate limit and the one-at-a-time rule live in the API process, so run one
  API worker, as the image does.
- The supplier is the one registered for the key that signed, never anything
  the message says. The agent sees only that supplier's documents, and
  another supplier's order answers the same as one that doesn't exist.
- If oe_connector serves a tool whose description or schema doesn't match the
  digest this release pins (below), the agent doesn't answer until both are
  updated.
- To rotate a supplier's key, add the new key id beside the old one for the
  same supplier, then remove the old one once they use the new key. Remove
  the supplier's entry to stop answering it at once.

**Pinned tool definitions.** OpenExecutive pins a digest of each tool
definition oe_connector serves (`orchestrator/erpnext_tool_pin.py`). A tool
whose description or input schema doesn't match is never shown to the
model, any call to it is refused, and the API logs which tool and why.
Upgrade oe_connector and OpenExecutive together: a release that changes a
tool updates both. To check a site, compare `bench --site <site>
oe-connector-tool-digests` with the pin.

Once a conversation has read ERPNext, none of its replies, then or later,
is added to peer or department memory, which other people's turns read, nor
opens a follow-up loop, so one person's figures do not resurface for someone
else.

What the Executive can reach is decided in ERPNext, not here: business modules
only, never payroll, employee, bank-account or personal contact data, standard
reports only, rows capped at 500, and a company required for totals when the
user sees several. Every `erpnext__*` call is in the audit log as a
`tool_invocation` row, and its result reaches the model inside an
`<untrusted_content source="erpnext">` block, since customers and suppliers
write some of ERPNext's text. The design, including later phases (the
morning brief's ERP block), is in the app's `docs/erpnext-integration.md`.

### Stopping ERPNext access

OpenExecutive's half of the stop runbook. ERPNext's half, its own switches
and the quarterly rehearsal are in oe_connector's
[`docs/stop-runbook.md`](https://github.com/ladislav-urban/oe_connector/blob/main/docs/stop-runbook.md).
Each setting below is read at start, so restart the API after changing it.

- **No new drafts:** `ERPNEXT_DRAFTS_ENABLED=false`. No card is offered, and
  a card already waiting refuses to approve (`drafts_off`). Drafts already
  made stay unsubmitted in ERPNext until someone submits or deletes them.
  Reads, the panel and alerts go on.
- **No calls into ERPNext at all:** remove the `erpnext` line from
  `/data/company/tokens`, or the `erpnext` entry from `mcp_servers.json`.
  The gateway can no longer reach ERPNext. What was already done there
  stays.
- **No unattended ERP calls:** leave `ERPNEXT_BRIEF_COMPANY` unset. The
  brief drops its ERP block and scheduled metrics send nothing, so neither
  calls ERPNext. Conversations still can.
- **Rotating a key,** without a stop: add the new key on the other side
  first, then switch, then drop the old one.
  - `ERPNEXT_CALLER_PRIVATE_KEY` (who is asking): add the new public half
    to the site's `oe_connector_caller_public_keys` beside the old one, set
    the new private key here, then remove the old public key there.
  - `ERPNEXT_PANEL_PUBLIC_KEYS` (the panel and alert events): add the new
    public half here beside the old one (it takes several), set the new
    private key in ERPNext, then remove the old public key here.

  Removing the old key first, on either side, is a stop: every call that
  key signs is refused.
- **The widest stop needs no access to OpenExecutive:** an ERPNext operator
  disables the service user `oe-connector-setup-reader` made (first name
  "OpenExecutive", role "OE Reader"). Every call from OpenExecutive then
  fails, whoever it acts for, from the next request. Use it first when it
  isn't clear what is wrong.

For the evidence, take `GET /erpnext/drafts/oversight?erpnext=1` before
cutting ERPNext off, if you can: it lists every card, who decided it and
what became of each draft in ERPNext. Once the gateway can't reach ERPNext,
`erpnext=1` answers 502, and the plain export still lists the cards and who
decided them. The audit log has every `erpnext__*` call either way.

---

## Operations

```bash
# Logs
docker compose logs -f api

# Smoke the API directly
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" https://api.example.com/health

# Same JSON, proxied through the UI (exercises the shared secret end to end)
curl -s https://exec.example.com/api/backend/health

# Inspect state on the volume — SELECT only unless you mean it
docker compose exec api sqlite3 /data/episodic_memory.db \
  "SELECT id, kind, status, run_at FROM scheduled_actions WHERE status='pending' LIMIT 10;"
```

**Rollback** is an image-tag rollback: redeploy the previous release tag
(see [Images](#images)). The volume is
not versioned with the image, so a release that migrates schema forward is not
undone by rolling the image back — check what changed under `*/store.py` before
relying on it. Additive column migrations (the common case — every column has a
default and older builds name their columns explicitly) are safe to roll back
over; a rolled-back build simply ignores the newer columns.

**Backups.** There is no snapshot cron in this repo. `/data/episodic_memory.db`
is the irreplaceable part (Chroma rebuilds from source documents), so back it up
with `sqlite3 /data/episodic_memory.db ".backup /tmp/backup.db"` and copy it off
the host — a plain file copy of a live SQLite database can be torn.

---

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Deploy reports success, `/health` never responds | Crash during boot, usually a missing env var | Read the API logs; look for a Pydantic `ValidationError` naming the variable |
| Container is killed and restarted repeatedly during startup | Health-check grace period too short | Raise it to ~5 minutes (see above) |
| API refuses to start: `BACKEND_SHARED_SECRET is required` | `OE_PUBLIC_DEPLOYMENT` is set with no secret | Working as intended — set the secret |
| Every UI request errors, API is healthy | UI proxy can't reach the API, or the shared secret differs between them | Check `BACKEND_BASE_URL` on the UI and that both sides carry the same `BACKEND_SHARED_SECRET` |
| Every UI request is `401` with a `caller_assertion_*` code | Signed callers are half set up | See [auth.md → Debugging](auth.md#debugging) |
| Browser console shows CORS errors | UI origin missing from `BACKEND_ALLOWED_ORIGINS` | Add the exact scheme + host |
| Scheduled actions firing twice | More than one API replica | Scale the API to exactly 1 (see the warning at the top) |
| Onboarding wizard says "no company profile" | Empty volume on first boot | Expected — complete the wizard; output lands at `/data/company/profile.yaml` |

---

## Optional: self-hosted Honcho

Per-person memory can run against hosted Honcho (set `HONCHO_API_KEY` and
`HONCHO_BASE_URL`) or a self-hosted instance. [docker/honcho/](../docker/honcho/)
carries the image and configuration for the self-hosted path.
