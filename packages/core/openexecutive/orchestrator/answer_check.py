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

**First, on every route** (``_cut_planted``), whether or not the turn read
ERPNext and whether or not the check is on: a sentence repeating the
specifics of text addressed to an assistant (a planted instruction) is cut
(``orchestrator.planted``). The turn keeps what that needs as its prompt is
assembled: ``material`` (everything the prompt held) and ``planted`` (the
spans: markers on quoted parts of the person's message and of history's,
markers on ERP text, and ``content_scan``'s verified evidence through
``note_planted``). Bounded (``PLANTED_BUDGET_S``, in a thread); on any error
or timeout the reply goes on as it was. The figure check reads the cut text,
so no record of its changes holds a specific; the audit row holds counts
only (``planted_*``), and only on a turn that had a span.

Off with ``ERPNEXT_ANSWER_CHECK=off``. Any error inside the check is logged
and the reply is left as it was: a checker must never cost the person their
answer. A check that runs out of time falls back to code alone (no model
call), which removes what it can't confirm.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from openexecutive.orchestrator import (
    answer_correct,
    figure_check,
    figure_confirm,
    name_check,
    planted,
)
from openexecutive.orchestrator.figure_check import ErpnextResult, Figure, NumberFormat, ResultTypes
from openexecutive.orchestrator.planted import Span as PlantedSpan

logger = logging.getLogger(__name__)

# What the planted-specifics cut may take; past it the reply goes on as it was.
PLANTED_BUDGET_S = 2.0

# The results kept per turn; a turn that reads more checks against the first.
MAX_RESULTS = 60
# What one reply's check may take before it falls back to code alone.
CHECK_BUDGET_S = 45.0
# A stopped or timed-out turn's: the person is waiting for it to wind down.
STOPPED_BUDGET_S = 20.0
CODE_ONLY_BUDGET_S = 10.0
CONTEXT_TIMEOUT_S = 10.0
CORRECTION_TIMEOUT_S = 45.0
CONFIRM_TIMEOUT_S = 15.0
# All of check 3's report runs in a turn, so checks 2 and the correction keep their time.
CONFIRM_BUDGET_S = 20.0
# The ERPNext reads whose results figures come from (gateway names).
_FIGURE_TOOLS = frozenset({"erpnext__list_records", "erpnext__get_record", "erpnext__run_report"})
# An ERPNext result as the model read it. A result cut to the reading budget
# loses its closing tag, so the block may run to the end of its text.
# A question asking what will or would happen: its reply works figures out
# from ERPNext's (a 13-week outlook, "what if Metro pays late"), which no
# result holds, so the check never blanks it for those (_kept).
# Phrases only, each closed by a word boundary: "project" (a DocType),
# "what would be our total …" and "Outlook" (the mail app) are lookups, and a
# lookup's figures stay held to its results (security review of #163).
# Matched only against what the person typed on that surface
# (``typed_question``, _projection): never against a card's headline, a
# file's name or text, a transcript, a forward or an email, which an outsider
# can word ("Updated forecast for Q1") to keep figures no result holds.
_PROJECTION_Q = re.compile(
    r"\b(?:what if|what happens if|forecasts?|forecasting|projections?|projected"
    r"|cash outlook|outlook for|\d+[- ]week (?:outlook|forecast|view|cash)"
    r"|week[- ]by[- ]week|month[- ]by[- ]month|next \d+ (?:days|weeks|months)"
    r"|(?:will|would) (?:it|that|things|cash|we) look|as late as"
    r"|if \w+ (?:pays?|paid) (?:late|later|as late|on time))\b",
    re.I,
)


def _projection(typed_question: str | None) -> bool:
    """Whether the person's own typed words ask for a projection. ``None``
    (no typed text on that path) never relaxes the check."""
    return bool(typed_question) and _PROJECTION_Q.search(typed_question or "") is not None


PROJECTION_NOTE = (
    "_The projected figures above are estimates; "
    "they couldn't be checked against ERPNext's records._"
)


def in_product_words(text: str) -> str:
    """What the check itself writes into a reply (``PROJECTION_NOTE``,
    ``answer_correct.WHOLE_REPLY``) in the product's words on an install with
    a name, "the ERP" for ERPNext (``connected_systems.in_product_reply``):
    added here, so every channel and the evals get the words the person sees
    (eval erpnext_094, Nov 18, 2026: the footer named ERPNext in 14 of 15
    runs). Unchanged on an install without one."""
    from openexecutive.prompts.connected_systems import in_product_reply

    return in_product_reply(text)
_ERPNEXT_BLOCK = re.compile(r'<untrusted_content source="erpnext"[^>]*>.*?(?:</untrusted_content>|\Z)', re.S)


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
    _sources: tuple[NumberFormat, figure_check.Sources, name_check.NameSources] | None = None
    # Check 3's report runs this turn: the quick total asked about -> the report's total.
    _reports: dict[str, float | None] = field(default_factory=dict)
    _done: tuple[str, str] | None = None
    # Everything the turn's prompt held, raw, as it was assembled (the
    # planted cut's material: orchestrator.planted), and the spans of text
    # addressed to an assistant it read.
    material: list[str] = field(default_factory=list)
    planted: list[PlantedSpan] = field(default_factory=list)
    _material_chars: int = 0
    # More material than planted.MAX_MATERIAL_CHARS: the cut doesn't run.
    _material_full: bool = False
    # The session's evidence spans, carried to its later turns (carry_from).
    _carried: list[PlantedSpan] | None = None

    def note_result(self, tool: str, arguments: Any, text: object) -> None:
        payload = figure_check.parse_result(text)
        if payload is None or len(self.results) >= MAX_RESULTS:
            return
        args = arguments if isinstance(arguments, dict) else {}
        self.results.append(ErpnextResult(tool, args, payload))

    def note_given(self, *texts: object) -> None:
        self.given.extend(t for t in texts if isinstance(t, str) and t.strip())
        for t in texts:
            self.note_material(t)

    def note_material(self, text: object) -> None:
        """Text the prompt held, for the planted cut. Past the cap it is
        noted as full, and the cut doesn't run (planted.MAX_MATERIAL_CHARS)."""
        if not isinstance(text, str) or not text.strip():
            return
        if self._material_chars + len(text) > planted.MAX_MATERIAL_CHARS:
            self._material_full = True
            return
        self.material.append(text)
        self._material_chars += len(text)

    def note_spans(self, spans: Iterable[PlantedSpan]) -> None:
        for span in spans:
            if len(self.planted) >= planted.MAX_SPANS:
                return
            if span.text.strip() and span not in self.planted:
                self.planted.append(span)

    def note_person(self, text: object) -> None:
        """The person's own words (this turn's, or history's): markers only
        inside what they quoted or pasted; any ERPNext block in them (a
        page's) is ERP text."""
        if not isinstance(text, str) or not text:
            return
        self.note_spans(planted.markers(_ERPNEXT_BLOCK.sub("", text), pasted_only=True))
        self._note_erp_blocks(text)

    def note_erpnext_text(self, text: object) -> None:
        """An ERPNext call's result text: material, and markers anywhere."""
        raw = text if isinstance(text, str) else _as_text(text)
        self.note_material(raw)
        self.note_spans(planted.markers(raw, pasted_only=False))

    def note_evidence(self, text: str, evidence: str, kind: str) -> None:
        span = planted.evidence_span(text, evidence, kind)
        if span is None:
            return
        self.note_spans([span])
        if self._carried is not None and span not in self._carried:
            self._carried.append(span)
            del self._carried[: max(0, len(self._carried) - planted.MAX_SPANS)]

    def carry_from(self, session: Any) -> None:
        """The evidence spans ``session``'s earlier turns found, so a span
        stays a span when history repeats its text; this turn's join them."""
        carried = getattr(session, "planted_spans", None)
        if isinstance(carried, list):
            self.note_spans(s for s in carried if isinstance(s, PlantedSpan))
            self._carried = carried

    def _note_erp_blocks(self, text: str) -> None:
        if "untrusted_content" not in text:
            return
        for block in _ERPNEXT_BLOCK.finditer(text):
            self.note_spans(planted.markers(block.group(0), pasted_only=False))

    def note_texts(self, contents: Iterable[Any]) -> None:
        """Text the turn read: message contents or tool results, with
        ERPNext's own results left out (their values are typed, above).
        All of it, ERPNext's too, is the planted cut's material."""
        for content in contents:
            for text in _texts(content):
                self.note_material(text)
                self._note_erp_blocks(text)
                stripped = _ERPNEXT_BLOCK.sub("", text)
                if stripped.strip():
                    self.texts.append(stripped)

    def note_messages(self, messages: Iterable[dict[str, Any]], system_blocks: Iterable[Any] = ()) -> None:
        """The conversation the turn starts from, and its system blocks.
        History's user messages get the person's markers, so a note pasted
        in an earlier turn is still a span in this one."""
        messages = [m for m in messages if isinstance(m, dict)]
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                self.note_person(m["content"])
        self.note_texts(m.get("content") for m in messages)
        self.note_texts(b.get("text") if isinstance(b, dict) else b for b in system_blocks)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    try:
        return value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


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


def _keep_whole() -> bool:
    """``ERPNEXT_ANSWER_CHECK=correct_keep``: figures are put right in place,
    and a reply the check would replace whole is kept with its known values
    written in, never replaced (decided Nov 5, 2026)."""
    from openexecutive.config import get_settings

    return get_settings().erpnext_answer_check == "correct_keep"


def note_erpnext_result(normalized: str, arguments: Any, text: object, *, failed: bool) -> None:
    """Called by the gateway after every ``erpnext__*`` call: on a turn being
    checked, keep a read's result."""
    turn = _bound.get()
    if turn is None or failed:
        return
    # Every ERPNext result's text: the planted cut's material and markers.
    turn.note_erpnext_text(text)
    if normalized not in _FIGURE_TOOLS:
        return
    turn.note_result(normalized, arguments, text)


def note_planted(text: object, evidence: object, categories: Iterable[str] = ()) -> None:
    """Called by ``outside_content`` for a read ``content_scan`` flagged: the
    span holding its evidence, when the evidence is verifiably the read's own
    words (``content_scan._quoted``). Never raises."""
    turn = _bound.get()
    if turn is None or not isinstance(evidence, str) or not evidence.strip():
        return
    try:
        from openexecutive.orchestrator import content_scan

        raw = content_scan._text(text)
        if not content_scan._quoted(evidence, raw):
            return
        kind = next((c for c in categories if c in content_scan.CATEGORIES), "other")
        turn.note_evidence(raw, evidence, kind)
    except Exception:
        logger.warning("answer_check: noting a flagged read's span failed")


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
    names: int = 0
    names_flagged: int = 0
    report_runs: int = 0
    report_differs: int = 0
    code_only: bool = False
    # The whole-reply sentence avoided: names alone failed, or a projection.
    kept: int = 0
    # Weekdays put right beside their dates (weekday_check).
    weekdays: int = 0
    # Record ids given their titles (record_titles).
    titles: int = 0
    # The planted cut (orchestrator.planted): spans the turn had, units cut,
    # whether its line was added, and whether the half-reply cap held.
    planted_spans: int = 0
    planted_cut: int = 0
    planted_appended: int = 0
    planted_capped: int = 0
    # Each failed figure or name as written, and why it failed: for the
    # private record of what the check changed (_audit_changes).
    reasons: list[tuple[str, str]] = field(default_factory=list)


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


async def _sources(turn: AnswerCheckTurn) -> tuple[NumberFormat, figure_check.Sources, name_check.NameSources]:
    """The turn's figure and name sources, read once (the code-only fallback
    reuses them)."""
    if turn._sources is None:
        context = await _context(turn)
        fmt = NumberFormat.parse(context.get("number_format"))
        doctypes = context.get("doctypes")
        described = doctypes if isinstance(doctypes, dict) else {}
        types = ResultTypes(described)

        def read() -> tuple[figure_check.Sources, name_check.NameSources]:
            figures = figure_check.sources_for(
                turn.results, types=types, given=turn.given, texts=turn.texts, fmt=fmt,
                currencies=context.get("currencies"),
            )
            names = name_check.name_sources_for(
                turn.results, doctypes=described, given=turn.given, texts=figure_check._capped(turn.texts),
            )
            return figures, names

        sources, names = await asyncio.to_thread(read)
        turn._sources = (fmt, sources, names)
    return turn._sources


async def _report(turn: AnswerCheckTurn, quick: figure_confirm.QuickTotal) -> float | None:
    """oe_connector's ``confirm_figure`` for ``quick``: the report's total as
    the turn's own caller, or None (refused, failed, cut off, not there)."""
    from openexecutive.orchestrator import erpnext_caller
    from openexecutive.orchestrator.mcp_gateway import gateway_server_names, get_active_gateway
    from openexecutive.prompts.connected_systems import ERPNEXT_CONFIRM_TOOL, ERPNEXT_SERVER

    gateway = get_active_gateway()
    if gateway is None or ERPNEXT_SERVER not in gateway_server_names(gateway):
        return None
    try:
        with erpnext_caller.answer_check_run():
            raw = await asyncio.wait_for(
                gateway.call_tool({"name": ERPNEXT_CONFIRM_TOOL, "arguments": quick.arguments()}), CONFIRM_TIMEOUT_S,
            )
    except Exception:
        logger.warning("answer_check: confirm_figure failed; the figure stands as check 1 left it", exc_info=True)
        return None
    return figure_confirm.report_total(figure_check.parse_result(raw))


async def _confirm(turn: AnswerCheckTurn, figures: list[Figure]) -> list[tuple[Figure, float]]:
    """Check 3 (``figure_confirm``): each figure check 1 matched to a
    measure's quick-way total, asked of its report, at most MAX_CONFIRMS
    reports a turn. A figure the report agrees with is confirmed; the ones it
    doesn't, with the report's total."""
    from openexecutive.orchestrator import erpnext_caller

    if not erpnext_caller.signing_setting():
        # Phase 1: no person's caller to run the report as; oe_connector refuses.
        return []
    context = await _context(turn)
    totals = figure_confirm.quick_totals(turn.results, context.get("measures"), date.today())
    differs: list[tuple[Figure, float]] = []
    deadline = asyncio.get_running_loop().time() + CONFIRM_BUDGET_S
    for fig in figures:
        if not totals or fig.matched is None or fig.status not in ("confirmed", "from_truncated"):
            continue
        quick = next(
            (
                q for q in totals
                if abs(q.value - abs(fig.matched)) <= max(q.value, 1.0) * 1e-9
                # A total added up from rows only for a figure check 1 read as
                # that sum, never one row's value that happens to equal it.
                and (not q.from_rows or fig.how.startswith("sum of "))
            ),
            None,
        )
        if quick is None:
            continue
        key = json.dumps(quick.arguments(), sort_keys=True)
        if key not in turn._reports:
            left = deadline - asyncio.get_running_loop().time()
            if len(turn._reports) >= figure_confirm.MAX_CONFIRMS or left <= 0:
                continue
            try:
                turn._reports[key] = await asyncio.wait_for(_report(turn, quick), left)
            except TimeoutError:
                turn._reports[key] = None
        total = turn._reports[key]
        if total is None:
            continue
        if fig.matches(total):
            fig.status, fig.how = "confirmed", f"ERPNext's report for {quick.measure}"
        else:
            differs.append((fig, total))
    return differs


def _passes(text: str, sources: figure_check.Sources, names: name_check.NameSources, fmt: NumberFormat) -> bool:
    """Whether every figure and name of ``text`` passes, read as the first
    pass reads them (names never across a figure)."""
    figures = figure_check.check_figures(text, sources, fmt)
    if any(f.flagged for f in figures):
        return False
    return not any(n.flagged for n in name_check.check_names(text, names, figures=[(f.start, f.end) for f in figures]))


async def _check(
    text: str,
    turn: AnswerCheckTurn,
    *,
    question: str,
    model: str,
    models: bool,
    counts: _Counts,
    typed_question: str | None = None,
) -> str:
    from openexecutive.orchestrator.answer_review import CANNOT_TELL, review

    fmt, sources, names = await _sources(turn)
    # Off the event loop: reading a long result or conversation takes time.
    figures = await asyncio.to_thread(figure_check.check_figures, text, sources, fmt)
    named = await asyncio.to_thread(
        lambda: name_check.check_names(text, names, figures=[(f.start, f.end) for f in figures])
    )
    counts.figures = len(figures)
    flagged = [f for f in figures if f.flagged]
    counts.flagged = len(flagged)
    wrong_names = [n for n in named if n.flagged]
    counts.names = len(named)
    counts.names_flagged = len(wrong_names)
    # Check 3: a headline figure added up from a list read, held to its
    # standard report. A report that agrees clears a total over a cut-off list
    # too; one that differs gives the figure its right value.
    reported = await _confirm(turn, figures) if models and not turn.brief else []
    counts.report_runs = len(turn._reports)
    counts.report_differs = len(reported)
    differs = {id(fig) for fig, _ in reported}
    # A figure in the wrong currency has its own value; only its mark changes.
    recurrenced = [f for f in figures if f.status == "wrong_currency" and f.matched is not None]
    differs |= {id(f) for f in recurrenced}
    reported = [*reported, *((f, abs(f.matched)) for f in recurrenced if f.matched is not None)]
    # A result the reply's own arithmetic gives another value for ("5 × $71.00
    # = $5.00"): written in by code, never reviewed, rewritten or left out.
    reworked = [f for f in figures if f.flagged and f.arithmetic is not None and id(f) not in differs]
    differs |= {id(f) for f in reworked}
    reported = [*reported, *((f, f.arithmetic) for f in reworked if f.arithmetic is not None)]
    flagged = [f for f in figures if f.flagged and id(f) not in differs]
    counts.flagged = len(flagged) + len(reported)
    if not flagged and not wrong_names and not reported:
        return text
    reviews = (
        await review(question=question, draft=text, flagged=flagged, results=turn.results, sources=sources)
        if models and flagged
        else [CANNOT_TELL] * len(flagged)
    )
    counts.reviewed = len(flagged) if models else 0
    # A name close to exactly one source name is not known for any other
    # name, for the correction to leave out.
    corrections: list[answer_correct.Correction] = [
        answer_correct.Correction(n, None) for n in wrong_names if n.status != "near"
    ]
    known: list[float] = []
    for fig, total in reported:
        corrections.append(answer_correct.Correction(fig, total))
        known.append(total)
    for fig, verdict in zip(flagged, reviews, strict=True):
        if verdict.verdict == "supported" and verdict.right is not None:
            counts.supported += 1
            known.append(verdict.right)
        else:
            corrections.append(answer_correct.Correction(fig, verdict.right if verdict.verdict == "wrong" else None))
            if verdict.right is not None:
                known.append(verdict.right)
    checked = sources.plus(known)
    # A name close to exactly one source name is that name, written in by code
    # first: a source name, which a customer may have written, never reaches
    # the correction call's prompt (answer_correct.write_names).
    near = [answer_correct.Correction(n, n.right) for n in wrong_names if n.status == "near"]
    counts.reasons = [(c.figure.text, _why(c)) for c in [*near, *corrections]]
    checked_names = names.plus(str(c.right) for c in near)
    if near:
        text, corrections, figures = answer_correct.write_names(text, near, corrections, figures)
        counts.substituted = len(near)
    if not corrections:
        if _passes(text, checked, checked_names, fmt):
            return text
        if any(f.flagged for f in figure_check.check_figures(text, checked, fmt)):
            # A figure fails after all (the names written in moved it): as before.
            if _keep_whole():
                counts.kept = 1
                return text
            counts.whole_reply = 1
            counts.substituted = 0
            turn.whole_replaced = True
            return answer_correct.WHOLE_REPLY
        return _whole(text, [], turn, typed_question=typed_question, counts=counts)
    failed = [c.figure for c in corrections]
    if not answer_correct.needs_rewrite(text, corrections):
        substituted = answer_correct.substitute(text, corrections)
        if _passes(substituted, checked, checked_names, fmt):
            counts.substituted += len(corrections)
            return substituted
    if _projection(typed_question) and any(
        isinstance(c.figure, Figure) and c.right is None for c in corrections
    ):
        # Worked-out figures no result holds: never cut from a projection,
        # whose weeks or cases would go missing (_whole).
        return _whole(text, corrections, turn, typed_question=typed_question, counts=counts)
    if models:
        try:
            rewrite = await _rewrite(question, text, corrections, model)
        except Exception as exc:
            from openexecutive.audit import cost_caps

            cap = cost_caps.capped(exc)
            if cap is not None:
                raise cap from None
            logger.warning("answer_check: the correction call failed; removing instead", exc_info=True)
            rewrite = None
        if (
            rewrite
            and answer_correct.changed_only_within(text, rewrite, answer_correct.allowed_spans(text, failed))
            and _passes(rewrite, checked, checked_names, fmt)
        ):
            counts.model_corrected = len(corrections)
            return rewrite
    fixed = answer_correct.fallback(text, corrections, figures)
    if fixed != answer_correct.WHOLE_REPLY and not _passes(fixed, checked, checked_names, fmt):
        # What code wrote must pass as a rewrite must; else the decided sentence.
        fixed = answer_correct.WHOLE_REPLY
    if fixed == answer_correct.WHOLE_REPLY:
        return _whole(text, corrections, turn, typed_question=typed_question, counts=counts)
    else:
        written = sum(1 for c in corrections if c.right is not None and answer_correct.written(c) in fixed)
        counts.substituted += written
        counts.left_out = len(corrections) - written
    return fixed


def _whole(
    text: str,
    corrections: list[answer_correct.Correction],
    turn: AnswerCheckTurn,
    *,
    typed_question: str | None,
    counts: _Counts,
) -> str:
    """What a reply the check would replace whole becomes, and a projection
    with figures no result holds.

    - Only names failed (no figure did): the reply stands. A name the check
      can't place is not a figure ERPNext contradicts, and blanking a reply
      whose every figure passed hid the answer (the pilot's Nov 5, 2026
      retake, "what's our cash position").
    - A projection (``_PROJECTION_Q`` on ``typed_question``): every known
      value is written in, and the figures nothing contradicts stand, under
      ``PROJECTION_NOTE``.
    - With ``correct_keep``: every known value written in, the rest kept.
    - Otherwise ``WHOLE_REPLY``, as before.
    """
    figure_fixes = [c for c in corrections if isinstance(c.figure, Figure)]
    if not figure_fixes:
        counts.kept = 1
        counts.left_out = 0
        return text
    if _projection(typed_question):
        known = [c for c in figure_fixes if c.right is not None]
        out = answer_correct.substitute(text, known) if known else text
        counts.kept = 1
        counts.substituted += sum(1 for c in known if answer_correct.written(c) in out)
        counts.left_out = 0
        return f"{out.rstrip()}\n\n{in_product_words(PROJECTION_NOTE)}"
    if _keep_whole():
        known = [c for c in figure_fixes if c.right is not None]
        out = answer_correct.substitute(text, known) if known else text
        counts.kept = 1
        counts.substituted += sum(1 for c in known if answer_correct.written(c) in out)
        counts.left_out = 0
        return out
    counts.whole_reply = 1
    counts.substituted = 0
    turn.whole_replaced = True
    return answer_correct.WHOLE_REPLY


def _why(c: answer_correct.Correction) -> str:
    """Why a figure or name failed, and what the check did with it, in a few
    words for the principal's record."""
    item = c.figure
    if isinstance(item, Figure):
        if item.status == "wrong_currency":
            why = "written in another currency than its value"
        elif item.arithmetic is not None:
            why = "the reply's own arithmetic gives another result"
        elif item.status == "from_truncated":
            why = "found only in a result that was cut short"
        else:
            why = "no value in the turn's ERPNext results matches it"
    else:
        why = "close to one name the results hold" if item.status == "near" else "a name no result holds"
    return f"{why}; {'written as ' + answer_correct.written(c) if c.right is not None else 'left out'}"


# How long the record of what the check changed keeps its sentences (the
# owner, Nov 8, 2026): the row and its counts stay, the text goes.
CHANGES_KEPT_DAYS = 30
# At most this many changed sentences per reply, each cut to this length.
_CHANGES_MAX = 12
_SENTENCE_MAX = 600


def changed_sentences(before: str, after: str, reasons: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """The sentences (list items, table rows) ``after`` changed from
    ``before``: each one as it was and as it went out, with the failed
    figures or names it held and why. Never the rest of the reply."""
    import difflib

    def parts(text: str) -> list[str]:
        if text == answer_correct.WHOLE_REPLY:
            return [text]
        return [text[u.start:u.end].strip() for u in answer_correct.units(text) if text[u.start:u.end].strip()]

    old, new = parts(before), parts(after)
    out: list[dict[str, Any]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        was = " ".join(old[i1:i2])
        held = [{"as_written": text, "why": why} for text, why in reasons if text and text in was]
        out.append({
            "before": was[:_SENTENCE_MAX],
            "after": " ".join(new[j1:j2])[:_SENTENCE_MAX],
            "figures": held,
        })
        if len(out) >= _CHANGES_MAX:
            break
    return out


def _audit_changes(before: str, after: str, counts: _Counts) -> None:
    """The principal's record of what the check changed in this reply: the
    changed sentences only, the principal's alone whoever's turn it was
    (never a team member's own row), their text kept CHANGES_KEPT_DAYS days
    (clear_old_changes)."""
    from openexecutive.audit import log_event as audit_log
    from openexecutive.audit.logger import _mailbox_scope

    if _mailbox_scope():
        # A turn that read someone's own mailbox is theirs alone, the
        # principal included: its sentences could quote their mail.
        return
    changes = changed_sentences(before, after, counts.reasons)
    if not changes:
        return
    audit_log(
        "answer_check_changes",
        f"Answer check changed {len(changes)} sentence(s)",
        actor="answer_check",
        details={"sentences": len(changes), "kept_days": CHANGES_KEPT_DAYS},
        full={"changes": changes},
        principal_only=True,
    )
    clear_old_changes(datetime.now(UTC), force=True)


# The day the old records' text was last cleared, in this process.
_cleared_on: date | None = None


def clear_old_changes(now: datetime, *, force: bool = False) -> int:
    """Clear the text of the change records older than CHANGES_KEPT_DAYS:
    once a day from the scheduler, and on every new record. Never raises."""
    global _cleared_on
    from openexecutive.audit.logger import get_audit_logger

    today = now.astimezone(UTC).date()
    if not force and _cleared_on == today:
        return 0
    _cleared_on = today
    try:
        return get_audit_logger().drop_full("answer_check_changes", now - timedelta(days=CHANGES_KEPT_DAYS))
    except Exception:
        logger.warning("answer_check: clearing old change records failed", exc_info=True)
        return 0


def _audit(counts: _Counts, *, changed: bool) -> None:
    from openexecutive.audit import log_event as audit_log

    # Only on a turn that had a span: every other row stays as it was.
    planted_counts = {
        "planted_spans": counts.planted_spans,
        "planted_cut": counts.planted_cut,
        "planted_appended": counts.planted_appended,
        "planted_capped": counts.planted_capped,
    } if counts.planted_spans else {}
    audit_log(
        "answer_check",
        f"Answer check: {counts.figures} figure(s), {counts.flagged} flagged"
        + (", reply corrected" if changed else ""),
        actor="answer_check",
        # Counts only: no figure or text, so the audit holds no ERPNext data.
        details={
            **planted_counts,
            "figures": counts.figures,
            "flagged": counts.flagged,
            "reviewed": counts.reviewed,
            "supported": counts.supported,
            "substituted": counts.substituted,
            "model_corrected": counts.model_corrected,
            "left_out": counts.left_out,
            "whole_reply": counts.whole_reply,
            "names": counts.names,
            "names_flagged": counts.names_flagged,
            "report_runs": counts.report_runs,
            "report_differs": counts.report_differs,
            "code_only": counts.code_only,
            "kept": counts.kept,
            "weekdays": counts.weekdays,
            "titles": counts.titles,
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
    typed_question: str | None = None,
) -> str:
    """``text`` with its figures held to the turn's ERPNext results: the same
    string when nothing changed. Checks a turn's reply once (a second call
    with the same text returns the first answer).

    Its calls are kept off the turn's cost cap (never the others): a cap
    must never switch the check off. When a cap refuses one, it raises
    ``CostCapReached``: the reply is held, never passed unchecked (the
    caller withholds what it hasn't shown yet). Nothing else raises.

    ``question`` is what the turn was asked (the person's words, or a card's
    or an email's text): context for the correction and review calls only,
    wrapped as untrusted there. ``typed_question`` is the text the person
    typed on that surface, or ``None`` where there is none; it alone decides
    whether figures no result holds stand as a projection (_projection).

    First, on every turn with a span, whether or not it read ERPNext or the
    check is on: the planted cut (``_cut_planted``). What follows reads the
    cut text."""
    if turn is None or not text.strip():
        return text
    if turn._done is not None and turn._done[0] == text:
        return turn._done[1]
    counts = _Counts()
    original, text = text, await _cut_planted(text, turn, counts)
    if not turn.results or not enabled():
        if counts.planted_spans:
            turn._done = (original, text)
            try:
                _audit(counts, changed=text != original)
            except Exception:
                logger.warning("answer_check: audit row failed", exc_info=True)
        return text
    if not model:
        from openexecutive.config import get_settings

        model = get_settings().default_model
    from openexecutive.audit import cost_caps

    try:
        with cost_caps._check_exempt():
            try:
                fixed = await asyncio.wait_for(
                    _check(
                        text, turn, question=question, model=model, models=True, counts=counts,
                        typed_question=typed_question,
                    ),
                    budget_s,
                )
            except TimeoutError:
                logger.warning("answer_check: out of time; checking with code alone")
                counts = _Counts(
                    code_only=True, planted_spans=counts.planted_spans, planted_cut=counts.planted_cut,
                    planted_appended=counts.planted_appended, planted_capped=counts.planted_capped,
                )
                fixed = await asyncio.wait_for(
                    _check(
                        text, turn, question=question, model=model, models=False, counts=counts,
                        typed_question=typed_question,
                    ),
                    CODE_ONLY_BUDGET_S,
                )
    except Exception as exc:
        cap = cost_caps.capped(exc)
        if cap is not None:
            logger.warning("answer_check: a cost cap refused the check's call; the reply is held")
            raise cap from None
        logger.exception("answer_check: the check failed; the reply is left as it was")
        return text
    if fixed != answer_correct.WHOLE_REPLY:
        try:
            from openexecutive.orchestrator import weekday_check

            # Off the event loop, and bounded: a reply is long text.
            fixed, counts.weekdays = await asyncio.wait_for(
                asyncio.to_thread(weekday_check.fix, fixed, date.today()), 2.0
            )
        except Exception:
            logger.warning("answer_check: the weekday check failed", exc_info=True)
        try:
            from openexecutive.orchestrator import record_titles

            # A record named by its id alone gets its title (supplier,
            # customer, project name) from the turn's own reads.
            fixed, counts.titles = await asyncio.wait_for(
                asyncio.to_thread(record_titles.add_titles, fixed, list(turn.results)), 2.0
            )
        except Exception:
            logger.warning("answer_check: adding record titles failed", exc_info=True)
    turn._done = (original, fixed)
    try:
        _audit(counts, changed=fixed != original)
    except Exception:
        logger.warning("answer_check: audit row failed", exc_info=True)
    if fixed != text:
        try:
            # From the cut text: the record never holds a planted specific.
            _audit_changes(text, fixed, counts)
        except Exception:
            logger.warning("answer_check: the record of its changes failed", exc_info=True)
    if fixed == answer_correct.WHOLE_REPLY:
        # Recorded as the decided sentence; shown in the product's words.
        fixed = in_product_words(fixed)
        turn._done = (original, fixed)
    return fixed


async def _cut_planted(text: str, turn: AnswerCheckTurn, counts: _Counts) -> str:
    """``text`` with the sentences that repeat a planted instruction's
    specifics cut (``orchestrator.planted``): off the event loop, bounded by
    PLANTED_BUDGET_S. Unchanged on a turn with no span, with more material
    than it keeps, or on any error or timeout. Counts only; never logs the
    text."""
    if not turn.planted:
        return text
    spans = list(turn.planted)
    counts.planted_spans = len(spans)
    if turn._material_full:
        logger.warning("answer_check: the turn read more than the planted cut keeps; the reply is left as it was")
        return text
    try:
        outcome = await asyncio.wait_for(
            asyncio.to_thread(planted.cut, text, spans, list(turn.material)), PLANTED_BUDGET_S,
        )
    except Exception as exc:
        # The exception's type only: its message could quote the text.
        logger.warning("answer_check: the planted cut failed (%s); the reply is left as it was", type(exc).__name__)
        return text
    counts.planted_cut = outcome.cut
    counts.planted_appended = int(outcome.appended)
    counts.planted_capped = int(outcome.capped)
    return outcome.text
