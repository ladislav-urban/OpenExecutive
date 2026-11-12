"""The bank-rules job (phase 3): Bank Transaction Rules for a System Manager
to enable in ERPNext, from the bank lines people already reconciled.

Weekly, for one company (a job's settings name one; several companies are
several jobs, as oe_connector lists them). No model and no tool loop:

- **Its one read**, ``bank_rule_evidence({company})``: oe_connector's own
  candidates, each an account, a direction ("in" or "out") and the word every
  supporting line shares, with how many lines over how many months. Counts
  only: never a bank line's description, amount or account number. The word is
  still bank-line text, so it is outsiders' text here: it goes only into the
  proposal's arguments, never into a notice.
- **Its proposals**, one ``propose_rule`` call per candidate, exactly as the
  read returned it, each under its own code-only marker
  (``erpnext_caller.proposal_for_job``, bounded a day in ``watch_days``). A
  proposal is inert: oe_connector writes an "OE Proposed Rule" a System
  Manager enables or dismisses in the desk, rebuilds the rule from the account,
  word and direction itself, and refuses anything that isn't one of today's
  candidates. Whoever set the job up is held back from deciding on its
  proposals by oe_connector, from its own record of the job (never from here).
  A refusal is recorded as a code and never stops the run.
- **The chain's head**: the read returns the OE Draft chain's newest row
  (``chain_head``); this install keeps it per company, and a head shorter
  than last seen tells the principal, since a proposal's Enable and Withdraw
  are sealed in the desk with no step here.

Its notices hold counts and refusal codes only. The plan is oe_connector's
private ``docs/phase3-lean-rules-plan.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, time
from typing import Any

from openexecutive.watch.registry import (
    CardSpec,
    ProposalSpec,
    ReadArguments,
    Settings,
    WatchJob,
    WatchRead,
)

JOB_ID = "bank-rules"
KIND = "Bank Transaction Rule"
EVIDENCE_TOOL = "bank_rule_evidence"
PROPOSE_TOOL = "propose_rule"
EVIDENCE_SHAPE: Mapping[str, Any] = {"company": "$company"}
# Mondays, after the bank-matching job's morning run.
WEEKDAYS = frozenset({0})
SCHEDULE = time(10, 30)
# At most this many proposals tried a run (oe_connector caps a company's own:
# five waiting, two new a day).
MAX_PROPOSALS = 5
DIRECTIONS = frozenset({"in", "out"})
# oe_connector's word: ASCII letters, five to thirty (bank_rules.WORD_RE).
_WORD_RE = re.compile(r"[a-z]{5,30}")
# An account's name as ERPNext keeps it: no control characters.
_ACCOUNT_RE = re.compile(r"[^\x00-\x1f\x7f-\x9f]{1,140}")
_HEX_RE = re.compile(r"[0-9a-f]{64}")

# oe_connector's refusals, read only to pick a code (its text is never kept).
_REFUSALS: tuple[tuple[str, str], ...] = (
    ("not one of today's candidates", "not_a_candidate"),
    ("no longer a candidate", "not_a_candidate"),
    ("already proposed or in ERPNext", "duplicate"),
    ("proposed rules waiting", "cap_open"),
    ("proposed rules for today", "cap_day"),
    ("are off for", "proposals_off"),
    ("is listed for bank matching", "no_listed_account"),
    ("you cannot read the company", "company_unreadable"),
    ("job's alone", "not_this_job"),
)
REFUSAL_CODES = frozenset(code for _said, code in _REFUSALS) | {"refused", "timeout", "unanswered"}

CHAIN_LINE = (
    "ERPNext's draft log is shorter than when the bank-rules job last read it (row {then}, now {now}). "
    "Rows may have been deleted; run oe-connector-verify-drafts."
)


# --------------------------------------------------------------------------- #
# The read
# --------------------------------------------------------------------------- #


def _evidence_args(settings: Settings, today: date, prior: tuple[Mapping[str, Any], ...] = ()) -> ReadArguments:
    company = settings["company"]
    return {"company": company} if isinstance(company, str) and company else None


def _candidate(item: Any) -> dict[str, str] | None:
    """One candidate as ``propose_rule`` takes it (account, word, direction),
    or None for anything not exactly oe_connector's shape."""
    if not isinstance(item, Mapping):
        return None
    account, word, direction = item.get("account"), item.get("word"), item.get("direction")
    if (
        not isinstance(account, str) or not _ACCOUNT_RE.fullmatch(account) or account != account.strip()
        or not isinstance(word, str) or not _WORD_RE.fullmatch(word)
        or direction not in DIRECTIONS
    ):
        return None
    return {"account": account, "word": word, "direction": str(direction)}


def candidates(result: Mapping[str, Any], company: str) -> list[dict[str, str]]:
    """The read's candidates for ``company``, each as ``propose_rule`` takes
    it, in its own order, without repeats; none when the result is for
    another company."""
    if not isinstance(result, Mapping) or result.get("company") != company:
        return []
    found: list[dict[str, str]] = []
    for item in result.get("candidates") or []:
        rule = _candidate(item)
        if rule is not None and rule not in found:
            found.append(rule)
    return found


def proposal_arguments(company: str, rule: Mapping[str, str]) -> dict[str, Any]:
    return {"company": company, "kind": KIND, "rule": dict(rule)}


def _key(rule: Mapping[str, str]) -> str:
    digest = hashlib.sha256(json.dumps(rule, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"rule:{digest}"


def proposals(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> Sequence[ProposalSpec]:
    """One proposal per candidate the read returned, at most MAX_PROPOSALS."""
    company = settings["company"]
    if not reads or not isinstance(company, str) or not company:
        return ()
    return tuple(
        ProposalSpec(key=_key(rule), arguments=proposal_arguments(company, rule))
        for rule in candidates(reads[0], company)[:MAX_PROPOSALS]
    )


def proposal_check(settings: Settings, arguments: Mapping[str, Any]) -> bool:
    """Whether ``arguments`` are exactly a proposal this job may leave now:
    its own company, its kind, and a rule of oe_connector's candidate shape."""
    company = settings.get("company")
    if not isinstance(company, str) or not company or set(arguments) != {"company", "kind", "rule"}:
        return False
    rule = arguments.get("rule")
    return (
        arguments.get("company") == company
        and arguments.get("kind") == KIND
        and isinstance(rule, Mapping)
        and set(rule) == {"account", "word", "direction"}
        and _candidate(rule) == dict(rule)
    )


def refusal_code(said: object) -> str:
    """The code for a refused proposal, from oe_connector's text (only
    searched, never kept)."""
    text = said if isinstance(said, str) else ""
    for words, code in _REFUSALS:
        if words in text:
            return code
    return "refused"


# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #


def build(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> Sequence[CardSpec]:
    """No notice from the read itself: the runner tells what the run proposed
    once its proposals are made, in counts and codes."""
    return ()


def chain_notices(reads: Sequence[Mapping[str, Any]], settings: Settings, today: date) -> Sequence[CardSpec]:
    """Keep the chain head the read returned, and tell the principal when it
    is behind the one this job last saw (a lower index, or the same index with
    another hash): counts and indexes only."""
    from openexecutive.watch.settings import swap_chain_head

    company = settings["company"]
    head = reads[0].get("chain_head") if reads and isinstance(reads[0], Mapping) else None
    if not isinstance(company, str) or not company or not isinstance(head, Mapping):
        return ()
    index, row_hash = head.get("index"), head.get("row_hash")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        return ()
    if not isinstance(row_hash, str) or not _HEX_RE.fullmatch(row_hash):
        return ()
    before = swap_chain_head(JOB_ID, company, index, row_hash, today.isoformat())
    if before is None:
        return ()
    then, then_hash = before
    if index > then or (index == then and row_hash == then_hash):
        return ()
    return (
        CardSpec(
            key=f"chain:{then}:{index}:{row_hash[:16]}",
            summary="ERPNext's draft log is shorter than before",
            notes=(CHAIN_LINE.format(then=then, now=index),),
            to_principal=True,
        ),
    )


JOB: WatchJob = WatchJob(
    id=JOB_ID,
    title="Bank rules",
    reads=(WatchRead(EVIDENCE_TOOL, EVIDENCE_SHAPE, _evidence_args, "the bank rules reconciled lines support"),),
    build=build,
    schedule=SCHEDULE,
    card_cap_day=3,
    default_settings={"company": ""},
    weekdays=WEEKDAYS,
    card_noun="proposed rules",
    read_notices=chain_notices,
    proposals=proposals,
    proposal_tool=PROPOSE_TOOL,
    proposal_check=proposal_check,
    proposal_refusal=refusal_code,
)


def listing() -> dict[str, Any]:
    """The job's entry in ``oe_connector_watch_jobs`` as an operator writes
    it: its one read, run as its own ERP user (oe_connector admits its
    ``propose_rule`` for a company this read allows, never listed)."""
    return {"as_job_user": True, "reads": [{"tool": EVIDENCE_TOOL, **EVIDENCE_SHAPE}]}
