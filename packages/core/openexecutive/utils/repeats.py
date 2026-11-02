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
COMPARE_CHARS = 300
MAX_EARLIER = 200
MAX_SENTENCES = 200

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+|\n+")
_WORD = re.compile(r"[a-z0-9$%.,]+")
# Words that say something isn't so or didn't happen.
_NOT = re.compile(
    r"\b(?:not|no|never|cannot|can't|couldn't|didn't|isn't|wasn't|won't|"
    r"haven't|hasn't|unable|failed|refused|without)\b|n't\b"
)


def sentences(text: str) -> list[str]:
    """``text`` cut into sentences and lines, each stripped, none empty."""
    return [s.strip() for s in _SENTENCE_END.split(text or "") if s and s.strip()]


def _norm(sentence: str) -> str:
    text = sentence.replace("’", "'").lower()[: COMPARE_CHARS * 2]
    return " ".join(_WORD.findall(text.replace("'", ""))).strip(" .,")[:COMPARE_CHARS]


def _negative(sentence: str) -> bool:
    return _NOT.search(sentence.replace("’", "'").lower()) is not None


def _alike(a: str, b: str) -> bool:
    if a == b:
        return True
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return (
        matcher.real_quick_ratio() >= SIMILAR
        and matcher.quick_ratio() >= SIMILAR
        and matcher.ratio() >= SIMILAR
    )


class _Said:
    """Sentences said so far, normalized, with whether each says "not"."""

    def __init__(self) -> None:
        self._items: list[tuple[str, bool]] = []

    def add(self, sentence: str) -> None:
        self._items.append((_norm(sentence), _negative(sentence)))
        del self._items[:-MAX_EARLIER]

    def has(self, sentence: str) -> bool:
        norm = _norm(sentence)
        if len(norm) < MIN_SENTENCE_CHARS:
            return False
        negative = _negative(sentence)
        return any(
            neg == negative and len(e) >= MIN_SENTENCE_CHARS and _alike(norm, e)
            for e, neg in self._items
        )


def repeated_sentences(reply: str) -> list[str]:
    """Each sentence of ``reply`` that says again what an earlier one said."""
    said = _Said()
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
