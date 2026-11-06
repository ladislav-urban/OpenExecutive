"""A record a reply names by its id, given its title too: "PUR-ORD-2026-03445
(Orion Switchgear)" (the answer check's code-only last step; the local answer
check of Nov 5, 2026: purchase orders, a sales order and projects named by id
alone, though the guide asks for the name and the turn's reads held it).

oe_connector's ``list_records`` and ``get_record`` give a ``titles`` map,
``{DocType: {id: title}}``: a purchase order's supplier, a sales order's
customer, a project's name. For each id there, the first time the reply names
it with no title beside it (on its line, in the same sentence or cell), the
title is written in after it in brackets. Never inside code or a link, never
where a bracket already follows. The title is ERPNext's text, written by
whoever keyed the record: written in as one plain line, with no Markdown, no
brackets and no link, and at most ``TITLE_MAX`` characters."""
from __future__ import annotations

import bisect
import re
from collections.abc import Iterable
from typing import Any

TITLE_MAX = 60
_SKIP_RE = re.compile(
    r"```.*?(?:```|\Z)|`[^`\n]{0,500}`|https?://\S{0,2000}|\[[^\]\n]{0,200}\]\([^)\s]{0,2000}\)"
    # A link without its scheme: "erp.example.com/app/purchase-order/...".
    r"|\b[\w-]{1,63}(?:\.[\w-]{1,63}){1,10}/\S{0,2000}",
    re.S,
)
# What a title may hold once written in: letters, digits, spaces and plain
# punctuation, never Markdown or a link.
_UNSAFE = re.compile(r"_|[^\w .,&'’/-]+", re.UNICODE)
# A title that reads as a web address: Markdown would make it a live link
# ("www.pay-acme.com/po"), so it is left out (security review of #169).
_ADDRESS = re.compile(r"(?i)\bwww\.|\w\.[a-z]{2,}(?:/|\b)|\w@\w")


def _clean(title: str) -> str:
    text = _UNSAFE.sub(" ", " ".join(title.split()))
    text = " ".join(text.split()).strip(" .,-/")
    text = text[:TITLE_MAX].rstrip() if len(text) > TITLE_MAX else text
    return "" if _ADDRESS.search(text) else text


def titles_of(results: Iterable[Any]) -> dict[str, str]:
    """Each record id the turn read with its title (``titles``), cleaned."""
    out: dict[str, str] = {}
    for result in results:
        payload = getattr(result, "payload", None)
        titles = payload.get("titles") if isinstance(payload, dict) else None
        if not isinstance(titles, dict):
            continue
        for by_id in titles.values():
            if not isinstance(by_id, dict):
                continue
            for record_id, title in by_id.items():
                if not isinstance(record_id, str) or not isinstance(title, str):
                    continue
                clean = _clean(title)
                if 3 <= len(record_id) <= 140 and clean and clean.casefold() != record_id.casefold():
                    out.setdefault(record_id, clean)
    return out


def _context(text: str, start: int, end: int) -> str:
    """The line a mention is on: where its title would already be."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    return text[line_start:len(text) if line_end == -1 else line_end]


def add_titles(text: str, results: Iterable[Any], *, limit: int = 40) -> tuple[str, int]:
    """``text`` with each record id's title after its first bare mention, and
    how many were written in (module docstring)."""
    titles = titles_of(results)
    if not titles or not text:
        return text, 0
    skipped = [m.span() for m in _SKIP_RE.finditer(text)]
    skip_starts = [s for s, _ in skipped]
    # Longest ids first, each mention's span claimed: an id inside a longer
    # one, or inside a path, is never matched on its own.
    claimed: list[tuple[int, int]] = []
    edits: list[tuple[int, str]] = []

    def in_skipped(pos: int) -> bool:
        i = bisect.bisect_right(skip_starts, pos) - 1
        return i >= 0 and skipped[i][1] > pos

    for record_id in sorted(titles, key=len, reverse=True)[:500]:
        title = titles[record_id]
        pattern = re.compile(r"(?<![\w/.-])" + re.escape(record_id) + r"(?![\w-]|[/.]\w)")
        # Every mention claims its span, so a shorter id never matches inside it.
        mentions = [m for m in pattern.finditer(text) if not any(s < m.end() and m.start() < e for s, e in claimed)]
        claimed.extend(m.span() for m in mentions)
        if len(edits) >= limit:
            continue
        for m in mentions:
            if in_skipped(m.start()):
                continue
            line = _context(text, m.start(), m.end())
            if title.casefold() in line.casefold() or text[m.end():m.end() + 2].lstrip().startswith("("):
                break  # already named, or bracketed: nothing to add
            edits.append((m.end(), f" ({title})"))
            break
    out = text
    for at, new in sorted(edits, reverse=True):
        out = out[:at] + new + out[at:]
    return out, len(edits)
