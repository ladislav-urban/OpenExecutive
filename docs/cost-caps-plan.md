# Cost caps plan

How OpenExecutive stops spending past a limit: a cap per person a day, a cap
per job a day and a monthly cap the principal sets. Each is checked before
every model call and fails closed. This is step S7 of the agentic ERP plan.
It has no ERPNext half.

Written Nov 9, 2026, against this fork's main and the upstream sync branch
(`ladislav-urban/OpenExecutive#186`). The code lands after that sync.

## In short

- **One check, before every model call.** `get_provider()` wraps every
  backend in a `CappedProvider`, the same way it wraps one for tracing. The
  wrapper reads today's counters and the month's, and raises `CostCapReached`
  before the call goes out. Every model call in the package already goes
  through `get_provider()`, so no call site can miss the check. A test
  pins that.
- **One count, after every model call.** `audit.usage.log_model_usage()`
  already records each call's cost. It also adds that cost to a counter
  table in SQLite, `usage_counters`, keyed by scope, key and day. Counters
  are rows in the database, so they survive a restart.
- **Fails closed.** If the counters can't be read, the call is refused.
  A call whose cost is unknown counts at the model's highest price for its
  `max_tokens`. A local model counts as free.
- **A job stops. The principal is told once.** An unattended run that hits
  a cap fails with the cap's reason. It doesn't retry until the next day,
  and the principal gets one alert per cap per day. A person who hits their
  own cap in chat is told so in the reply, and the turn makes no further
  calls.

## What is counted, and against whom

Each call is counted against up to three scopes:

| Scope | Key | Where it comes from |
| --- | --- | --- |
| `person` | People id | The turn's speaker: the signed-in caller, a verified chat-app sender, or `Session.run_for_person_id` on an unattended run a teammate queued. The principal's own turns count against the principal. |
| `job` | Job id | A new ContextVar, `audit.usage.bind_job(job_id)`, set by every unattended runner: a scheduled action (`scheduled:<id>`), the morning brief (`brief`), alert triage (`alert-triage`), a workflow run (`workflow:<name>`), take-the-lead (`take-the-lead`), and the action-card runner (`action-card`). S5's watch jobs bind `watch:<job>` the same way. |
| `total` | `all` | Every call. The monthly cap reads this one. |

A call with neither a person nor a job counts only against `total`. That
includes a system utility call such as a session title.

Voice transcription (`providers/transcription.py`) is billed per audio
minute, not by tokens, and doesn't go through `get_provider()`. It is not
capped in this step. Triage's injectable `client` is for tests only.

## The caps

| Setting | Default | Meaning |
| --- | --- | --- |
| `COST_CAP_PERSON_DAILY_USD` | 0 (off) | One person's calls in a calendar day, in the company's time zone. Never the principal's |
| `COST_CAP_JOB_DAILY_USD` | 0 (off) | One job's calls in a day |
| `COST_CAP_MONTHLY_USD` | 0 (off) | Every call in a calendar month |

- **Setting a cap.** The principal sets the monthly cap, and can override a
  person's daily cap in People. The environment values are the defaults, so
  an operator can turn caps on before anyone opens Settings.
- **No overrun.** A cap is an amount, and a call is refused once the
  counted spend plus the call's worst case (its `max_tokens` at the model's
  output price, plus the prompt at its input price) would pass it. A single
  call can't overrun a cap by more than its estimate's error.
- **The principal.** The person cap never applies to the principal's own
  calls. The monthly cap does: at it, the principal's chat is refused like
  every other call, so the monthly cap is a real ceiling. The reply tells
  the principal where to raise it.

## Changes

| Where | What |
| --- | --- |
| `audit/usage.py` | `bind_job()`, and the counter upsert in `log_model_usage()` |
| `audit/cost_caps.py` (new) | `usage_counters` table, `check(model, kwargs)`, `CostCapReached`, the worst-case estimate from `audit.pricing` |
| `providers/capped.py` (new) and `providers/registry.py` | `CappedProvider` around every backend, inside the tracing wrapper |
| `scheduler/runner.py`, `briefing/`, `workflows/`, `orchestrator/take_the_lead.py`, `delegation/action_cards.py` | Bind the job id. On `CostCapReached`, mark the run failed with the cap's reason and no retry until tomorrow |
| `orchestrator/executive.py` | On `CostCapReached` in a turn, end it with a plain sentence that names the cap |
| `alerts/` | One alert to the principal per cap per day, deduplicated by `external_id` `cost-cap:<scope>:<key>:<day>` |
| `api/routes/settings` and People | The monthly cap and a person's daily override, principal only |
| UI | Settings field, the People override, and today's spend against each cap on the usage page |
| Docs | `architecture-facts.yaml`, the `prebuilt` sections for providers, scheduler and api, the guide, `.env.example` |

## Tests (the S7 gate)

- A capped job stops before its next call, and a call that would pass the
  cap is refused before it goes out.
- The counter survives a restart: a new process reads the same day's spend.
- The principal is told, once per cap per day.
- Every model call in the package goes through `get_provider()`. The
  existing `test_usage_recording.py` pattern pins this.
- An unreadable counter table refuses the call.
- A teammate's queued run counts against that teammate, not the principal.
- The principal's calls pass the person cap and stop at the monthly cap.
- A day turns over at midnight in the company's time zone.

One `security-reviewer` round before the PR.

## Decisions

The owner answered the open questions on Nov 9, 2026 (decision 163):

1. **Defaults.** Every cap is off by default, so an install that sets
   nothing changes nothing. A deployment turns caps on in its own
   configuration. Its monthly figure is suggested from its own spend: the
   highest of its last three months in `GET /audit/usage`, plus half again.
   It is set on the operator's word when the caps deploy.
2. **The principal.** The principal is exempt from the person cap. The
   monthly cap still applies to the principal's calls.
3. **A day** runs in the company's time zone.
