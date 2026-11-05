"""Correcting a reply's failed figures (oe_connector's ``docs/answer-check-plan.md``,
section 5), without ever saying so.

Three steps, cheapest first, each checked in code:

1. **Substitution.** A failed figure whose right value is known (check 2's,
   once code found it in ERPNext's results) is replaced in code, written as the
   reply wrote it: the same currency mark, separators, scale and decimals.
2. **The correction call**, only when a value isn't known, or when the words
   around a substituted figure depend on it (a comparison, a ranking). One call
   to the turn's own model with no tools: a constant system prompt
   (``prompts.answer_check.CORRECTION_SYSTEM``, nothing cached), and in the user
   turn the question, the corrections built in code, and the draft wrapped as
   untrusted content. It never sees ERPNext's results. Its rewrite is kept only
   when, outside the units holding failed figures, it matches the draft token
   for token (``changed_only_within``), and every figure in it passes check 1
   again. There is never a second call.
3. **Removal**, when the rewrite fails or the call does: the sentence, list item
   or table row holding each failed figure goes, found in the reply's Markdown.
   When that would take the first sentence or a heading, a table's header or
   total row, leave a table or list empty, or leave fewer than half the reply's
   figures, the whole reply becomes ``WHOLE_REPLY`` instead (question 9).

Nothing here says a figure was wrong, checked or corrected: the person reads
only the final text.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from openexecutive.orchestrator.figure_check import Figure, format_like

# Decided Nov 4, 2026 (question 9). It never mentions checking.
WHOLE_REPLY = "I couldn't get those figures from ERPNext reliably. Open the report to see them."

# Words that make a sentence depend on its figures' values: substituting a
# value there may need the words around it changed too.
_DEPENDS = re.compile(
    r"\b(?:rose|risen|rise|rises|fell|fallen|fall|falls|increase[ds]?|decrease[ds]?|grew|grown|grow|"
    r"dropped|drop|drops|declined?|up|down|higher|lower|more|less|fewer|above|below|exceed(?:s|ed)?|"
    r"largest|smallest|biggest|top|bottom|most|least|ahead|behind|over|under|double[ds]?|half|"
    r"twice|times|compared|versus|vs|than)\b",
    re.I,
)
_TOKEN = re.compile(r"\w+|[^\w\s]")
_LINK = re.compile(
    r"(?i)[a-z][a-z0-9+.-]*:[^\s)>\]]+"  # any scheme: https:, mailto:, tel:
    r"|\]\([^)]*\)"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"  # an email
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b"  # a domain
)
_STOP = re.compile(r"[.!?](?=\s|$)")


@dataclass(frozen=True)
class Unit:
    """A piece of the reply that is removed whole: a sentence of prose, a list
    item (with the items under it), a table row, or a heading."""

    start: int
    end: int
    kind: str  # "sentence", "item", "row", "header", "heading", "code"
    # The first sentence of the reply, a table's total row.
    lead: bool = False
    # Which list or table it belongs to (its first line's offset), or -1.
    block: int = -1


_ITEM = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)])[ \t]+")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]*_]*\s+(?=[\"'(\[*_]*[A-Z0-9$€£¥₹])")


def units(text: str) -> list[Unit]:
    """The reply's removable units, in order (see ``Unit``)."""
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    out: list[Unit] = []
    i = 0
    in_code = False
    seen_prose = False
    while i < len(lines):
        line, start = lines[i], offsets[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            end = offsets[j] + len(lines[j]) if j < len(lines) else len(text)
            out.append(Unit(start, end, "code"))
            i = j + 1
            in_code = False
            continue
        if in_code or not stripped:
            i += 1
            continue
        if stripped.startswith("#"):
            out.append(Unit(start, start + len(line), "heading", lead=True))
            seen_prose = True
            i += 1
            continue
        if stripped.startswith("|"):
            block = start
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            for k in range(i, j):
                row = lines[k].strip()
                kind = "header" if k == i or (k == i + 1 and set(row) <= set("|:- ")) else "row"
                cells = [c.strip(" *_") for c in row.strip("|").split("|")]
                total = kind == "row" and (
                    bool(cells) and re.match(r"(?i)^(grand\s+)?totals?\b", cells[0] or "") is not None
                    or row.replace(" ", "").startswith("|**")
                )
                out.append(Unit(offsets[k], offsets[k] + len(lines[k]), kind, lead=total, block=block))
            seen_prose = True
            i = j
            continue
        item = _ITEM.match(line)
        if item:
            # One list: items until a blank line followed by a non-item, or a
            # line that is neither an item nor indented under one.
            block = start
            j = i
            while j < len(lines):
                here = lines[j]
                if not here.strip():
                    nxt = lines[j + 1] if j + 1 < len(lines) else ""
                    if _ITEM.match(nxt) or (nxt.startswith((" ", "\t")) and nxt.strip()):
                        j += 1
                        continue
                    break
                if not (_ITEM.match(here) or here.startswith((" ", "\t"))):
                    break
                j += 1
            k = i
            while k < j:
                head = _ITEM.match(lines[k])
                if not head:
                    k += 1
                    continue
                indent = len(head.group(1).expandtabs())
                m = k + 1
                while m < j:
                    sub = _ITEM.match(lines[m])
                    if sub and len(sub.group(1).expandtabs()) <= indent:
                        break
                    if not sub and lines[m].strip() and not lines[m].startswith((" ", "\t")):
                        break
                    m += 1
                end = offsets[m - 1] + len(lines[m - 1])
                out.append(Unit(offsets[k], end, "item", block=block))
                k += 1
            seen_prose = True
            i = j
            continue
        # A paragraph: lines until a blank line or another block.
        j = i
        while j < len(lines) and lines[j].strip() and not (
            lines[j].strip().startswith(("#", "|", "```")) or _ITEM.match(lines[j])
        ):
            j += 1
        para_start = start
        para_end = offsets[j - 1] + len(lines[j - 1])
        body = text[para_start:para_end]
        cuts = [0, *(m.end() for m in _SENTENCE_END.finditer(body)), len(body)]
        for a, b in zip(cuts, cuts[1:], strict=False):
            if body[a:b].strip():
                out.append(Unit(para_start + a, para_start + b, "sentence", lead=not seen_prose))
                seen_prose = True
        i = j
    return out


def unit_of(all_units: list[Unit], start: int, end: int) -> Unit | None:
    """The smallest unit holding ``[start, end)``."""
    holding = [u for u in all_units if u.start <= start and end <= u.end]
    return min(holding, key=lambda u: u.end - u.start) if holding else None


@dataclass(frozen=True)
class Correction:
    """A failed figure, and its right value when one is known."""

    figure: Figure
    right: float | None


def substitute(text: str, corrections: Iterable[Correction]) -> str:
    """``text`` with each correction's known value written in its figure's place."""
    out = text
    for c in sorted(corrections, key=lambda c: c.figure.start, reverse=True):
        if c.right is None:
            continue
        out = out[: c.figure.start] + format_like(c.figure, c.right) + out[c.figure.end:]
    return out


def needs_rewrite(text: str, corrections: Iterable[Correction]) -> bool:
    """Whether code alone can't fix these: a value that isn't known, or a
    substituted one whose sentence compares or ranks by it."""
    all_units = units(text)
    for c in corrections:
        if c.right is None:
            return True
        unit = unit_of(all_units, c.figure.start, c.figure.end)
        if unit is not None and _DEPENDS.search(text[unit.start:unit.end]):
            return True
    return False


def _tokens(text: str) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in _TOKEN.finditer(text)]


def changed_only_within(draft: str, rewrite: str, allowed: list[tuple[int, int]]) -> bool:
    """Whether ``rewrite`` matches ``draft`` token for token outside the
    ``allowed`` spans of the draft (the units holding failed figures).
    Whitespace and line breaks aside. A change must lie within one span; an
    insertion between two of its tokens, or at its end before its closing
    stop. No change may add a sentence, a heading, a table cell or a link (an
    address, a domain or an email the draft didn't have)."""
    if set(_LINK.findall(rewrite)) - set(_LINK.findall(draft)):
        return False
    old, new = _tokens(draft), _tokens(rewrite)

    def span_of(pos: int) -> int:
        return next((n for n, (a, b) in enumerate(allowed) if a <= pos < b), -1)

    # Sentence stops (a stop followed by a space or the end, never a decimal
    # point), headings and table cells, by position.
    marks_old = {m.start() for m in _STOP.finditer(draft)} | {i for i, c in enumerate(draft) if c in "#|"}
    marks_new = {m.start() for m in _STOP.finditer(rewrite)} | {i for i, c in enumerate(rewrite) if c in "#|"}

    def structure(marks: set[int], toks: list[tuple[str, int]], j1: int, j2: int) -> int:
        return sum(1 for k in range(j1, j2) if toks[k][1] in marks)

    matcher = difflib.SequenceMatcher(None, [t for t, _ in old], [t for t, _ in new], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        added = structure(marks_new, new, j1, j2)
        if i1 == i2:
            before = span_of(old[i1 - 1][1]) if i1 > 0 else -1
            after = span_of(old[i1][1]) if i1 < len(old) else -1
            if before < 0 or (after != before and old[i1 - 1][1] in marks_old) or added:
                return False
            continue
        spans = {span_of(old[k][1]) for k in range(i1, i2)}
        if len(spans) != 1 or -1 in spans or added > structure(marks_old, old, i1, i2):
            return False
    return True


def fallback(text: str, corrections: list[Correction], every: list[Figure]) -> str:
    """The code-only fix: each known value written in, and the unit of every
    other failed figure removed (``remove``). A known value whose sentence
    compares or ranks by it is removed too, since its words may no longer fit."""
    all_units = units(text)
    keep: list[Correction] = []
    gone: list[Figure] = []
    for c in corrections:
        unit = unit_of(all_units, c.figure.start, c.figure.end)
        depends = unit is not None and _DEPENDS.search(text[unit.start:unit.end]) is not None
        if c.right is not None and not depends:
            keep.append(c)
        else:
            gone.append(c.figure)
    return remove(text, gone, every, keep=keep) if gone else substitute(text, keep)


def remove(text: str, failed: list[Figure], every: list[Figure], *, keep: Iterable[Correction] = ()) -> str:
    """``text`` without the unit holding each failed figure, or
    ``WHOLE_REPLY`` when leaving them out could mislead (module docstring).
    ``keep``'s known values are written in, in the same pass."""
    all_units = units(text)
    gone: set[Unit] = set()
    for fig in failed:
        unit = unit_of(all_units, fig.start, fig.end)
        if unit is None or unit.lead or unit.kind in ("heading", "header", "code"):
            return WHOLE_REPLY
        gone.add(unit)
    # A list or table left with no items or rows.
    for block in {u.block for u in gone if u.block >= 0}:
        members = [u for u in all_units if u.block == block and u.kind in ("item", "row")]
        top = [u for u in members if not any(o is not u and o.start <= u.start and u.end <= o.end for o in members)]
        if all(any(g.start <= u.start and u.end <= g.end for g in gone) for u in top):
            return WHOLE_REPLY
    left = [f for f in every if not any(u.start <= f.start and f.end <= u.end for u in gone)]
    if every and len(left) * 2 < len(every):
        return WHOLE_REPLY
    edits = [(u.start, u.end, "") for u in gone]
    edits += [
        (c.figure.start, c.figure.end, format_like(c.figure, c.right))
        for c in keep
        if c.right is not None and not any(u.start <= c.figure.start and c.figure.end <= u.end for u in gone)
    ]
    out = text
    for start, end, new in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + new + out[end:]
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out or WHOLE_REPLY


def allowed_spans(text: str, failed: Iterable[Figure]) -> list[tuple[int, int]]:
    """The units of ``text`` a rewrite may change: those holding a failed figure."""
    all_units = units(text)
    spans: list[tuple[int, int]] = []
    for fig in failed:
        unit = unit_of(all_units, fig.start, fig.end)
        spans.append((unit.start, unit.end) if unit is not None else (fig.start, fig.end))
    return spans


def describe(corrections: Iterable[Correction], write: Callable[[Figure, float], str] = format_like) -> str:
    """The corrections for the correction call, one per line, built in code."""
    lines = []
    for n, c in enumerate(corrections, start=1):
        if c.right is None:
            lines.append(f'{n}. "{c.figure.text}": no known value. Leave it out.')
        else:
            lines.append(f'{n}. "{c.figure.text}" should be "{write(c.figure, c.right)}".')
    return "\n".join(lines)
