"""The proposed rules waiting for a System Manager, on the principal's Home
(phase 3: the bank-rules job's proposals, ``watch.jobs.bank_rules``).

Read from code only, never a turn: oe_connector's ``proposed_rules`` is
withheld from chat (``connected_systems.ERPNEXT_WITHHELD_TOOLS``) and runs
here under its own marker (``erpnext_caller.proposed_rules_as``) as the
principal's own ERPNext user, so it shows what is still waiting after anyone
decides. oe_connector answers only a System Manager, so for anyone else the
list is empty.

``GET /erpnext/proposed-rules`` reads it (at most once every ``FRESH_S`` per
person); Home's "Needs you" count uses the last read (``waiting_count``),
which never calls ERPNext. A rule's word comes from bank lines: outsiders'
text, kept to a plain word here and shown as text, never as markup or to a
model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

FRESH_S = 300.0
CALL_TIMEOUT_S = 30.0
MAX_ITEMS = 50
_NAME_RE = re.compile(r"[^\x00-\x1f\x7f-\x9f]{1,140}")
_WORD_RE = re.compile(r"[a-z]{5,30}")
_KINDS = frozenset({"Bank Transaction Rule"})
_TYPES = frozenset({"Withdrawal", "Deposit", "Any"})
# Frappe's timestamp ("2026-11-12 10:30:00.123456"), kept to the second.
_WHEN_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d{1,6})?")


@dataclass(frozen=True)
class ProposedRule:
    name: str
    kind: str
    company: str
    account: str
    # "Withdrawal", "Deposit" or "Any", as ERPNext names a rule's direction.
    transaction_type: str
    # The description word the rule would match (bank-line text).
    word: str
    lines: int
    misses: int
    # ERPNext's desk link to the proposal ("" when it gave none fit to link).
    url: str
    # When the job proposed it (ERPNext's time, ISO to the second), or "".
    proposed: str = ""


# person id -> (when read, what waits)
_read: dict[int, tuple[float, tuple[ProposedRule, ...]]] = {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) and _NAME_RE.fullmatch(value) and value == value.strip() else ""


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100_000 else 0


def _item(raw: Any) -> ProposedRule | None:
    """One waiting proposal, or None for anything not oe_connector's shape."""
    from openexecutive.integrations.erpnext_drafts import _desk_link

    if not isinstance(raw, dict) or raw.get("status") != "Proposed" or raw.get("kind") not in _KINDS:
        return None
    rule, evidence = raw.get("rule"), raw.get("evidence")
    if not isinstance(rule, dict) or not isinstance(evidence, dict):
        return None
    name, company, account = _text(raw.get("name")), _text(raw.get("company")), _text(rule.get("account"))
    conditions = rule.get("description_contains")
    words = [c.get("value") for c in conditions if isinstance(c, dict)] if isinstance(conditions, list) else []
    word = words[0] if len(words) == 1 and isinstance(words[0], str) and _WORD_RE.fullmatch(words[0]) else ""
    kind = str(raw["kind"])
    transaction_type = rule.get("transaction_type")
    when = _WHEN_RE.fullmatch(raw["proposed"]) if isinstance(raw.get("proposed"), str) else None
    if not name or not company or not account or not word or transaction_type not in _TYPES:
        return None
    return ProposedRule(
        name=name, kind=kind, company=company, account=account, transaction_type=str(transaction_type), word=word,
        lines=_count(evidence.get("lines")), misses=_count(evidence.get("misses")), url=_desk_link(raw.get("url")),
        proposed=f"{when.group(1)}T{when.group(2)}" if when else "",
    )


def parse(result: Any) -> tuple[ProposedRule, ...]:
    """The waiting proposals in oe_connector's answer, at most MAX_ITEMS."""
    if not isinstance(result, dict) or not isinstance(result.get("proposed_rules"), list):
        return ()
    found = (_item(raw) for raw in result["proposed_rules"][:MAX_ITEMS])
    return tuple(item for item in found if item is not None)


async def _ask(person: Any) -> tuple[ProposedRule, ...] | None:
    """Read them from ERPNext as ``person``: the list, () when oe_connector
    refuses (not a System Manager), None when it couldn't be asked."""
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_SERVER

    gateway = get_active_gateway()
    if gateway is None or ERPNEXT_SERVER not in gateway_server_names(gateway):
        return None
    arguments: dict[str, Any] = {}
    try:
        with erpnext_caller.proposed_rules_as(arguments, person):
            raw = await asyncio.wait_for(
                gateway.call_tool({"name": erpnext_caller.PROPOSED_RULES_TOOL, "arguments": arguments}),
                timeout=CALL_TIMEOUT_S,
            )
    except erpnext_caller.WatchRefused:
        return ()
    except Exception:
        logger.warning("erpnext_rule_proposals: the proposed rules couldn't be read")
        return None
    try:
        result = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return ()
    if not isinstance(result, dict) or "error" in result:
        return ()
    return parse(result)


def _may_see(person: Any) -> bool:
    return (
        person is not None and getattr(person, "id", None) is not None
        and getattr(person, "is_principal", False) is True and getattr(person, "active", False) is True
    )


async def waiting(person: Any, *, now: float | None = None) -> tuple[ProposedRule, ...]:
    """What waits for the principal ``person``, read again when the last read
    is older than FRESH_S; () for anyone else."""
    if not _may_see(person):
        return ()
    at = time.monotonic() if now is None else now
    held = _read.get(person.id)
    if held is not None and at - held[0] < FRESH_S:
        return held[1]
    found = await _ask(person)
    if found is None:
        # Not asked (ERPNext not connected): what was read last, if anything.
        return held[1] if held is not None else ()
    _read[person.id] = (at, found)
    return found


def waiting_count(person: Any) -> int:
    """Home's "Needs you" count: the last read for the principal, never an
    ERPNext call."""
    if not _may_see(person):
        return 0
    held = _read.get(person.id)
    return len(held[1]) if held is not None else 0


def _reset_for_tests() -> None:
    _read.clear()
