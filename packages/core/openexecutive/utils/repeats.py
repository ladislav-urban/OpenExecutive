"""Sentences a reply says twice.

A turn's extra round (the claim check's "do it now or say it isn't done",
orchestrator/claim_check.py) is asked for one short line, but the model
sometimes writes its whole answer again, slightly reworded, and a non-streamed
reply (the ERPNext panel's) then carries both. Pilot, Nov 2, 2026: "Whether
to release it is Rachel's call…" followed by "Whether to release it is
Rachel's decision…".

``new_line`` keeps, from such a round's text, only what the reply hasn't
already said, and at most a short line of it. A sentence that says something
isn't done, or failed, is never taken for a repeat of one that says it was:
"I couldn't note the hold" reads almost like "I've noted the hold", and it is
the line the extra round exists for. ``repeated_sentences`` finds a sentence a
reply says twice, for the evals' deterministic check.

For the evals' check only, two table rows or list items that both name
figures, and not the same ones, are not repeats however alike their words: a
forecast's rows ("| Jan 13 | 203,526.52 |" and "| Jan 20 | 203,526.52 |") and
a list of invoices ("- $332.50 lands Nov 25", "- $645.90 lands Dec 28") (eval
erpnext_094, Nov 18, 2026: 12 of 15 runs flagged such rows). Prose keeps the
words-only rule, so a restated sentence with its figures written another way
is still a repeat, and ``new_line`` (the claim check's extra round) is
unchanged: words only.

Cost is bounded: each sentence is compared on its first ``COMPARE_CHARS``
characters, against at most ``MAX_EARLIER`` earlier sentences, and difflib's
cheap upper bounds rule most pairs out before its quadratic ratio runs.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# Two sentences this alike (difflib's ratio over their normalized words)
# say the same thing: "is Rachel's call" against "is Rachel's decision".
SIMILAR = 0.75
# A sentence shorter than this ("Done.", "Yes.") may repeat harmlessly.
MIN_SENTENCE_CHARS = 25
# The most an extra round adds: the "one short line" it is asked for.
MAX_LINE_CHARS = 300
# Bounds on the comparison's cost (difflib's ratio is quadratic).
COMPARE_CHARS = 120
MAX_EARLIER = 200
MAX_SENTENCES = 200
# The most difflib ratios one call works out; past it, a sentence counts as
# new (kept), which costs a repeat at worst, never a lost line.
MAX_RATIOS = 2000

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+|\n+")
_WORD = re.compile(r"[a-z0-9$%.,]+")
# A figure: ASCII digits, with the separators a number is written with.
_FIGURE = re.compile(r"[0-9](?:[0-9,.]*[0-9])?")
# A table row or a list item: where parallel lines differ only in figures.
_ROW = re.compile(r"\s*(?:\||[-*\u2022]\s|[0-9]+[.)]\s)")
# Words that say something isn't so or didn't happen.
_NOT = re.compile(
    r"\b(?:not|no|never|cannot|can't|couldn't|didn't|isn't|wasn't|won't|"
    r"haven't|hasn't|unable|failed|refused|without)\b|n't\b"
)
# Words that say an action wasn't done: a sentence with one is the line the
# claim check's extra round exists for, so it is only ever a repeat when it
# is word for word an earlier sentence. "I've removed it so it won't show"
# against "I couldn't remove it, so it will still show" both say "not".
_FAILED = re.compile(
    r"\b(?:couldn't|can't|cannot|unable|failed|refused|didn't|haven't|"
    r"hasn't|isn't done|wasn't done|not done|not able)\b"
)


def sentences(text: str) -> list[str]:
    """``text`` cut into sentences and lines, each stripped, none empty."""
    return [s.strip() for s in _SENTENCE_END.split(text or "") if s and s.strip()]


def _norm(sentence: str) -> str:
    text = sentence.replace("’", "'").lower()[: COMPARE_CHARS * 2]
    return " ".join(_WORD.findall(text.replace("'", ""))).strip(" .,")[:COMPARE_CHARS]


def _figures(sentence: str) -> tuple[str, ...]:
    """The figures a table row or list item names, as a sorted multiset
    ("$5,382.50" and "$5382.50" alike), from as much of it as is compared and
    as much again; () for prose, or a row with no figure."""
    row = _ROW.match(sentence)
    if not row:
        return ()
    # The item's own marker ("1) ") is not one of its figures.
    body = sentence[row.end():][: COMPARE_CHARS * 2]
    return tuple(sorted(f.replace(",", "") for f in _FIGURE.findall(body)))


def _negative(sentence: str) -> bool:
    return _NOT.search(sentence.replace("’", "'").lower()) is not None


def _failed(sentence: str) -> bool:
    return _FAILED.search(sentence.replace("’", "'").lower()) is not None


class _Said:
    """Sentences said so far, normalized, with whether each says "not"."""

    def __init__(self, *, rows_by_figures: bool = False) -> None:
        self._items: list[tuple[str, bool, tuple[str, ...]]] = []
        self._ratios = 0
        # The evals' check: rows naming different figures differ.
        self._rows_by_figures = rows_by_figures

    def add(self, sentence: str) -> None:
        self._items.append((_norm(sentence), _negative(sentence), _figures(sentence)))
        del self._items[:-MAX_EARLIER]

    def _alike(self, a: str, b: str) -> bool:
        if a == b:
            return True
        matcher = SequenceMatcher(None, a, b, autojunk=False)
        if matcher.real_quick_ratio() < SIMILAR or matcher.quick_ratio() < SIMILAR:
            return False
        if self._ratios >= MAX_RATIOS:
            return False
        self._ratios += 1
        return matcher.ratio() >= SIMILAR

    def has(self, sentence: str) -> bool:
        norm = _norm(sentence)
        if len(norm) < MIN_SENTENCE_CHARS:
            return False
        # A sentence that says "not" ("could not", "won't", "unable") may be
        # the line that retracts a claim: a repeat only word for word. A
        # doubled negative sentence is the lesser harm.
        if _failed(sentence) or _negative(sentence):
            return any(norm == e for e, _, _ in self._items)
        figures = _figures(sentence) if self._rows_by_figures else ()
        return any(
            not neg
            and len(e) >= MIN_SENTENCE_CHARS
            and not (figures and figs and figs != figures)
            and self._alike(norm, e)
            for e, neg, figs in self._items
        )


def repeated_sentences(reply: str) -> list[str]:
    """Each sentence of ``reply`` that says again what an earlier one said."""
    said = _Said(rows_by_figures=True)
    found = []
    for s in sentences(reply)[:MAX_SENTENCES]:
        if said.has(s):
            found.append(s)
        said.add(s)
    return found


def new_line(earlier: str, text: str) -> str:
    """From an extra round's ``text``, the sentences ``earlier`` (the reply so
    far) doesn't already say, cut to one short line. A sentence that says
    something isn't done comes first and is always kept (cut if it alone is
    longer than ``MAX_LINE_CHARS``); the rest fill the line in order."""
    said = _Said()
    for s in sentences(earlier)[-MAX_EARLIER:]:
        said.add(s)
    fresh: list[str] = []
    for s in sentences(text)[:MAX_SENTENCES]:
        if not said.has(s):
            fresh.append(s)
            said.add(s)
    failures = [s for s in fresh if _negative(s)]
    others = [s for s in fresh if not _negative(s)]
    kept: list[str] = []
    size = 0
    for s in failures + others:
        if kept and size + len(s) + 1 > MAX_LINE_CHARS:
            if s in failures:
                continue
            break
        kept.append(s)
        size += len(s) + 1
    kept.sort(key=fresh.index)
    line = " ".join(kept)
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1].rstrip() + "…"
