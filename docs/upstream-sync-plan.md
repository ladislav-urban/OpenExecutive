# Upstream sync plan

How this fork, `ladislav-urban/OpenExecutive` (Dynocortex's OpenExecutive),
takes in the 164 commits that `SenteLabsAI/OpenExecutive` has gained since the
two split. The owner accepted this plan on Nov 7, 2026 (see Decisions at the
end); the sync starts Wed Nov 11.

Measured on Nov 7, 2026, against upstream main `7fbeff93`
(`SenteLabsAI/OpenExecutive#724`) and this fork's main `aaa81fe0`
(`ladislav-urban/OpenExecutive#182`).

## In short

- **Merge upstream into a sync branch, in five batches, oldest first.**
  Don't rebase and don't cherry-pick. A merge keeps both histories and every
  author's credit. It records `7fbeff93` as the new common base, so the next
  sync starts from there instead of from Oct 19 again.
- **Most of the conflict is in a few places.** There are 310 conflict hunks
  in 119 files. Over half are in the orchestrator's turn loop
  (`executive.py`), Home (`Briefing.tsx`, `today.py`), the frame pages
  (`embed.ts` and its friends) and the architecture docs.
- **The real risk is where nothing conflicts as text.** Three upstream
  changes meet this fork's ERPNext paths in behaviour:
  - Approvals as one gate on every channel
    (`SenteLabsAI/OpenExecutive#706`).
  - Cards answered from chat and from chat apps
    (`SenteLabsAI/OpenExecutive#658`, `#718`, `#722`).
  - Home as Needs you / Done for you (`SenteLabsAI/OpenExecutive#710`).

  Each needs a test that pins this fork's rule, not just a clean merge.
- **Effort:** about 8–10 working days for one session. That covers the
  merges, the ERPNext checks, oe_connector's changes and a rehearsal on
  erpnext-9a's bench pair.
- **Timing:** start Wed Nov 11, after Tuesday's reshoot, on a branch that
  never reaches the pilot until it has passed the rehearsal. Land it before
  phase 0's OpenExecutive-side card work, because that work would be built on
  upstream's new card model.

## Where the two stand

| | |
|---|---|
| Split at | `528e546d` (`SenteLabsAI/OpenExecutive#551`), Oct 19, 2026 |
| Upstream only | 164 commits (127 Rufus, 23 dependabot, 12 ours already upstreamed, 2 others) |
| This fork only | 231 commits (269 with merges) |
| Changed since the split | upstream 621 files (+67k / −7k), this fork 614 files (+56k / −4k) |
| A trial merge (`git merge-tree`) | 119 files in conflict, 310 hunks |

Our 12 upstreamed commits include the whole frame work:
- `SenteLabsAI/OpenExecutive#623`: the nonce Content-Security-Policy.
- `#628`: the host list.
- `#635`: confirming inside a frame.
- `#638`: the user hint.
- `#641` and `#648`: tests.

Upstream reshaped them in review, and this fork has moved on since. Only one
other upstream commit touches those files (`SenteLabsAI/OpenExecutive#677`,
Stop and "I'll finish it"). So nearly all of the frame conflict is our code
against an older copy of our own code.

Everything ERPNext lives in this fork only. Upstream has no `erpnext` code at
all. The ERPNext parts conflict only where they hook into shared files: the
gateway, the decision routes, the chips, `today.py` and the frame pages.

## Merge, rebase or selective port

| | Verdict | Why |
|---|---|---|
| **Merge, in batches** | **Do this** | No history rewritten, so no force-push. Every author and PR link stays. The merge base moves, so later syncs are small. Each batch is one reviewable merge commit, and CI runs on each. |
| Rebase our 231 onto upstream | No | It rewrites every SHA the pilot, the evals and other sessions point at, and needs a force-push. It replays 231 commits through the same conflicts, many of them more than once. |
| Port only what we want | No, except as a fallback for one batch | It loses the merge base, so every later sync conflicts from Oct 19 again. 159 upstream commits would be copied by hand, and upstream's later fixes depend on the earlier ones. |

How it's done: a branch `sync/upstream-2026-11` off `private/main`. Each batch
is `git merge <upstream cut>` with its conflicts resolved, tests run and CI
green before the next. The branch merges back into `main` with a merge commit,
not a squash. A squash would throw away the merge base, which is the point of
merging. That is the one exception to this repo's squash rule; the owner
should agree to it (question 2).

`main` keeps moving during the sync. The sync branch takes `main` in with a
merge at each batch boundary, never a rebase.

## Batches, in order

The cut points follow upstream's own first-parent history. Each batch ends at
a green upstream commit.

| # | Upstream range | Commits | Files upstream / also changed here | What it brings |
|---|---|---|---|---|
| 1 | to `SenteLabsAI/OpenExecutive#600` (Oct 19–21) | 44 | 294 / 98 | Delegation (meeting invites from a card, inbox work), briefing fixes, workflow and people features; dependency bumps |
| 2 | to `#640` (Oct 21–26) | 40 | 229 / 124 | The frame work as upstream took it (`#623`, `#628`, `#635`, `#638`), a long turn's steps with Stop, delegation reply cards as a conversation |
| 3 | to `#701` (Oct 26–Nov 3) | 57 | 261 / 104 | Memory facts and "what it worked out", contact notes, chat fixes, file tools, content scan |
| 4 | to `#706` (Nov 3) | 4 | 98 / 43 | **Pulse becomes Memory (`#702`); Approvals as the one gate (`#706`, new `orchestrator/levels.py`)** |
| 5 | to `#725` (Nov 3–6) | 18 | 129 / 61 | **Home as Needs you / Done for you (`#710`), grouped approvals (`#709`), cards that ask (`#718`), cards answered in chat (`#722`)**, held steps in plain words (`#714`), people fixes |

Batch 4 is kept to four commits on purpose. It carries the two biggest
behaviour changes, and its review should be about them alone.

## Conflicts by area

Hunks are from the trial merge of the whole range at once. The batches split
them up, but the areas stay the same.

| Area | Files | Hunks | How to resolve |
|---|---|---|---|
| Turn loop: `orchestrator/executive.py` | 1 | 23 | Upstream rewrote the gates for `#706`. Our side added the host surface, the ERPNext panel, the answer check and extension tools. Take upstream's structure and put our hooks back in, one hunk at a time, with `test_host_surface_turns.py`, `test_private_turns.py` and the ERPNext panel tests as the guard. The hardest file. |
| Home: `Briefing.tsx`, `api/routes/today.py`, `briefing/*`, `ProposalCard.tsx` | 7 | ~30 | Upstream's Needs you / Done for you layout plus our fields. Keep `tiles`, `needs_you_count`, `dismiss_older` and `quiet_banner` in `GET /today`, because oe_connector's pin reads them. Take `done_for_you` as an addition. |
| Frame pages: `embed.ts`, `EmbedProvider`, `FrameConfirm`, `EmbeddedShell`, `cardActions.ts`, `seen.ts`, `host-list.json`, `hostOrigins.ts`, `middleware.ts`, `cards/[id]/page.tsx`, their `scripts/*.test.mjs` | ~22 | ~90 | Take ours: it is the newer copy of the same code. Then re-apply `SenteLabsAI/OpenExecutive#677`'s part by hand. Check `oe-embed/v1` and the host list against oe_connector's pin. |
| Architecture docs: `architecture-facts.yaml`, 17 `prebuilt/*.json` | 18 | ~35 | Union of both: each side added different topics. Re-author the sections both sides changed (`integrations`, `api`, `today`, `agents`, `memory`, `caching`), and validate with `python -m json.tool`. |
| Guide: `guide/prebuilt/*.json` | 5 | ~8 | Union, then the Pulse → Memory wording. |
| Routes: `chat.py`, `people.py`, `auth.py`, `decisions.py`, `sessions.py` | 5 | ~17 | Mostly additive on both sides. In `decisions.py`, keep `erpnext_draft` as its own decision spec (see below). |
| Memory and people: `episodic.py`, `facts.py`, `session_store.py`, `people/store.py`, `people_tools.py` | 5 | ~10 | Upstream rewrote parts of episodic memory; take theirs and keep our columns. |
| Smaller orchestrator files: `action_chips.py`, `mcp_gateway.py`, `extensions.py`, `outside_content.py`, `session.py`, `schedule_tools.py`, `take_the_lead.py` | 7 | ~12 | Small text conflicts. The gateway's real conflict is in behaviour (below). |
| Tests | 16 | ~30 | Follow the code they test; never drop one of our tests to get green. |
| Build and deps: `uv.lock`, `package.json`, `package-lock.json`, `docker/pysandbox/requirements.txt` | 4 | ~12 | Take upstream's versions, then re-run `uv lock` and `npm install`. Don't hand-merge lock files. |
| Repo files: `CLAUDE.md`, `.env.example`, `docs/auth.md`, `docs/deployment.md`, `.github/workflows/*` | 7 | ~20 | Union. Keep this fork's ERPNext, pilot and runner sections. Our CI workflows win where they differ: self-hosted runners, the gitleaks job, the claude review rules. |
| Evals: `evals/run_evals.py`, `evals/runner.py` | 2 | ~4 | Keep our scenario fields (`surface`, `demo_viewer`, `product_name`, `prior_turns[].page`, `forbid_*`). |

## Where nothing conflicts but behaviour does

Each of these gets a test on the sync branch that fails if the rule is lost.

1. **Approvals gate vs the ERPNext gateway rules (`#706`).** Upstream's
   `levels.decide` turns a held gateway step into an Approve card. Under
   Hands-off, or inside an approved card, it skips the block. This fork's
   ERPNext rules are different in kind:
   - `_check_erpnext_caller` signs who is asking.
   - The gateway refuses every `erpnext__draft*` call outside the draft
     path's own markers.
   - Supplier runs, briefing runs and the principal-only rule.

   These are hard refusals, not approvals, and must run before
   `levels.decide`, at every level, approved card or not. Test: under
   Hands-off and inside an approved card, an `erpnext__draft_create` call
   outside the draft path is still refused, and an `erpnext__*` call still
   carries the signed caller.
2. **Cards answered from chat (`#658`, `#722`).** Upstream approves decision
   cards from a chat app's tap and from the `answer_card` chat tool. Both go
   through `decisions.resolve_in_chat`, and only for classes in
   `CHAT_CARD_CLASSES`. An `erpnext_draft` card must stay out of that list.
   It is approved only on the web card, by its own approver, with a signed
   caller and the draft's fingerprint (`integrations/erpnext_drafts.py`).
   Test: `shown_in_chat` is false for an `erpnext_draft` card, and
   `answer_card` on one is refused.
3. **Home (`#710`, `#709`).**
   - Done for you lists one row per approved card. Approved ERPNext drafts
     will show there, so their text must go through `in_product_reply` like
     the card's ("the ERP" on a named install).
   - Grouped approvals must not merge a draft card with another kind.
   - The native Dynocortex Home reads `GET /today` through oe_connector's pin.
     The fields it reads must stay, or the pin is re-cut with oe_connector's
     native screen changed in the same step.
4. **Cards that ask (`#718`).** Upstream's `POST /alerts/{id}/answer` runs a
   turn as whoever answered. ERPNext alert drafts (`/erpnext/alert-drafts`)
   are alerts. Check that an answer turn from an ERPNext alert keeps the
   ERPNext caller rules (the turn's caller, no supplier context).
5. **Memory replaces Pulse (`#702`).** Tool, route and guide strings now say
   "the Memory page". `/memories` stays the address. Any reply or prompt of
   ours that says "Pulse" changes with it.
6. **Per-channel levels removed (`#706`).** One level now covers every
   channel. Check the demo bundle and demo-reset (`DEMO_RESET_COMPANY`): a
   demo person's per-channel level must map onto the one level, so the
   guided scenarios see the same cards as before.
7. **Prompt caching.** Upstream changed block 0's parts (the persona and the
   outside-content rules). After each batch, check that the ERPNext guide
   (`prompts/connected_systems.py`) is still constant text in block 0 and the
   tool list is still sorted (`test_committee_prompt_cache_safety.py`,
   `test_executive_tool_loop_cache.py`).

## What changes for oe_connector

- **Navigation (`oe_connector/navigation.py`).** The "Pulse" entry
  (`/memories`, icon `activity`) becomes "Memory", and the embed config's
  `_place("pulse", "/memories", "Pulse")` does too. Keep "pulse" as a search
  keyword so old habits still find it. The path is unchanged, and upstream
  keeps old `?tab=` links working. `test_navigation.py`, the single-UI
  checklist and the design docs follow.
- **Pinned API (`oe_connector/ui/openexecutive_api.json`).** Run
  `make embed-outputs` on the sync branch, then
  `bench oe-connector-check-ui --outputs` against it.
  - What to expect: no endpoint gone, if the Home fields above are kept;
    `GET /today` gains `done_for_you` (an added field is fine).
  - Re-pin with `bench oe-connector-pin-ui-api` once the merge lands.
- **Draft cards.** Nothing changes in oe_connector's draft protocol. These
  stay as they are:
  - `draft_check`'s fingerprint.
  - `expected_fingerprint` on create.
  - The card key.
  - The caller signing.

  The risk is on this side (points 1–3 above). After the merge, the guided
  draft-and-approve scenario (S14) is the end-to-end check.
- **Embed contract (`oe-embed/v1`).** Ours wins the merge, so the protocol
  and host list stay as oe_connector pins them. Confirm with the same check
  as the pin. Upstream's Stop and "I'll finish it" (`#677`) adds UI inside
  the frame, not to the contract.
- **Hosted pages in the desk.** Home's layout changes (Needs you / Done for
  you), Memory replaces Pulse, and held steps show inline in chat. The
  hosted-page screenshots in oe_connector's help and the website's S-series
  will look different. List them for a later reshoot rather than block on
  them.

## Evals

- Run the full ERPNext panel set (`erpnext_*.yaml`, 87 scenarios) on the
  rehearsal bench before batch 1 as a baseline, and again after batch 5.
  Every scenario that passed at the baseline must still pass.
- Upstream changed `evals/runner.py` and `run_evals.py`. Keep our scenario
  fields, and run upstream's new scenarios too.
- The finance guides (invoice mismatch, overdue debt, forecast) and S14 are
  the end-to-end set. They passed on the pilot at `27881b1`/`aaa81fe`.
- Unit and integration suites run per batch in CI as usual.

## How it's rehearsed

On erpnext-9a's rehearsal bench pair, never the pilot:

1. Bring the pair to the pilot's current state: OpenExecutive `aaa81fe`,
   oe_connector main, and the demo bundle reset.
2. Baseline: the ERPNext eval set and the guided scenarios (finance guides,
   S14, the panel tours), with the results saved.
3. Deploy the sync branch's images to the pair. Run oe_connector's pin check,
   the baseline again and S14's draft-and-approve. Check that a demo-reset
   still gives the same demo people, levels and cards.
4. Fault drills from this fork's history:
   - The gateway's first start behind the maintenance page
     (`ladislav-urban/OpenExecutive#181`).
   - A draft changed before approval (the refusal and its wording).
   - A shared chat member viewing a card's chip.
5. The pilot gets the merge only after the owner has seen the rehearsal's
   results (question 5).

## Effort

| Step | Days |
|---|---|
| Batch 1 (delegation, briefing) | 1 |
| Batch 2 (frame pages: mostly take ours, re-apply `#677`) | 1 |
| Batch 3 (memory, people, chat) | 1.5 |
| Batch 4 (Memory, Approvals gate, `executive.py`), with the gateway tests | 2 |
| Batch 5 (Home, card answers), with the Home and chat-answer tests | 1.5 |
| oe_connector: navigation, re-pin, docs | 0.5 |
| Evals and the rehearsal, with fixes they find | 1.5–2.5 |
| **Total** | **9–10** |

Each batch also gets a security-reviewer pass (it touches `api/`,
`integrations/` and the cached blocks). The merges are sequential, so a
second session speeds up only the oe_connector and eval steps.

## Timing

- **Tue Nov 10, the weekly reshoot on the pilot:** untouched. The sync
  doesn't start until Wed, and the pilot stays on `aaa81fe` until the
  rehearsal passes.
- **Wed Nov 11, phase 0 starts.** Phase 0's OpenExecutive side changes draft
  cards: watch caller, batched cards, `draft_update` and comments, cost caps
  and separation of duties. It would be built on upstream's card model:
  - one Approvals gate,
  - grouped approvals,
  - cards that ask,
  - Done for you.

  If phase 0 lands first, its card code gets merged against `#706`–`#722`
  afterwards, which is the worst-conflicting area. Recommended order:
  - **Nov 11–13:** batches 1–3. In parallel, phase 0's design and
    oe_connector-side work.
  - **Nov 16–18:** batches 4–5 and the oe_connector changes.
  - **Nov 18–20:** evals and the rehearsal. The merge lands on `main`, and
    the pilot deploy is the owner's go.
  - **From about Nov 19:** phase 0's OpenExecutive card work starts on the
    merged `main`.
- **Nov 17, the next weekly reshoot:** stays on the pilot as it is. The
  merge is not deployed before then.
- **After this:** sync weekly, the day after each reshoot. Upstream moves
  about 20 commits a day at its busiest, and a week's merge is a day's work
  at most.

## The fork going public as "OpenExecutive"

- **History and credit.** A merge keeps upstream's authors, PR numbers and
  the Apache-2.0 lineage in the public history, as the licence asks. Add a
  `NOTICE` and a README line saying this is Dynocortex's fork of
  `SenteLabsAI/OpenExecutive`. Keep upstream's licence unchanged.
- **The name.** The public fork keeps the name "OpenExecutive", labelled as
  Dynocortex's fork (decision 6).
- **Self-hosted CI runners.** This fork's CI runs on two self-hosted runners
  on one WSL host. On a public repo, a pull request from anyone could run
  code on that host. Before going public:
  - fork PRs must run on GitHub-hosted runners only, or wait for a
    maintainer's approval;
  - the self-hosted runners keep to `push` and same-repo PRs.

  Upstream's `claude-code-review-fork.yml` (label-gated) arrives with the
  merge and is the model for that.
- **History audit before publishing.** Run gitleaks over the whole history
  (CI scans only new commits). Look for:
  - company data or a `.env` ever committed;
  - pilot host names and internal URLs in docs and commit messages.

  This is needed whether or not the sync happens first.
- **Order.** Publishing after the sync means the public fork starts level
  with upstream plus our work. Publishing before means upstream's 164
  commits arrive later as one large public merge. After is cleaner.

## Decisions

The owner accepted the recommendations on Nov 7, 2026 (through the
coordinator, Control Room 132; the name in oe-connector-63's window):

1. **Merge in five batches**, as above. No rebase, no selective port.
2. **The sync lands on `main` as a merge commit**, this time and at each
   later sync. Every other PR stays squash-merged.
3. **The sync goes before phase 0's OpenExecutive card work.** Batches start
   Wed Nov 11, after Tuesday's reshoot. Phase 0's card code starts on the
   merged `main`, about Nov 19.
4. **Home's pinned fields stay** (`tiles`, `needs_you_count`,
   `dismiss_older`, `quiet_banner`). Moving the native Home to Needs you /
   Done for you is a later change in oe_connector.
5. **The pilot gets the sync only after the rehearsal on erpnext-9a's bench
   pair and the owner's go**, and never in a reshoot week before that
   reshoot has run.
6. **The public fork keeps the name "OpenExecutive"**, labelled as
   Dynocortex's fork of `SenteLabsAI/OpenExecutive`.
7. **The fork is published after the sync**, once the history audit and the
   self-hosted runner change are done.
8. **Syncs are weekly from then on**, the day after each reshoot.
