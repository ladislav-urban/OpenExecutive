"""Check 2 of the answer check: a small model reviews the flagged figures
(oe_connector's ``docs/answer-check-plan.md``, section 3).

Runs only when check 1 (``figure_check``) flags a figure. One call to
``ROUTING_MODEL`` (question 4) gets the question, the draft with each flagged
figure numbered, and the turn's ERPNext results, wrapped as untrusted content
as they are for the main model. For each figure it answers ``supported``,
``wrong`` or ``cannot_tell``, and for the first two it names the operation and
the numbers it took from the results.

**Its answers are checked in code** (section 3, "Its answers are checked in
code"): every number it names must be one of ERPNext's own values in the
results, an operation over a cut-off result's rows can't add up to a total,
and the operation is worked out here, never taken from the model. So it can
neither confirm a figure nor supply a value that isn't in what ERPNext
returned. A verdict that doesn't parse, names a number that isn't there, or
doesn't add up is ``cannot_tell``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from openexecutive.orchestrator.figure_check import ErpnextResult, Figure, Sources

logger = logging.getLogger(__name__)

Verdict = Literal["supported", "wrong", "cannot_tell"]

# Each result's JSON in the review is cut to this, and all of them to the total.
MAX_RESULT_CHARS = 12_000
MAX_RESULTS_CHARS = 60_000
MAX_OPERANDS = 60
REVIEW_TIMEOUT_S = 20.0
_TWO = frozenset({"difference", "ratio", "percent", "percent_change"})
_MANY = frozenset({"sum", "average"})


@dataclass(frozen=True)
class Review:
    """Check 2's verdict on one flagged figure, as code read it: ``right`` is
    the value code worked out from ERPNext's own numbers (for ``supported``
    the figure's own, for ``wrong`` the one the results give)."""

    verdict: Verdict
    right: float | None = None


CANNOT_TELL = Review("cannot_tell")


def marked_draft(draft: str, flagged: list[Figure]) -> str:
    """The draft with flagged figure n written ``[[Fn: <figure>]]``."""
    out = draft
    for n, fig in sorted(enumerate(flagged, start=1), key=lambda p: p[1].start, reverse=True):
        out = out[: fig.start] + f"[[F{n}: {fig.text}]]" + out[fig.end:]
    return out


def _results_text(results: list[ErpnextResult]) -> str:
    from openexecutive.orchestrator.erpnext_caller import ASSERTION_ARG

    parts: list[str] = []
    used = 0
    for n, result in enumerate(results, start=1):
        arguments = {k: v for k, v in result.arguments.items() if k != ASSERTION_ARG}
        body = json.dumps(result.payload, ensure_ascii=False, default=str)
        if len(body) > MAX_RESULT_CHARS:
            body = body[:MAX_RESULT_CHARS] + " …(cut)"
        part = f"Call {n}: {result.tool.removeprefix('erpnext__')} {json.dumps(arguments, default=str)}\n{body}"
        if used + len(part) > MAX_RESULTS_CHARS:
            parts.append(f"Call {n}: left out (too long)")
            continue
        parts.append(part)
        used += len(part)
    return "\n\n".join(parts)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _worked_out(op: object, operands: list[float]) -> float | None:
    if op == "value" and len(operands) == 1:
        return operands[0]
    if op in _MANY and 1 <= len(operands) <= MAX_OPERANDS:
        total = sum(abs(v) for v in operands)
        return total if op == "sum" else total / len(operands)
    if op in _TWO and len(operands) == 2:
        a, b = abs(operands[0]), abs(operands[1])
        if op == "difference":
            return abs(a - b)
        if b == 0 and op in ("ratio", "percent") or a == 0 and op == "percent_change":
            return None
        if op == "ratio":
            return a / b
        if op == "percent":
            return a / b * 100.0
        return abs(b - a) / a * 100.0
    return None


def read_verdict(item: object, fig: Figure, sources: Sources) -> Review:
    """One verdict from the model, checked against ERPNext's own values."""
    if not isinstance(item, dict):
        return CANNOT_TELL
    verdict = item.get("verdict")
    if verdict not in ("supported", "wrong"):
        return CANNOT_TELL
    raw = item.get("operands")
    if not isinstance(raw, list) or not raw:
        return CANNOT_TELL
    operands = [_number(v) for v in raw]
    if any(v is None for v in operands):
        return CANNOT_TELL
    numbers = [v for v in operands if v is not None]
    op = item.get("op")
    if not all(sources.has_value(v) for v in numbers):
        return CANNOT_TELL
    if op in _MANY and len(numbers) > 1 and any(sources.in_cut_off_rows_only(v) for v in numbers):
        # Rows of a result ERPNext cut off never add up to its total.
        return CANNOT_TELL
    value = _worked_out(op, numbers)
    if value is None:
        return CANNOT_TELL
    if fig.matches(value):
        return Review("supported", value)
    if verdict == "wrong":
        return Review("wrong", value)
    return CANNOT_TELL


def _parse(text: str) -> dict[int, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except (ValueError, RecursionError):
        return {}
    items = data.get("figures") if isinstance(data, dict) else None
    out: dict[int, Any] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool):
            out.setdefault(item["id"], item)
    return out


async def review(
    *,
    question: str,
    draft: str,
    flagged: list[Figure],
    results: list[ErpnextResult],
    sources: Sources,
) -> list[Review]:
    """Check 2's verdict on each flagged figure, in order. Never raises: a
    failed or late call leaves every figure ``cannot_tell``."""
    if not flagged:
        return []
    try:
        return await asyncio.wait_for(_review(question, draft, flagged, results, sources), REVIEW_TIMEOUT_S)
    except Exception:
        logger.warning("answer_review: the review failed; every flagged figure stays unconfirmed", exc_info=True)
        return [CANNOT_TELL] * len(flagged)


async def _review(
    question: str, draft: str, flagged: list[Figure], results: list[ErpnextResult], sources: Sources,
) -> list[Review]:
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.content_trust import wrap_untrusted
    from openexecutive.prompts.answer_check import REVIEW_SYSTEM
    from openexecutive.providers import get_provider

    model = get_settings().routing_model
    content = (
        f"The person asked:\n{wrap_untrusted(question, source='question')}\n\n"
        f"The draft, with the figures to check marked:\n"
        f"{wrap_untrusted(marked_draft(draft, flagged), source='draft')}\n\n"
        f"The ERPNext results the draft was written from:\n"
        f"{wrap_untrusted(_results_text(results), source='erpnext')}\n\n"
        f"Figures to check: {', '.join(f'F{n}' for n in range(1, len(flagged) + 1))}."
    )
    response = await get_provider(model).messages_create(
        model=model,
        max_tokens=1500,
        system=REVIEW_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    log_model_usage(response, model=model, actor="answer_check")
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
    items = _parse(text)
    return [read_verdict(items.get(n), fig, sources) for n, fig in enumerate(flagged, start=1)]
