"""Company documents that are standing rules: in every chat turn's context.

Retrieval finds a company document only when it is close enough to the
question, and a short rules page ("who owns what", "who can approve a
purchase") is rarely close to the question it answers: on the pilot's retake
(Nov 5, 2026) "Who owns what on Home's cards?" was 0.66 from its own question,
past the 0.55 cut, so answers named owners from people's departments instead.

A company document (``company/docs/*.md`` or ``.txt``) whose YAML front matter
says ``always: true`` is a standing rule:

    ---
    always: true
    ---
    # Who owns what

Every chat and panel turn gets the standing rules in its user turn (never a
cached block), before the retrieved excerpts, up to ``MAX_CHARS`` in all; a
rule past it is left out, with a log line. Retrieval leaves those documents out
of its own results, so a rule is never quoted twice. The front matter is
stripped before a document is indexed.

Read from the files, cached by their modification times, so a document the
operator adds, edits or removes counts from the next turn.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# What the standing rules may add to one turn, in all.
MAX_CHARS = 2500
_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
_FRONT_MATTER = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_TRUE = frozenset({"true", "yes", "on", "1"})

_lock = threading.Lock()
_cache: tuple[tuple[tuple[str, float], ...], list[tuple[str, str]]] | None = None


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """``text``'s front matter as plain ``key: value`` pairs, and the rest.
    Only flat keys are read; anything else in it is ignored."""
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t", "#")):
            meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, text[match.end():]


def is_standing(meta: dict[str, str]) -> bool:
    return meta.get("always", "").lower() in _TRUE


def _docs_dir() -> Path:
    from openexecutive.config import get_settings

    return get_settings().company_profile_path.parent / "docs"


def standing_rules(docs_dir: Path | None = None) -> list[tuple[str, str]]:
    """The standing rules, ``(filename, text)`` in filename order, within
    ``MAX_CHARS``. Never raises: an unreadable folder or file is left out."""
    global _cache
    folder = docs_dir or _docs_dir()
    try:
        files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _SUFFIXES)
        stamp = tuple((p.name, p.stat().st_mtime) for p in files)
    except OSError:
        return []
    with _lock:
        if _cache is not None and _cache[0] == stamp:
            return list(_cache[1])
    rules: list[tuple[str, str]] = []
    used = 0
    for path in files:
        try:
            meta, body = split_front_matter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not is_standing(meta):
            continue
        body = body.strip()
        if used + len(body) > MAX_CHARS:
            logger.warning("standing_rules: %s left out, past the %d characters a turn gets", path.name, MAX_CHARS)
            continue
        rules.append((path.name, body))
        used += len(body)
    with _lock:
        _cache = (stamp, rules)
    return list(rules)
