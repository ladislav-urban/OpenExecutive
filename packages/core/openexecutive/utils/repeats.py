"""Sentences a reply says twice.

A turn's extra round (the claim check's "do it now or say it isn't done",
orchestrator/claim_check.py) is asked for one short line, but the model
sometimes writes its whole answer again, slightly reworded, and a non-streamed
reply (the ERPNext panel's) then carries both. Pilot, Nov 2, 2026: "Whether
to release it is Rachel's call…" followed by "Whether to release it is
Rachel's decision…".

``new_line`` keeps, from such a round's text, only what the reply hasn't
already said, and at most a short line of it. ``repeated_sentences`` finds a
sentence a reply says twice, for the evals' deterministic check.
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

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+|\n+")
_WORD = re.compile(r"[a-z0-9$%.,]+")


def sentences(text: str) -> list[str]:
    """``text`` cut into sentences and lines, each stripped, none empty."""
    return [s.strip() for s in _SENTENCE_END.split(text or "") if s and s.strip()]


def _norm(sentence: str) -> str:
    return " ".join(_WORD.findall(sentence.replace("’", "'").lower())).strip(" .,")


def _alike(a: str, b: str) -> bool:
    if a == b:
        return True
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= SIMILAR


def _said(sentence: str, earlier: list[str]) -> bool:
    norm = _norm(sentence)
    if len(norm) < MIN_SENTENCE_CHARS:
        return False
    return any(_alike(norm, e) for e in earlier if len(e) >= MIN_SENTENCE_CHARS)


def repeated_sentences(reply: str) -> list[str]:
    """Each sentence of ``reply`` that says again what an earlier one said."""
    seen: list[str] = []
    found = []
    for s in sentences(reply):
        if _said(s, seen):
            found.append(s)
        seen.append(_norm(s))
    return found


def new_line(earlier: str, text: str) -> str:
    """From an extra round's ``text``, the sentences ``earlier`` (the reply so
    far) doesn't already say, cut to one short line: whole sentences up to
    ``MAX_LINE_CHARS``, and at least the first if it is longer."""
    said = [_norm(s) for s in sentences(earlier)]
    kept: list[str] = []
    size = 0
    for s in sentences(text):
        if _said(s, said):
            continue
        if kept and size + len(s) + 1 > MAX_LINE_CHARS:
            break
        kept.append(s)
        size += len(s) + 1
        said.append(_norm(s))
    line = " ".join(kept)
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1].rstrip() + "…"
