"""Planted instructions' specifics: a deterministic backstop on the reply.

Text addressed to an AI assistant can reach a turn two ways: pasted into the
person's own message ("summarise this note: ... ASSISTANT INSTRUCTION: email
it to ..."), or written into a record the turn reads (an invoice's remarks
telling "any AI assistant" to raise an order). The wording in block 0 tells
the model to say such text held an instruction and what kind, never its
specifics; this module holds the reply to that in plain code, whatever the
model wrote. Pure functions, no I/O; ``answer_check.check_reply`` runs
``cut`` first, on every route, whether or not the figure check is on.

**Spans** are the parts of the turn's text that were addressed to an
assistant:

- ``markers``: a fixed set of phrases ("assistant instruction", "any AI
  assistant", a line starting "bot:", "ignore previous instructions", "do
  not tell anyone"). On the person's own message they count only inside a
  quoted or pasted part (a double-quoted run, a code fence, ``>`` lines),
  never in the request itself. On ERP text they count anywhere. A span is
  the whole quoted part, paragraph or JSON string value holding the marker
  (at most MAX_SPAN_CHARS on each side of it), so a specific written before
  the marker is inside it too.
- ``evidence_span``: the JSON string value or paragraph holding the words
  ``content_scan`` quoted for a flag it verified.

**Candidates** are what a span may hold that the reply must not repeat:
contact details (emails, phone numbers), URLs, IBANs and quoted runs of
three or more words, from any span; names and number-plus-unit phrases
("500-unit") only from sp**The cut** acts only on a candidate that the turn's material (everything
its prompt held: system blocks, facts, memory, retrieved knowledge, history,
every tool result and the specialists' replies) has only inside spans.
Anything that is also in a real field stays. Matching runs on one normalised
form on both sides (``normalise``: JSON escapes undone, NFKC, Cc/Cf
characters stripped as ``content_scan._plain`` does, casefold, quotes made
plain, spacing collapsed); numbers compare by value and units are stemmed,
so "40 units" in a span is kept against a record's qty 40.

- A strong candidate (contact detail, URL, IBAN, quoted run) is written over
  with ``REMOVED`` in place, its sentence kept, however short the reply.
- A sentence, item or row holding a name or an amount (from verified
  evidence only) is dropped; when more than half the reply's units would go,
  the reply is kept and ``LINE`` appended instead (the cap: a span can't
  empty an answer through its weak candidates).
- When the rest doesn't already say what kind of request it was, ``LINE`` is
  appended; its ``{kind}`` comes only from ``content_scan.CATEGORIES``' keys
  (``KINDS``), never from the text.

 ``{kind}`` comes only
from ``content_scan.CATEGORIES``' keys (``KINDS``), never from the text.

Left to the wording and the evals: specifics split or reworded ("payroll
at outside-example dot com", "five hundred units"), and an injection with
no marker that the scan didn't flag.

Known limits:

- Evidence spans a turn found are carried to the conversation's later turns
  on ``Session.planted_spans``, in process memory only: a restart, or a
  conversation picked up by another worker, loses them, and a follow-up
  there can repeat an evidence-only specific that history still holds.
  (Marker spans are found again from history's user messages, so a note the
  person pasted stays covered.) Sessions are rebuilt from their saved
  messages, with nothing else of the session persisted, so there is nowhere
  cheap to keep them.
- An assistant reply already in history that holds a specific (one written
  before this check, or kept whole by the cap) counts as material outside
  any span, so a later turn keeps that specific. For conversations from
  before this change that is transitional; for the cap it is accepted.

Every input is capped (``MAX_*``) and every pattern is linear, with bounded
repeats and no nested quantifiers, so a crafted payload can't make a turn
spend its time here.
"""
from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# What a marker search reads of one text.
MAX_SCAN_CHARS = 400_000
# How far a span reaches on each side of what opened it, and the spans one
# turn keeps.
MAX_SPAN_CHARS = 8_000
MAX_SPANS = 40
# A reply longer than this is left as it is.
MAX_REPLY_CHARS = 60_000
# The turn's material kept for the comparison; past it the cut doesn't run
# (``answer_check``), since what it couldn't keep would look span-only.
MAX_MATERIAL_CHARS = 6_000_000
# Candidates of each kind a turn compares.
MAX_CANDIDATES = 60

# What the instruction asked for, by ``content_scan.CATEGORIES`` key, in the
# words ``LINE`` reads. Nothing else ever fills ``{kind}``.
KINDS: dict[str, str] = {
    "ignore_rules": "change my instructions",
    "forward_or_share": "forward or share something",
    "reveal_info": "reveal information",
    "other_address": "write to a different address",
    "open_link": "open a link",
    "payment_change": "change payment or bank details",
    "credentials": "hand over a password or login",
    "other": "do something",
}
LINE = "Part of what I read was addressed to an AI assistant and asked me to {kind}; I didn't act on it."


def line(kind: str) -> str:
    """``LINE`` for a category key; anything else reads as "do something"."""
    return LINE.format(kind=KINDS.get(kind, KINDS["other"]))


@dataclass(frozen=True)
class Span:
    """Text addressed to an assistant. ``evidence``: opened by
    ``content_scan``'s verified evidence (names and number-plus-unit phrases
    are cut only from these). ``kind``: its category key, "" for a marker."""

    text: str
    evidence: bool = False
    kind: str = ""


@dataclass(frozen=True)
class Outcome:
    """What ``cut`` did: the reply, and counts only."""

    text: str
    spans: int = 0
    cut: int = 0
    appended: bool = False
    capped: bool = False
    # Strong specifics written over with REMOVED in place.
    redacted: int = 0


# ---------------------------------------------------------------------------
# Normalisation


@functools.cache
def _invisible() -> re.Pattern[str]:
    """Every Cc/Cf character but the whitespace ones, as one class. Built
    once, from the planes that hold them (0, 1 and 14)."""
    chars = [
        chr(c) for c in (*range(0x0, 0x20000), *range(0xE0000, 0xE1000))
        if unicodedata.category(chr(c)) in ("Cc", "Cf") and not chr(c).isspace()
    ]
    return re.compile("[" + "".join(re.escape(ch) for ch in chars) + "]")


_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\([nrt\"\\/])")
_ESCAPED = {"n": "\n", "r": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "…": "..."})


def _unescape(text: str) -> str:
    def one(m: re.Match[str]) -> str:
        if m.group(1):
            return chr(int(m.group(1), 16))
        return _ESCAPED[m.group(2)]

    return _JSON_ESCAPE.sub(one, text)


def normalise(text: str) -> str:
    """``text`` as both sides of every comparison read it: JSON escapes
    undone, NFKC, Cc/Cf stripped, casefolded, quotes plain, spacing
    collapsed. Emails and URLs come out lower-cased with the rest."""
    text = unicodedata.normalize("NFKC", _unescape(text))
    text = _invisible().sub("", text).casefold().translate(_QUOTES)
    return " ".join(text.split())


_NUMBER = re.compile(r"\d[\d,]{0,30}(?:\.\d{1,12})?")


def _value(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


_PLURAL = re.compile(r"(?:ies|(?<=[sxz])es|(?<=ch)es|(?<=sh)es|(?<!s)s)$")


def _stem(word: str) -> str:
    """A unit word without its plural ending: "units" -> "unit",
    "boxes" -> "box", "batteries" -> "battery"."""
    word = word.casefold()
    if len(word) <= 3:
        return word
    return _PLURAL.sub(lambda m: "y" if m.group(0) == "ies" else "", word)


# ---------------------------------------------------------------------------
# Markers and spans

_MARKER = re.compile(
    r"\bassistant\s{1,4}instructions?\b"
    r"|\b(?:any|the|an)\s{1,4}ai\s{1,4}(?:assistants?|agents?|models?)\b"
    r"|(?:^|\\n|\")[ \t>]{0,8}(?:assistant|bot|ai)[ \t]{0,4}:"
    r"|\bignore\s{1,4}(?:(?:all|any|previous|prior|your)\s{1,4}){1,2}(?:instructions|rules)\b"
    r"|\bdo\s{1,4}not\s{1,4}(?:tell|mention)\s{1,4}(?:this|anyone)\b",
    re.IGNORECASE | re.MULTILINE,
)
# A quoted or pasted part of the person's message.
_DOUBLE_QUOTED = re.compile(r'"([^"]{0,8000})"')
_CURLY_QUOTED = re.compile("“([^”]{0,8000})”")
_FENCED = re.compile(r"```([\s\S]{0,20000}?)```")
_BLOCKQUOTE = re.compile(r"(?:^[ \t]{0,3}>[^\n]*(?:\n|\Z))+", re.MULTILINE)
# Where an ERP text's span ends: a blank line (as text or JSON-escaped) or
# the end of its JSON string value.
_BOUNDARY = re.compile(r'\n[ \t]*\n|\\n\\n|(?<!\\)"')


def _quoted_parts(text: str) -> list[tuple[int, int]]:
    parts: list[tuple[int, int]] = []
    for pattern in (_DOUBLE_QUOTED, _CURLY_QUOTED, _FENCED):
        parts.extend((m.start(1), m.end(1)) for m in pattern.finditer(text))
    parts.extend((m.start(), m.end()) for m in _BLOCKQUOTE.finditer(text))
    return parts


def _paragraph(text: str, start: int, end: int) -> tuple[int, int]:
    """The paragraph or JSON string value holding ``text[start:end]``: back to
    the previous boundary and on to the next, each at most MAX_SPAN_CHARS."""
    window = max(0, start - MAX_SPAN_CHARS)
    begin = window
    for before in _BOUNDARY.finditer(text, window, start):
        begin = before.end()
    limit = min(len(text), end + MAX_SPAN_CHARS)
    stop = _BOUNDARY.search(text, end, limit)
    return begin, (stop.start() if stop is not None else limit)


def markers(text: str, *, pasted_only: bool) -> list[Span]:
    """The spans markers open in ``text``, each the whole part holding the
    marker, so a specific written before it is inside too. ``pasted_only``
    (the person's own message): only inside a quoted or pasted part, and the
    span is that part. Otherwise (ERP text) anywhere, and the span is its
    paragraph or JSON string value. Either way at most MAX_SPAN_CHARS on each
    side of the marker."""
    if not isinstance(text, str) or not text:
        return []
    text = text[:MAX_SCAN_CHARS]
    spans: list[Span] = []
    if pasted_only:
        for start, end in _quoted_parts(text):
            # From the opening quote, so a pasted line "bot: ..." counts too.
            hit = _MARKER.search(text, max(0, start - 1), end)
            if hit is not None:
                begin = max(start, hit.start() - MAX_SPAN_CHARS)
                spans.append(Span(text[begin:min(end, hit.end() + MAX_SPAN_CHARS)]))
        return spans
    pos = 0
    while len(spans) < MAX_SPANS:
        hit = _MARKER.search(text, pos)
        if hit is None:
            break
        begin, end = _paragraph(text, hit.start(), hit.end())
        spans.append(Span(text[begin:end]))
        pos = max(end, hit.end())
    return spans


_WORDS = re.compile(r"\w+")
_GAP = r"(?:[\W_]|\\u[0-9a-fA-F]{4}){1,12}"


def evidence_span(text: str, evidence: str, kind: str = "") -> Span | None:
    """The span holding ``evidence`` (words ``content_scan`` verified in
    ``text``): its JSON string value or paragraph, or the evidence itself
    when it can't be placed in the raw text."""
    if not isinstance(text, str) or not isinstance(evidence, str):
        return None
    words = _WORDS.findall(evidence)[:12]
    if not words:
        return None
    kind = kind if kind in KINDS else "other"
    text = text[:MAX_MATERIAL_CHARS]
    found = re.compile(_GAP.join(re.escape(w) for w in words), re.IGNORECASE).search(text)
    if found is None:
        return Span(evidence[:MAX_SPAN_CHARS], evidence=True, kind=kind)
    start, end = _paragraph(text, found.start(), found.end())
    return Span(text[start:end], evidence=True, kind=kind)


# ---------------------------------------------------------------------------
# Candidates

_EMAIL = re.compile(r"[\w.%+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
_URL = re.compile(r"(?:\b[a-z][a-z0-9+.-]{1,15}://|\bwww\.)[^\s<>\"'\])]{1,500}", re.IGNORECASE)
_PHONE = re.compile(r"(?<![\w+])\+?\d(?:[ \t().-]{0,2}\d){6,20}(?!\w)")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
_QUOTED_RUN = re.compile('"([^"]{1,300})"|“([^”]{1,300})”')
_NAME = re.compile(
    r"(?<![\w'])[A-ZÀ-Þ][\w'’&-]{1,30}(?:[ \t]{1,3}[A-ZÀ-Þ][\w'’&-]{1,30}){0,2}(?![\w'])"
)
_NUM_UNIT = re.compile(
    r"(?<![\w.,])((?:\d{1,3}(?:,\d{3}){1,5}|\d{1,12})(?:\.\d{1,6})?)(?:[ \t]{0,2}-[ \t]{0,2}|[ \t]{1,3})([^\W\d_]{2,20})\b"
)
_TRAILING = ".,;:!?)]}>'\""

# Words a name run never is, or starts or ends with: sentence words, the
# marker words, days, months, and the DocTypes a record names.
_STOP_WORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "there", "here", "please", "kindly", "any", "all",
    "every", "each", "do", "does", "not", "no", "ignore", "assistant", "assistants", "instruction",
    "instructions", "ai", "bot", "model", "agent", "note", "notes", "important", "urgent", "attention",
    "open", "email", "mail", "send", "forward", "share", "tell", "say", "make", "prepare", "create", "raise",
    "pay", "book", "reply", "call", "write", "add", "today", "tomorrow", "yesterday", "now", "asap", "i",
    "we", "you", "he", "she", "they", "it", "our", "your", "my", "their", "his", "her", "its", "us", "them",
    "dear", "hi", "hello", "thanks", "thank", "regards", "best", "mr", "mrs", "ms", "dr", "and", "or", "but",
    "for", "from", "to", "of", "in", "on", "at", "by", "with", "erpnext", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})
_DOCTYPES = frozenset(
    s.casefold() for s in (
        "Purchase Order", "Purchase Invoice", "Purchase Receipt", "Sales Order", "Sales Invoice",
        "Delivery Note", "Quotation", "Supplier Quotation", "Material Request", "ToDo", "Dunning",
        "Payment Entry", "Journal Entry", "Salary Slip", "Customer", "Supplier", "Item", "Employee",
        "Lead", "Opportunity", "Project", "Task", "Contact", "Address", "Issue", "Payment Request",
    )
)
_NOT_UNITS = _STOP_WORDS | frozenset({"am", "pm", "st", "nd", "rd", "th", "is", "are", "was", "were"})


@dataclass
class _Candidates:
    strings: set[str] = field(default_factory=set)  # emails, URLs, quoted runs
    phones: set[str] = field(default_factory=set)  # digits only
    ibans: set[str] = field(default_factory=set)  # compact, casefolded
    names: set[str] = field(default_factory=set)
    amounts: set[tuple[float, str]] = field(default_factory=set)  # (value, stemmed unit)

    def __bool__(self) -> bool:
        return bool(self.strings or self.phones or self.ibans or self.names or self.amounts)


def _phones(text: str) -> set[str]:
    out: set[str] = set()
    for m in _PHONE.finditer(text):
        if _DATE.fullmatch(m.group(0).strip()):
            continue
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) >= 7:
            out.add(digits)
    return out


def _amounts(text: str) -> set[tuple[float, str]]:
    out: set[tuple[float, str]] = set()
    for m in _NUM_UNIT.finditer(text):
        value, unit = _value(m.group(1)), m.group(2).casefold()
        if value is not None and unit not in _NOT_UNITS:
            out.add((value, _stem(unit)))
    return out


def _name(run: str) -> str:
    words = run.split()
    while words and words[0].casefold().strip("'’") in _STOP_WORDS:
        words.pop(0)
    while words and words[-1].casefold().strip("'’") in _STOP_WORDS:
        words.pop()
    phrase = normalise(" ".join(words))
    return "" if len(phrase) < 3 or phrase in _DOCTYPES else phrase


def candidates(spans: Iterable[Span]) -> _Candidates:
    """What each span holds that the reply must not repeat (module
    docstring): the weak kinds from verified evidence alone."""
    found = _Candidates()
    for span in spans:
        raw = _unescape(span.text[: 2 * MAX_SPAN_CHARS + 100])
        for m in _EMAIL.finditer(raw):
            found.strings.add(normalise(m.group(0)).strip(_TRAILING))
        for m in _URL.finditer(raw):
            found.strings.add(normalise(m.group(0)).rstrip(_TRAILING))
        for m in _QUOTED_RUN.finditer(raw):
            inner = normalise(m.group(1) or m.group(2) or "").strip(_TRAILING + " ")
            if len(inner.split()) >= 3:
                found.strings.add(inner)
        found.phones |= _phones(raw)
        found.ibans |= {re.sub(r"\s", "", m.group(0)).casefold() for m in _IBAN.finditer(raw)}
        if span.evidence:
            found.names |= {n for n in (_name(m.group(0)) for m in _NAME.finditer(raw)) if n}
            found.amounts |= _amounts(raw)
    found.strings = set(sorted(s for s in found.strings if len(s) >= 4)[:MAX_CANDIDATES])
    found.phones = set(sorted(found.phones)[:MAX_CANDIDATES])
    found.ibans = set(sorted(found.ibans)[:MAX_CANDIDATES])
    found.names = set(sorted(found.names)[:MAX_CANDIDATES])
    found.amounts = set(sorted(found.amounts)[:MAX_CANDIDATES])
    return found


def _word_in(phrase: str, text: str) -> bool:
    start = text.find(phrase)
    while start != -1:
        end = start + len(phrase)
        if (start == 0 or not text[start - 1].isalnum()) and (end == len(text) or not text[end].isalnum()):
            return True
        start = text.find(phrase, start + 1)
    return False


def _holds(unit: str, found: _Candidates) -> bool:
    """Whether a reply unit (raw) holds any of ``found``."""
    norm = normalise(unit)
    if any(s in norm for s in found.strings):
        return True
    if found.ibans and any(i in norm.replace(" ", "") for i in found.ibans):
        return True
    if found.phones:
        here = _phones(norm)
        if any(p in h or h in p for p in found.phones for h in here):
            return True
    if any(_word_in(n, norm) for n in found.names):
        return True
    return bool(found.amounts and found.amounts & _amounts(norm))


def _outside(material: Sequence[str], spans: Sequence[Span]) -> str:
    """The turn's material, normalised, with every span's text taken out."""
    text = " | ".join(normalise(m) for m in material if isinstance(m, str) and m)
    for part in sorted({normalise(s.text) for s in spans}, key=len, reverse=True):
        if len(part) >= 4:
            text = text.replace(part, " | ")
    return text


def _span_only(found: _Candidates, outside: str) -> _Candidates:
    """The candidates the material holds only inside spans."""
    kept = _Candidates()
    kept.strings = {s for s in found.strings if s not in outside}
    if found.ibans:
        compact = outside.replace(" ", "")
        kept.ibans = {i for i in found.ibans if i not in compact}
    if found.phones:
        seen = _phones(outside)
        kept.phones = {p for p in found.phones if not any(p in s or s in p for s in seen)}
    kept.names = {n for n in found.names if not _word_in(n, outside)}
    if found.amounts:
        values = set()
        for m in _NUMBER.finditer(outside):
            for piece in (m.group(0), *m.group(0).split(",")):
                v = _value(piece)
                if v is not None:
                    values.add(v)
        kept.amounts = {a for a in found.amounts if a[0] not in values}
    return kept


# The rest of the reply already says the text held an instruction.
_SAYS_SO = re.compile(r"instruction|addressed to (?:an? )?(?:ai|assistant)|didn't act on it|did not act on it")


REMOVED = "[removed]"
_LEAD = re.compile(r"[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?")


def _strong(found: _Candidates) -> _Candidates:
    return _Candidates(strings=set(found.strings), phones=set(found.phones), ibans=set(found.ibans))


def _weak(found: _Candidates) -> _Candidates:
    return _Candidates(names=set(found.names), amounts=set(found.amounts))


def _patterns(found: _Candidates) -> list[re.Pattern[str]]:
    """Each strong candidate as the raw reply may write it: any spacing
    between its words, an IBAN in groups, a phone with its separators."""
    out = [
        re.compile(r"\s{1,8}".join(re.escape(w) for w in s.split()), re.IGNORECASE)
        for s in sorted(found.strings, key=len, reverse=True) if s.split()
    ]
    out += [re.compile(r"\s?".join(re.escape(c) for c in i), re.IGNORECASE) for i in found.ibans]
    out += [re.compile(r"\+?" + r"[\s().-]{0,3}".join(p)) for p in found.phones]
    return out


def _redact(reply: str, found: _Candidates) -> tuple[str, int]:
    """``reply`` with every strong candidate written over with REMOVED in
    place, its sentence kept. A unit that still holds one after that (written
    in another form: fullwidth, split by an invisible character) is REMOVED
    whole, keeping its list mark."""
    from openexecutive.orchestrator.answer_correct import units as reply_units

    holding = [u for u in reply_units(reply) if _holds(reply[u.start:u.end], found)]
    if not holding:
        return reply, 0
    patterns = _patterns(found)
    count = 0
    out: list[str] = []
    pos = 0
    for u in sorted(holding, key=lambda u: (u.start, -u.end)):
        if u.start < pos:
            continue  # inside a unit already redacted (an item's sub-item)
        segment = _invisible().sub("", reply[u.start:u.end])
        for pattern in patterns:
            segment, n = pattern.subn(REMOVED, segment)
            count += n
        if _holds(segment, found):
            lead = _LEAD.match(segment)
            head = lead.group(0) if lead is not None else ""
            tail = segment[len(segment.rstrip()):]
            segment = f"{head}{REMOVED}{tail}"
            count += 1
        out.append(reply[pos:u.start])
        out.append(segment)
        pos = u.end
    out.append(reply[pos:])
    return "".join(out), count


def cut(reply: str, spans: Sequence[Span], material: Sequence[str]) -> Outcome:
    """``reply`` without the specifics of a span that the turn's material
    holds only inside spans (module docstring): a strong one (contact detail,
    URL, IBAN, quoted run) written over with REMOVED in place, always; a unit
    holding a name or an amount from verified evidence dropped, unless more
    than half the reply would go (the cap). The same string when nothing
    changes."""
    spans = list(spans)[:MAX_SPANS]
    if not spans or not reply.strip() or len(reply) > MAX_REPLY_CHARS:
        return Outcome(reply, spans=len(spans))
    found = candidates(spans)
    if not found:
        return Outcome(reply, spans=len(spans))
    from openexecutive.orchestrator.answer_correct import units as reply_units

    # The reply first: most turns repeat nothing, and then the material is
    # never read.
    if not any(_holds(reply[u.start:u.end], found) for u in reply_units(reply)):
        return Outcome(reply, spans=len(spans))
    found = _span_only(found, _outside(material, spans))
    if not found:
        return Outcome(reply, spans=len(spans))
    kind = next((s.kind for s in spans if s.evidence and s.kind), "other")
    text, redacted = _redact(reply, _strong(found))
    weak = _weak(found)
    units = [u for u in reply_units(text) if text[u.start:u.end].strip()]
    hits = [u for u in units if weak and _holds(text[u.start:u.end], weak)]
    # The smallest unit that holds it: an item, not the list item above it.
    hits = [
        u for u in hits
        if not any(o is not u and u.start <= o.start and o.end <= u.end for o in hits)
    ]
    if not hits and not redacted:
        return Outcome(reply, spans=len(spans))
    if hits and 2 * len(hits) > len(units):
        # The cap: names or amounts in most of the reply; it stays, with the line.
        return Outcome(
            f"{text.rstrip()}\n\n{line(kind)}", spans=len(spans), appended=True, capped=True, redacted=redacted,
        )
    if hits:
        out: list[str] = []
        pos = 0
        for u in sorted(hits, key=lambda u: u.start):
            if u.start >= pos:
                out.append(text[pos:u.start])
                pos = u.end
        out.append(text[pos:])
        text = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    appended = _SAYS_SO.search(normalise(text)) is None
    if appended:
        text = f"{text.rstrip()}\n\n{line(kind)}"
    return Outcome(text, spans=len(spans), cut=len(hits), appended=appended, redacted=redacted)
