"""The answer check: a reply's figures held to what ERPNext returned that turn,
and corrected before the person keeps it.

oe_connector's ``docs/answer-check-plan.md`` (PR (a): numbers). Decided by the
owner on Nov 4, 2026: a figure that doesn't match ERPNext is corrected, the
reply never says so, and it streams as today and is replaced when it ends
("stream it and correct in place").

**What a turn keeps** (``AnswerCheckTurn``, bound by its route with
``collect``): every ERPNext read result of the turn, by call, as the gateway
returns it (``note_erpnext_result``, from the MCP loop, ``run_script`` and the
committee's draft alike); the person's message and page; and every other text
the turn read (other tools' results, the conversation so far), with ERPNext's
own results taken out of it. The reply's own drafted text never counts.

**When the reply ends** (``check_reply``), on a turn that read ERPNext:

1. ``answer_check_context`` (oe_connector, hidden: ``CODE_ONLY_TOOLS``) gives
   the site's number format and which fields are numeric, as the turn's own
   caller, inside ``erpnext_caller.answer_check_run`` (the gateway runs that
   tool only there, and nothing else there).
2. Check 1 (``figure_check``) finds where each figure came from.
3. Check 2 (``answer_review``) reviews only the flagged ones, checked in code.
4. The correction (``answer_correct``): a known value written in by code; the
   correction call only when a value isn't known or the words depend on it;
   removal as the fallback.

The text that comes back replaces the reply (``reply_replaced`` on the web
chat's stream; the final text for every other route). One ``answer_check``
audit row per checked turn holds counts only, never a figure or text.

Off with ``ERPNEXT_ANSWER_CHECK=off``. Any error inside the check is logged
and the reply is left as it was: a checker must never cost the person their
answer. A check that runs out of time falls back to code alone (no model
call), which removes what it can't confirm.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from openexecutive.orchestrator import answer_correct, figure_check
from openexecutive.orchestrator.figure_check import ErpnextResult, Figure, NumberFormat, ResultTypes

logger = logging.getLogger(__name__)

# The results kept per turn; a turn that reads more checks against the first.
MAX_RESULTS = 60
# What one reply's check may take before it falls back to code alone.
CHECK_BUDGET_S = 60.0
# A stopped or timed-out turn's: the person is waiting for it to wind down.
STOPPED_BUDGET_S = 20.0
CODE_ONLY_BUDGET_S = 10.0
CONTEXT_TIMEOUT_S = 10.0
CORRECTION_TIMEOUT_S = 45.0
# The ERPNext reads whose results figures come from (gateway names).
_FIGURE_TOOLS = frozenset({"erpnext__list_records", "erpnext__get_record", "erpnext__run_report"})
_ERPNEXT_BLOCK = re.compile(r'<untrusted_content source="erpnext"[^>]*>.*?</untrusted_content>', re.S)


@dataclass
class AnswerCheckTurn:
    """What one turn's answer check reads (module docstring)."""

    results: list[ErpnextResult] = field(default_factory=list)
    given: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    # The morning brief's: its context call runs as the brief's service caller.
    brief: bool = False
    # Set by the check: the reply was replaced whole (answer_correct.WHOLE_REPLY).
    whole_replaced: bool = False
    _context: dict[str, Any] | None = None
    _done: tuple[str, str] | None = None

    def note_result(self, tool: str, arguments: Any, text: object) -> None:
        payload = figure_check.parse_result(text)
        if payload is None or len(self.results) >= MAX_RESULTS:
            return
        args = arguments if isinstance(arguments, dict) else {}
        self.results.append(ErpnextResult(tool, args, payload))

    def note_given(self, *texts: object) -> None:
        self.given.extend(t for t in texts if isinstance(t, str) and t.strip())

    def note_texts(self, contents: Iterable[Any]) -> None:
        """Text the turn read: message contents or tool results, with
        ERPNext's own results left out (their values are typed, above)."""
        for content in contents:
            for text in _texts(content):
                stripped = _ERPNEXT_BLOCK.sub("", text)
                if stripped.strip():
                    self.texts.append(stripped)

    def note_messages(self, messages: Iterable[dict[str, Any]], system_blocks: Iterable[Any] = ()) -> None:
        """The conversation the turn starts from, and its system blocks."""
        self.note_texts(m.get("content") for m in messages if isinstance(m, dict))
        self.note_texts(b.get("text") if isinstance(b, dict) else b for b in system_blocks)


def _texts(content: Any) -> Iterator[str]:
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                yield block
            elif isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    yield block["text"]
                elif block.get("type") == "tool_result":
                    yield from _texts(block.get("content"))


_bound: ContextVar[AnswerCheckTurn | None] = ContextVar("answer_check_turn", default=None)


@contextlib.contextmanager
def collect(turn: AnswerCheckTurn) -> Iterator[AnswerCheckTurn]:
    """Bind ``turn`` as what the gateway's ERPNext results go to. Saved and
    put back rather than reset by token, as ``erpnext_panel.collect_views``
    is, so it holds across the web chat stream's task hops."""
    previous = _bound.get()
    _bound.set(turn)
    try:
        yield turn
    finally:
        _bound.set(previous)


def current() -> AnswerCheckTurn | None:
    return _bound.get()


def enabled() -> bool:
    from openexecutive.config import get_settings

    return get_settings().erpnext_answer_check != "off"


def note_erpnext_result(normalized: str, arguments: Any, text: object, *, failed: bool) -> None:
    """Called by the gateway after every ``erpnext__*`` call: on a turn being
    checked, keep a read's result."""
    turn = _bound.get()
    if turn is None or failed or normalized not in _FIGURE_TOOLS:
        return
    turn.note_result(normalized, arguments, text)


def note_tool_results(tool_results: Iterable[Any]) -> None:
    """Called by the Executive's loop with each round's tool results."""
    turn = _bound.get()
    if turn is not None:
        turn.note_texts(tool_results)


@dataclass
class _Counts:
    figures: int = 0
    flagged: int = 0
    reviewed: int = 0
    supported: int = 0
    substituted: int = 0
    model_corrected: int = 0
    left_out: int = 0
    whole_reply: int = 0
    code_only: bool = False


def _doctypes(turn: AnswerCheckTurn) -> list[str]:
    names: dict[str, None] = {}
    for result in turn.results:
        doctype = result.arguments.get("doctype")
        if result.tool != "erpnext__run_report" and isinstance(doctype, str) and doctype.strip():
            names.setdefault(doctype.strip(), None)
    return list(names)[:30]


async def _context(turn: AnswerCheckTurn) -> dict[str, Any]:
    """``answer_check_context`` for the DocTypes the turn read, once a turn;
    {} when ERPNext isn't there or refuses (the plain format is used then)."""
    if turn._context is not None:
        return turn._context
    turn._context = {}
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_ANSWER_CHECK_TOOL, ERPNEXT_SERVER

    gateway = get_active_gateway()
    if gateway is None or ERPNEXT_SERVER not in gateway_server_names(gateway):
        return turn._context
    try:
        with erpnext_caller.answer_check_run(), (
            erpnext_caller.brief_run() if turn.brief else contextlib.nullcontext()
        ):
            raw = await asyncio.wait_for(
                gateway.call_tool({
                    "name": ERPNEXT_ANSWER_CHECK_TOOL, "arguments": {"doctypes": _doctypes(turn)},
                }),
                CONTEXT_TIMEOUT_S,
            )
    except Exception:
        logger.warning("answer_check: answer_check_context failed; reading the plain format", exc_info=True)
        return turn._context
    payload = figure_check.parse_result(raw)
    if isinstance(payload, dict):
        turn._context = payload
    else:
        logger.warning("answer_check: answer_check_context gave nothing usable; reading the plain format")
    return turn._context


async def _rewrite(question: str, draft: str, corrections: list[answer_correct.Correction], model: str) -> str | None:
    """The correction call (``answer_correct``, step 2): the turn's own model,
    no tools, a constant system prompt, the draft as untrusted content."""
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.orchestrator.content_trust import wrap_untrusted
    from openexecutive.prompts.answer_check import CORRECTION_SYSTEM
    from openexecutive.providers import get_provider

    content = (
        f"The person asked:\n{wrap_untrusted(question, source='question')}\n\n"
        f"Corrections:\n{answer_correct.describe(corrections)}\n\n"
        f"The draft:\n{wrap_untrusted(draft, source='draft')}"
    )
    response = await asyncio.wait_for(
        get_provider(model).messages_create(
            model=model,
            max_tokens=min(8192, max(1024, len(draft) // 2)),
            system=CORRECTION_SYSTEM,
            messages=[{"role": "user", "content": content}],
        ),
        CORRECTION_TIMEOUT_S,
    )
    log_model_usage(response, model=model, actor="answer_check")
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text").strip()
    return text or None


def _passes(text: str, sources: figure_check.Sources, fmt: NumberFormat) -> bool:
    return not any(f.flagged for f in figure_check.check_figures(text, sources, fmt))


async def _check(text: str, turn: AnswerCheckTurn, *, question: str, model: str, models: bool, counts: _Counts) -> str:
    from openexecutive.orchestrator.answer_review import CANNOT_TELL, review

    context = await _context(turn)
    fmt = NumberFormat.parse(context.get("number_format"))
    doctypes = context.get("doctypes")
    types = ResultTypes(doctypes if isinstance(doctypes, dict) else {})
    sources = figure_check.sources_for(turn.results, types=types, given=turn.given, texts=turn.texts, fmt=fmt)
    figures = figure_check.check_figures(text, sources, fmt)
    counts.figures = len(figures)
    flagged = [f for f in figures if f.flagged]
    counts.flagged = len(flagged)
    if not flagged:
        return text
    reviews = (
        await review(question=question, draft=text, flagged=flagged, results=turn.results, sources=sources)
        if models
        else [CANNOT_TELL] * len(flagged)
    )
    counts.reviewed = len(flagged) if models else 0
    corrections: list[answer_correct.Correction] = []
    known: list[float] = []
    for fig, verdict in zip(flagged, reviews, strict=True):
        if verdict.verdict == "supported" and verdict.right is not None:
            counts.supported += 1
            known.append(verdict.right)
        else:
            corrections.append(answer_correct.Correction(fig, verdict.right if verdict.verdict == "wrong" else None))
            if verdict.right is not None:
                known.append(verdict.right)
    if not corrections:
        return text
    checked = figure_check.sources_for(
        turn.results, types=types, given=turn.given, texts=turn.texts, extra=known, fmt=fmt,
    )
    failed = [c.figure for c in corrections]
    if not answer_correct.needs_rewrite(text, corrections):
        substituted = answer_correct.substitute(text, corrections)
        if _passes(substituted, checked, fmt):
            counts.substituted = len(corrections)
            return substituted
    if models:
        try:
            rewrite = await _rewrite(question, text, corrections, model)
        except Exception:
            logger.warning("answer_check: the correction call failed; removing instead", exc_info=True)
            rewrite = None
        if (
            rewrite
            and answer_correct.changed_only_within(text, rewrite, answer_correct.allowed_spans(text, failed))
            and _passes(rewrite, checked, fmt)
        ):
            counts.model_corrected = len(corrections)
            return rewrite
    fixed = answer_correct.fallback(text, corrections, figures)
    if fixed == answer_correct.WHOLE_REPLY:
        counts.whole_reply = 1
        turn.whole_replaced = True
    else:
        counts.substituted = sum(1 for c in corrections if c.right is not None and format_in(fixed, c.figure, c.right))
        counts.left_out = len(corrections) - counts.substituted
    return fixed


def format_in(text: str, fig: Figure, value: float) -> bool:
    return figure_check.format_like(fig, value) in text


def _audit(counts: _Counts, *, changed: bool) -> None:
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "answer_check",
        f"Answer check: {counts.figures} figure(s), {counts.flagged} flagged"
        + (", reply corrected" if changed else ""),
        actor="answer_check",
        # Counts only: no figure or text, so the audit holds no ERPNext data.
        details={
            "figures": counts.figures,
            "flagged": counts.flagged,
            "reviewed": counts.reviewed,
            "supported": counts.supported,
            "substituted": counts.substituted,
            "model_corrected": counts.model_corrected,
            "left_out": counts.left_out,
            "whole_reply": counts.whole_reply,
            "code_only": counts.code_only,
            "changed": changed,
        },
    )


async def check_reply(
    text: str,
    turn: AnswerCheckTurn | None,
    *,
    question: str = "",
    model: str = "",
    budget_s: float = CHECK_BUDGET_S,
) -> str:
    """``text`` with its figures held to the turn's ERPNext results: the same
    string when nothing changed. Checks a turn's reply once (a second call
    with the same text returns the first answer). Never raises."""
    if turn is None or not turn.results or not text.strip() or not enabled():
        return text
    if turn._done is not None and turn._done[0] == text:
        return turn._done[1]
    if not model:
        from openexecutive.config import get_settings

        model = get_settings().default_model
    counts = _Counts()
    try:
        try:
            fixed = await asyncio.wait_for(
                _check(text, turn, question=question, model=model, models=True, counts=counts), budget_s,
            )
        except TimeoutError:
            logger.warning("answer_check: out of time; checking with code alone")
            counts = _Counts(code_only=True)
            fixed = await asyncio.wait_for(
                _check(text, turn, question=question, model=model, models=False, counts=counts),
                CODE_ONLY_BUDGET_S,
            )
    except Exception:
        logger.exception("answer_check: the check failed; the reply is left as it was")
        return text
    turn._done = (text, fixed)
    try:
        _audit(counts, changed=fixed != text)
    except Exception:
        logger.warning("answer_check: audit row failed", exc_info=True)
    return fixed
