"""Check 1 of the answer check: every figure in a reply against the turn's sources.

oe_connector's ``docs/answer-check-plan.md``, section 2. No model call: code
reads each figure the reply gives (an amount, a percentage, a scaled number, a
quantity of something) and looks for where it came from, among:

- **ERPNext's values**: a JSON number in an ``erpnext__*`` result of the turn,
  or a numeric string in a field or report column whose type is numeric (a
  child table's, read through a dotted field such as ``parameters.rating``),
  or in a rating field ("4"). A number written inside a text field (a remark, a description) never counts:
  customers and suppliers write those too.
- **A simple combination of them**: a column's sum, average or count, the same
  for the rows that share one value of another column ("the overdue ones"),
  the first or the largest few rows' sum, a row count; and the difference,
  ratio, percentage or percentage change of two headline values (totals,
  summaries, a document's fields), of two cells of one row, or of one column's
  cells in neighbouring rows. Pairs are never added up: over hundreds of
  values, some pair's sum is near almost any rounded figure.
- **Figures the reply already confirmed**: a total of the figures just before
  it, or a difference, ratio or change of two of them.
- **What the person gave**: their message and the page they are on. A figure
  there isn't the reply's to check.
- **Other text the turn read**: another tool's result, a script's output, the
  conversation so far. These are free text, so a figure found only there is
  ``from_text``: not checked for a person (question 8 of the plan), and never
  ``confirmed``.

A figure matches a value when it equals the value rounded to the precision the
reply shows: "$1.2M" matches 1,184,330 and "43%" matches 42.71 or 0.4271
(question 2a, "rounding is ok"). A whole number written with trailing zeros may
be rounded, keeping two significant digits: "1,200" matches 1,184. Signs are
ignored: "owes $500" quotes an outstanding of -500.

Each figure ends ``confirmed``, ``given``, ``from_text``, ``from_truncated`` (a
sum, count or average over a result ERPNext cut off) or ``unconfirmed``. The
last two are what check 2 and the correction look at
(``orchestrator.answer_check``).

Numbers are read in the site's own format (ERPNext's ``number_format``, from
``answer_check_context``), then in the plain one, so "1.234,50" reads as 1234.5
on a site that writes it so. Number words are English only for now (question
5): thousand, million, billion and k, M, bn.
"""
from __future__ import annotations

import bisect
import dataclasses
import functools
import json
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["confirmed", "given", "from_text", "from_truncated", "unconfirmed", "wrong_currency"]
FLAGGED: frozenset[str] = frozenset({"from_truncated", "unconfirmed", "wrong_currency"})

# Number words a count may be written in ("six customers"), English only.
_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_WORD_NUMBER_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b(?=[ ]+[A-Za-z])", re.I)
# "N times the/as/more…": a multiple of one figure by another, not a frequency
# ("chased 3 times this month").
_MULTIPLE_NEXT = frozenset({"the", "as", "more", "larger", "bigger", "higher", "greater", "what", "that", "our"})
# A currency symbol's codes when answer_check_context names none.
_DEFAULT_SYMBOLS: dict[str, frozenset[str]] = {
    "$": frozenset({"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN"}),
    "€": frozenset({"EUR"}), "£": frozenset({"GBP"}), "₹": frozenset({"INR"}),
    "¥": frozenset({"JPY", "CNY"}), "₩": frozenset({"KRW"}), "₽": frozenset({"RUB"}),
}

# A reply's figures past this many are left as they are: a reply that long is
# a table dump, and the check must stay quick.
MAX_FIGURES = 80
# Values kept from one turn's results; past it, the rest are left out.
MAX_VALUES = 20_000
# Headline values two of which may be combined (any pair).
MAX_HEADLINE = 300
# Combinations of two values kept per kind (difference, ratio, change).
MAX_DERIVED = 200_000
# A column's rows are grouped by another column only when it has at most this
# many different values (a status, a customer group).
MAX_GROUPS = 30
# The first and the largest rows summed together, up to this many.
MAX_RUN = 50
# Text read for numbers (the person's message, other tools' results, the
# conversation): each piece's first MAX_TEXT_PIECE characters, and the turn's
# pieces up to MAX_TEXT_CHARS in all, newest first, so a long conversation or
# a pasted export can't stall the check.
MAX_TEXT_PIECE = 60_000
MAX_TEXT_CHARS = 300_000

# ``answer_check_context``'s ``number_format`` when the site says nothing.
PLAIN_FORMAT = "#,###.##"

_CURRENCY_SYMBOLS = "$€£¥₹₩₽₺₪₫฿₦₱₴₲₵₡₭₮₸"
_SPACES = "   "
_SCALES: dict[str, float] = {
    "k": 1e3, "K": 1e3, "thousand": 1e3,
    "m": 1e6, "M": 1e6, "mn": 1e6, "million": 1e6, "millions": 1e6,
    "bn": 1e9, "B": 1e9, "billion": 1e9, "billions": 1e9,
}
_MONTHS = frozenset({
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august", "september", "october",
    "november", "december",
})
# Words after a plain whole number that make it a period or part of the
# reply's own wording, not a figure from the data: "30 days", "3 options".
_NOT_COUNTED = frozenset({
    "second", "seconds", "minute", "minutes", "min", "mins", "hour", "hours", "hr", "hrs", "day", "days",
    "week", "weeks", "month", "months", "quarter", "quarters", "year", "years", "yr", "yrs", "am", "pm",
    "times", "option", "options", "way", "ways", "thing", "things", "step", "steps", "point", "points",
    "question", "questions", "reason", "reasons", "idea", "ideas", "suggestion", "suggestions", "tip",
    "tips", "area", "areas", "takeaway", "takeaways", "priority", "priorities", "key", "main", "of",
    "and", "or", "to", "in", "on", "at", "by", "for", "from", "the", "a", "an", "is", "are", "was",
    "were", "be", "more", "less", "than", "with", "as", "per", "that", "which", "who", "what", "it",
    "x", "st", "nd", "rd", "th", "s",
})
# Records a count may be of ("six customers", "three Cedar & Pine
# quotations"): such a count is held to the counts the turn read, never to any
# value that happens to equal it (a quantity of 6, a price of 3).
_RECORD_NOUNS = frozenset({
    "customer", "supplier", "vendor", "client", "order", "invoice", "bill", "quotation", "quote",
    "project", "task", "batch", "issue", "claim", "delivery", "receipt", "payment", "entry",
    "journal", "opportunity", "lead", "visit", "asset", "account", "employee", "request",
    "note", "record", "document", "job", "card", "contract", "item", "product", "sku", "line",
    "warehouse", "branch", "department", "site", "bank", "repair", "inspection", "reminder",
    "dunning", "timesheet", "milestone", "shipment", "return", "credit", "debit",
})
# What a count of each record is labelled with in ``Sources.counts``: its own
# word, and the DocTypes or fields that hold it.
_COUNT_WORDS: dict[str, tuple[str, ...]] = {
    "customer": ("customer", "party"), "supplier": ("supplier", "party"), "vendor": ("supplier", "party"),
    "client": ("customer", "party"), "bill": ("purchase invoice", "bill"), "quote": ("quotation",),
    "batch": ("work order", "batch", "job card"), "job": ("job card", "work order"),
    "delivery": ("delivery",), "shipment": ("delivery", "shipment"), "entry": ("entry", "journal"),
    "card": ("card",), "line": ("items", "line"), "product": ("item",), "sku": ("item",),
    "credit": ("credit", "return"), "debit": ("debit", "return"),
}
# Words before a plain whole number that make it an ordinal or a label.
_LABEL_BEFORE = frozenset({
    "top", "first", "last", "next", "past", "step", "phase", "option", "number", "no", "page", "line",
    "version", "chapter", "section", "rank", "ranked", "level", "tier", "round", "week", "day",
    "quarter", "q", "fy", "h", "item", "row", "table", "figure", "question", "point", "part",
})
# Three capitals before or after a number are a currency only when they are
# a currency's ISO 4217 code. Anything else is a word, a unit or an item's
# code: "162 LED panels" was read as 162 in a currency "LED", not the
# report's USD, and written back as "162 $ panels" (pilot, Nov 6, 2026).
_ISO_4217 = frozenset("""
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN
    BZD CAD CDF CHF CLP CNY COP CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL
    GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW
    KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN
    NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK
    SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU
    UZS VED VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR ZMW ZWG ZWL
""".split())  # noqa: SIM905 - the codes read as ISO lists them
# Codes that are also words written in capitals ("ALL", "TOP", "CUP"):
# never a currency in a reply.
_NOT_CURRENCY = frozenset({
    "THE", "AND", "FOR", "NOT", "BUT", "ALL", "ANY", "ERP", "API", "PDF", "CSV", "YOY",
    "TOP", "CUP", "PEN", "MAD", "TRY", "BOB", "GEL", "BAM", "MOP", "NIO", "SOS", "PAB",
})
_CURRENCY_CODES = _ISO_4217 - _NOT_CURRENCY

_SCALE_RE = r"(?:thousand|millions?|billions?|bn|mn|[kKMmB])(?![A-Za-z])"
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_DATE_RES = (
    re.compile(r"(?<![\d.,])\d{4}-\d{1,2}-\d{1,2}(?![\d])"),
    re.compile(r"(?<![\d.,])\d{1,2}[/.]\d{1,2}[/.]\d{2,4}(?![\d])"),
    re.compile(r"(?<![\d.,])\d{4}/\d{1,2}/\d{1,2}(?![\d])"),
    re.compile(r"(?<![\d.,])\d{1,2}:\d{2}(?::\d{2})?(?![\d])"),
)
_LIST_NUMBER_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)?\d+(?:\.\d+)*[.)][ \t]", re.M)
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_URL_RE = re.compile(r"\]\([^)]*\)|https?://\S+")


@dataclass(frozen=True)
class NumberFormat:
    """How a site writes numbers: its group (thousands) and decimal marks."""

    group: str
    decimal: str
    indian: bool = False

    @classmethod
    def parse(cls, number_format: object) -> NumberFormat:
        """ERPNext's ``number_format`` ("#,###.##", "#.###,##", "# ###.##",
        "#,##,###.##", "#'###.##"); the plain format for anything else."""
        fmt = number_format.strip() if isinstance(number_format, str) and number_format.strip() else PLAIN_FORMAT
        marks = [c for c in fmt if c != "#"]
        if not marks:
            return cls(",", ".")
        group = marks[0].replace(" ", " ")
        # "#,###" or "#.###": a group mark and no decimals.
        decimal = ("," if group == "." else ".") if len(marks) == 1 else marks[-1]
        return cls(group, decimal, fmt.startswith("#,##,###"))


PLAIN = NumberFormat(",", ".")


@functools.lru_cache(maxsize=8)
def _figure_re(space_groups: bool) -> re.Pattern[str]:
    # A space groups digits only on a site that writes "1 234,50", and only
    # before three digits, so "between 3 and 4 500" stays two numbers there.
    space = r"|[ ](?=\d{3}(?!\d))" if space_groups else ""
    num = r"\d+(?:(?:[,.'’" + _SPACES + r"]" + space + r")\d+)*"
    return re.compile(
        r"(?P<sign>[-−–(]\s?)?"
        r"(?P<pre>(?:[A-Z]{3}[ " + _SPACES + r"]?)|[" + _CURRENCY_SYMBOLS + r"][ " + _SPACES + r"]?)?"
        r"(?P<num>" + num + r")"
        r"(?P<scale>[ " + _SPACES + r"]?" + _SCALE_RE + r")?"
        r"(?P<pct>[ " + _SPACES + r"]?(?:%|percent\b|per[ ]?cent\b))?"
        r"(?P<post>[ " + _SPACES + r"]?[" + _CURRENCY_SYMBOLS + r"]|[ " + _SPACES + r"][A-Z]{3}\b)?"
    )


@dataclass
class Figure:
    """One figure in a reply: where it is, and what it may stand for."""

    start: int
    end: int
    text: str
    # The number as written, and the reply's text around it within ``text``.
    num: str
    pre: str
    post: str
    # Readings of the number (in the site's format, then the plain one), each
    # the value before its scale, with the decimals it shows.
    readings: tuple[tuple[float, int], ...]
    scale: float
    scale_text: str
    percent: bool
    # The format of the first reading.
    fmt: NumberFormat
    status: Status = "unconfirmed"
    # "N times …": matched against ratios of two values.
    multiple: bool = False
    # The record a count counts ("six customers": "customer"), held to counts
    # of that record only; "" for any other figure.
    count: str = ""
    # What the reply's own arithmetic before it gives ("5 × $71.00 = …"),
    # when it gives another value: the value it should be (own_arithmetic).
    arithmetic: float | None = None
    # The currency mark as written ("$", "€", "USD"), and for wrong_currency
    # the mark the value's own currency takes.
    currency_text: str = ""
    currency_fix: str | None = None
    # The value it was matched to, when one was (a source value or a combination).
    matched: float | None = None
    how: str = ""
    # The word right after it ("days" in "62.5 days"), lowercased, or "".
    unit: str = ""

    @property
    def not_money(self) -> bool:
        """Whether it plainly isn't an amount of money: a count of records,
        or a number of days, hours, stars or units, with no currency mark."""
        if self.currency_text or self.percent:
            return False
        return bool(self.count) or self.unit in _NOT_MONEY_UNITS

    @property
    def flagged(self) -> bool:
        return self.status in FLAGGED

    def intervals(self) -> list[tuple[float, float]]:
        """The absolute values this figure stands for, as closed intervals:
        each reading's rounding interval, and for a percentage the same as a
        fraction."""
        out: list[tuple[float, float]] = []
        for mantissa, decimals in self.readings:
            half = 0.5 * 10.0 ** (-decimals)
            if decimals == 0 and mantissa >= 1:
                digits = str(int(mantissa))
                zeros = len(digits) - len(digits.rstrip("0"))
                # Rounded, keeping two significant digits: "1,200" for 1,184,
                # never "1,000" for 1,400.
                places = min(zeros, max(len(digits) - 2, 0))
                half = max(half, 0.5 * 10.0 ** places)
            lo = max(mantissa - half, 0.0) * self.scale
            hi = (mantissa + half) * self.scale
            # Float noise: a value exactly on the edge still matches.
            pad = max(abs(hi), 1.0) * 1e-9
            out.append((lo - pad, hi + pad))
            if self.percent:
                out.append(((lo - pad) / 100.0, (hi + pad) / 100.0))
        return out

    def matches(self, value: float) -> bool:
        x = abs(value)
        return any(lo <= x <= hi for lo, hi in self.intervals())


def _reading(raw: str, fmt: NumberFormat) -> tuple[float, int] | None:
    """``raw`` read in ``fmt``: (value, decimals shown), or None when it isn't
    written that way."""
    text = raw.replace("’", "'")
    for space in _SPACES:
        text = text.replace(space, " ")
    int_part, frac = text, ""
    if fmt.decimal in text:
        head, _, tail = text.rpartition(fmt.decimal)
        if not tail.isdigit():
            return None
        int_part, frac = head, tail
    if not int_part:
        return None
    if fmt.group in int_part:
        groups = int_part.split(fmt.group)
        if not all(g.isdigit() for g in groups) or len(groups[0]) > 3:
            return None
        rest = groups[1:]
        if fmt.indian:
            if len(rest[-1]) != 3 or any(len(g) != 2 for g in rest[:-1]):
                return None
        elif any(len(g) != 3 for g in rest):
            return None
        int_part = "".join(groups)
    if not int_part.isdigit():
        return None
    return float(f"{int_part}.{frac}" if frac else int_part), len(frac)


def _readings(raw: str, fmt: NumberFormat) -> tuple[tuple[tuple[float, int], ...], NumberFormat]:
    out: list[tuple[float, int]] = []
    used = fmt
    for candidate in (fmt, PLAIN) if fmt != PLAIN else (fmt,):
        got = _reading(raw, candidate)
        if got is not None and got not in out:
            if not out:
                used = candidate
            out.append(got)
    return tuple(out), used


def _masked(text: str) -> list[tuple[int, int]]:
    """Spans no figure is read from: code, link targets and URLs, dates,
    times, and list numbering."""
    spans = [m.span() for m in _CODE_RE.finditer(text)]
    spans += [m.span() for m in _URL_RE.finditer(text)]
    for date_re in _DATE_RES:
        spans += [m.span() for m in date_re.finditer(text)]
    spans += [m.span() for m in _LIST_NUMBER_RE.finditer(text)]
    # Merged and sorted, so a number's overlap is one bisect (_is_masked).
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _is_masked(masked: list[tuple[int, int]], starts: list[int], start: int, end: int) -> bool:
    i = bisect.bisect_right(starts, end - 1) - 1
    return i >= 0 and masked[i][1] > start


def _word_before(text: str, pos: int) -> str:
    m = re.search(r"([A-Za-z]+)[\s#.:]*$", text[max(0, pos - 40):pos])
    return m.group(1).lower() if m else ""


def _word_after(text: str, pos: int) -> str:
    """The word right after ``pos`` (past spaces and emphasis), or ""."""
    m = re.match(r"[ \t*_]*([A-Za-z][A-Za-z'’-]*)", text[pos:pos + 40])
    return m.group(1).lower() if m else ""


# ERPNext's Rating fields are stored as a fraction of this many stars.
RATING_STARS = 5


def _is_rating(key: str) -> bool:
    name = key.lower()
    return name == "rating" or name.endswith("_rating") or name.startswith("rating_")


def _counts_records(text: str, end: int) -> str:
    """The record the number ending at ``end`` counts, or "": the last record
    noun within the next four words, before any punctuation or number ("nine
    open orders", "three Cedar & Pine quotations", "six customer projects":
    projects)."""
    m = re.match(r"[ \t*_]*((?:[A-Za-z][A-Za-z'’&-]*[ \t]+){0,4}[A-Za-z][A-Za-z'’-]*)", text[end:end + 80])
    if not m:
        return ""
    noun = ""
    for word in m.group(1).lower().split():
        word = word.strip("'’-&")
        if word in (
            "of", "and", "or", "to", "in", "on", "at", "by", "for", "from", "with", "are", "is", "were", "was",
            "per", "each", "every", "across", "a", "an",
        ):
            break
        stem = word[:-3] + "y" if word.endswith("ies") else word[:-2] if word.endswith("ches") else word.rstrip("s")
        if stem in _RECORD_NOUNS:
            noun = stem
    return noun


def count_words(noun: str) -> tuple[str, ...]:
    """What a count of ``noun`` may be labelled with (``_COUNT_WORDS``)."""
    return _COUNT_WORDS.get(noun, (noun,))


def _in_table_cell(text: str, start: int, end: int) -> bool:
    """Whether the figure is a whole cell of a Markdown table row."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    if not text[line_start:line_end].lstrip().startswith("|"):
        return False
    before = text[line_start:start].rsplit("|", 1)[-1]
    after = text[end:line_end].split("|", 1)[0]
    return not before.strip(" *_~") and not after.strip(" *_~")


_TIME_UNITS = frozenset({
    "second", "seconds", "minute", "minutes", "min", "mins", "hour", "hours", "hr", "hrs",
    "day", "days", "week", "weeks", "month", "months", "year", "years", "yr", "yrs",
})
# Words after a figure that make it something other than money ("62.5 days",
# "4.5 stars", "120 units"): never corrected to a money column's value.
_NOT_MONEY_UNITS = _TIME_UNITS | frozenset({
    "star", "stars", "out", "unit", "units", "pcs", "pieces", "piece", "kg", "km", "times", "points",
})


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return inner.split("|")


def _column_counts_time(text: str, start: int) -> bool:
    """Whether the table cell at ``start`` sits under a header naming a unit
    of time ("Days late", "Hours", "Weeks overdue")."""
    line_start = text.rfind("\n", 0, start) + 1
    column = text[line_start:start].count("|") - (1 if text[line_start:start].lstrip().startswith("|") else 0)
    # The table's first line is its header: walk up while lines are rows.
    header_start = line_start
    while header_start > 0:
        prev_start = text.rfind("\n", 0, header_start - 1) + 1
        if not text[prev_start:header_start - 1].lstrip().startswith("|"):
            break
        header_start = prev_start
    header_end = text.find("\n", header_start)
    header = text[header_start:len(text) if header_end == -1 else header_end]
    if header_start == line_start or set(header.replace("|", "").strip()) <= set("-: "):
        return False
    cells = _cells(header)
    if not 0 <= column < len(cells):
        return False
    words = {w.lower() for w in _WORD_RE.findall(cells[column].strip(" *_~"))}
    return bool(words & _TIME_UNITS)


def find_figures(text: str, fmt: NumberFormat = PLAIN, *, everything: bool = False) -> list[Figure]:
    """The figures ``text`` gives, in order. ``everything`` reads every number
    that isn't a date, an id or list numbering (a source's text, where a plain
    "14" may be what the reply repeats)."""
    masked = _masked(text)
    starts = [s for s, _ in masked]
    figures: list[Figure] = []
    for m in _figure_re(fmt.group == " ").finditer(text):
        num = m.group("num")
        num_start, num_end = m.span("num")
        if _is_masked(masked, starts, num_start, num_end):
            continue
        before = text[num_start - 1] if num_start > 0 else ""
        after = text[num_end] if num_end < len(text) else ""
        pre, post = m.group("pre") or "", m.group("post") or ""
        scale_text, pct = m.group("scale") or "", bool(m.group("pct"))
        if pre.strip().isalpha() and pre.strip() not in _CURRENCY_CODES:
            pre = ""
        if post.strip().isalpha() and post.strip() not in _CURRENCY_CODES:
            post = ""
        # Joined to letters or another number: an id, a code or a range
        # (SINV-00042, Q3, FY2026, 0-30), never a figure.
        if not pre and before and (before.isalnum() or before in "_/#@"):
            continue
        if not pre and num_start >= 2 and before in "-–" and text[num_start - 2].isalnum():
            continue
        if after == "_" or (after.isalpha() and not scale_text and not pct):
            continue
        if after in ("-", "–", "/") and num_end + 1 < len(text) and text[num_end + 1].isalnum():
            continue
        scale = _SCALES.get(scale_text.strip(), 1.0)
        if scale_text.strip() in ("m", "B") and not (pre or post):
            # "12m" is twelve metres or minutes, "5B" a grade, unless money.
            continue
        readings, used = _readings(num, fmt)
        if not readings:
            continue
        marked = bool(pre or post or scale_text or pct)
        separated = any(c in num for c in ",.'’ " + _SPACES)
        value = readings[0][0]
        if not marked and not separated:
            if len(num) > 1 and num.startswith("0"):
                # "03469": a short id (an invoice's number), never a figure or
                # a count ("so 03469 bills goods we already have" became
                # "so several bills goods…" on the pilot, Nov 6, 2026).
                continue
            if 1900 <= value <= 2100 and len(num) == 4:
                continue  # a year
            if re.search(r"(?:\d[ \t]+|\+)$", text[max(0, num_start - 4):num_start]) or re.match(
                r"[ \t]+\d", text[num_end:num_end + 3]
            ):
                continue  # one group of a phone number or a code ("+1 555 123 4567")
            if not everything:
                if _word_before(text, m.start()) in _MONTHS or _word_after(text, num_end) in _MONTHS:
                    continue  # "Nov 4", "4 November"
                multiple = _is_multiple(text, num_end)
                in_cell = _in_table_cell(text, num_start, num_end)
                if in_cell and _column_counts_time(text, num_start):
                    # A "Days late" cell is "184 days", as in a sentence: a
                    # count of time worked out from dates, never an ERPNext
                    # figure (Cedar & Pine's was blanked, pilot, Nov 6, 2026).
                    continue
                if not (in_cell or value >= 10_000 or multiple):
                    # A plain whole number is a figure only as a count of
                    # something: "14 orders", never "30 days" or "top 5".
                    following = _word_after(text, num_end)
                    if (
                        not following
                        or following in _NOT_COUNTED
                        or _word_before(text, m.start()) in _LABEL_BEFORE
                        or text[num_end:num_end + 1] != " "
                    ):
                        continue
        start = num_start - len(pre)
        end = num_end
        for group in ("scale", "pct", "post"):
            if m.group(group) and (group != "post" or post):
                end = m.end(group)
        sign = m.group("sign") or ""
        if sign.startswith("(") and text[end:end + 1] == ")":
            start, end = m.start("sign"), end + 1
        figures.append(
            Figure(
                start=start, end=end, text=text[start:end], num=num,
                pre=text[start:num_start], post=text[num_end:end],
                readings=readings, scale=scale, scale_text=scale_text.strip() if scale_text else "",
                percent=pct, fmt=used,
                multiple=not marked and not everything and _is_multiple(text, num_end),
                count=(
                    _counts_records(text, num_end)
                    if not marked and not separated and not everything and value < 10_000
                    and not _in_table_cell(text, num_start, num_end)
                    else ""
                ),
                currency_text=(pre.strip() or post.strip()) if (pre.strip() or post.strip()) else "",
                unit=_word_after(text, end),
            )
        )
    if not everything:
        figures.extend(_word_figures(text, masked, figures))
        figures.sort(key=lambda fig: fig.start)
    return figures


def _is_multiple(text: str, end: int) -> bool:
    """Whether the number ending at ``end`` reads "N times the/as/more…"."""
    m = re.match(r"[ ]*(?:times|×)[ ]+([A-Za-z]+)", text[end:end + 40])
    if m and m.group(1).lower() in _MULTIPLE_NEXT:
        return True
    # "12×" alone is a multiple; "5 × $71.00" is an operand of the reply's
    # own arithmetic (own_arithmetic), a quantity, not "N times as large".
    if text[end:end + 1] == "×":
        return re.match(r"×[ ]*[\d(" + _CURRENCY_SYMBOLS + r"]", text[end:end + 4]) is None
    return False


def _word_figures(text: str, masked: list[tuple[int, int]], found: list[Figure]) -> list[Figure]:
    """Counts written as words ("six customers"), checked like "6 customers"."""
    starts = [s for s, _ in masked]
    out: list[Figure] = []
    for m in _WORD_NUMBER_RE.finditer(text):
        start, end = m.span(1)
        if _is_masked(masked, starts, start, end) or any(f.start < end and start < f.end for f in found):
            continue
        following = _word_after(text, end)
        multiple = _is_multiple(text, end)
        if not multiple and (not following or following in _NOT_COUNTED or _word_before(text, start) in _LABEL_BEFORE):
            continue
        out.append(
            Figure(
                start=start, end=end, text=m.group(1), num=m.group(1), pre="", post="",
                readings=((float(_NUMBER_WORDS[m.group(1).lower()]), 0),), scale=1.0, scale_text="",
                percent=False, fmt=PLAIN, multiple=multiple,
                count="" if multiple else _counts_records(text, end), unit=following,
            )
        )
    return out


def numbers_in(text: str, fmt: NumberFormat = PLAIN) -> list[float]:
    """Every number ``text`` writes (the person's message, another tool's
    text), as absolute values with their scale."""
    return [
        mantissa * fig.scale
        for fig in find_figures(text, fmt, everything=True)
        for mantissa, _ in fig.readings
    ]


# ---------------------------------------------------------------------------
# The turn's ERPNext values


_SKIP_KEYS = frozenset({
    "filters", "limit", "docstatus", "idx", "indent", "is_group", "lft", "rgt", "period", "notes",
    "columns", "views", "history",
})
_NUMERIC_FIELDTYPES = frozenset({"Currency", "Float", "Int", "Percent"})


@dataclass(frozen=True)
class ErpnextResult:
    """One ERPNext call of the turn: its tool, arguments and parsed result."""

    tool: str
    arguments: dict[str, Any]
    payload: Any


def parse_result(text: object) -> Any:
    """A tool's result text as JSON, or None (a refusal, an error, not JSON)."""
    if not isinstance(text, str) or not text.lstrip().startswith(("{", "[")):
        return None
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if isinstance(payload, dict) and "error" in payload and len(payload) <= 3:
        return None
    return payload


@dataclass
class ResultTypes:
    """Which fields hold numbers, from ``answer_check_context`` (per DocType,
    with its child tables)."""

    doctypes: dict[str, Any] = field(default_factory=dict)

    def numeric(self, doctype: object, table: str | None = None) -> frozenset[str]:
        described = self.doctypes.get(doctype) if isinstance(doctype, str) else None
        if table and isinstance(described, dict):
            tables = described.get("tables")
            described = tables.get(table) if isinstance(tables, dict) else None
        fields = described.get("numeric") if isinstance(described, dict) else None
        return frozenset(f for f in fields if isinstance(f, str)) if isinstance(fields, list) else frozenset()


@dataclass
class _Rows:
    """One list of rows in a result: a list_records or report result's
    ``rows``, or a document's child table."""

    label: str
    truncated: bool
    count: int
    numeric: dict[str, list[float | None]] = field(default_factory=dict)
    text: dict[str, list[str | None]] = field(default_factory=dict)


def _number(value: Any, numeric_field: bool) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        got = float(value)
    elif numeric_field and isinstance(value, str):
        try:
            got = float(value.replace(",", "").strip())
        except ValueError:
            return None
    else:
        return None
    return got if math.isfinite(got) else None


# A field whose name holds one of these words, and none of the others, holds
# money; so does a report's Currency column.
_MONEY_WORDS = frozenset({
    "amount", "amt", "price", "cost", "costs", "outstanding", "balance", "paid", "debit", "credit", "revenue",
    "income", "expense", "expenses", "profit", "salary", "valuation", "fee", "fees", "total", "budget",
    "actual", "variance",
})
_NOT_MONEY_WORDS = frozenset({
    "qty", "quantity", "count", "days", "day", "hours", "hour", "weight", "percent", "percentage", "pct",
    "rating", "score", "age", "weeks", "months", "years", "rate", "ratio", "units", "conversion",
})


def _is_money_key(key: str, currency_fields: frozenset[str]) -> bool:
    if key in currency_fields:
        return True
    words = set(re.split(r"[^a-z]+", key.lower()))
    return bool(words & _MONEY_WORDS) and not words & _NOT_MONEY_WORDS


def _aggregate_names(arguments: dict[str, Any]) -> frozenset[str]:
    """list_records' own names for its aggregates: ``as``, else
    ``<function>_<field>`` or ``count`` (oe_connector's records.py)."""
    names: set[str] = set()
    aggregates = arguments.get("aggregates")
    for agg in aggregates if isinstance(aggregates, list) else []:
        if not isinstance(agg, dict):
            continue
        alias, fn, fld = agg.get("as"), agg.get("function"), agg.get("field")
        if isinstance(alias, str) and alias:
            names.add(alias)
        elif isinstance(fn, str) and isinstance(fld, str):
            names.add("count" if fld == "*" else f"{fn.lower()}_{fld.replace('.', '_')}")
    return frozenset(names)


class _Reader:
    """Reads the turn's results into lists of rows and single values."""

    def __init__(self, types: ResultTypes) -> None:
        self.types = types
        self.tables: list[_Rows] = []
        # (value, whether it is a row of a cut-off result)
        self.cells: list[tuple[float, bool]] = []
        # Values outside any list of rows: totals, summaries, a document's fields.
        self.headline: list[float] = []
        # Each value's currencies: its result's company currency for a
        # `base_*` field or a report, else its document's or row's own.
        self.currencies: dict[float, set[str]] = {}
        self._company: str | None = None
        self._document: str | None = None
        # Values of fields that are counts (row_count, count, open_count…),
        # with what they count: the field and its result's DocType.
        self.counts: list[tuple[float, str]] = []
        self._doctype = ""
        # Values seen in money columns, and in any other numeric column.
        self.money: set[float] = set()
        self.other: set[float] = set()
        self._currency_fields: frozenset[str] = frozenset()

    def _note_kind(self, value: float, key: str) -> None:
        kind = self.money if _is_money_key(key, self._currency_fields) else self.other
        kind.add(round(abs(value), 6))

    def _note_count(self, value: float, key: str) -> None:
        name = key.lower()
        if name in ("count", "row_count", "total_count", "records") or name.endswith("_count") or name.startswith(
            ("count(", "count_", "number_of")
        ):
            self.counts.append((abs(value), f"{key} of {self._doctype}".lower()))

    def _note_currency(self, value: float, key: str, row_currency: object = None) -> None:
        own = row_currency if isinstance(row_currency, str) and len(row_currency) == 3 else self._document
        currency = self._company if key.startswith("base_") or not own else own
        if currency:
            self.currencies.setdefault(round(abs(value), 6), set()).add(currency)

    def rows(self, rows: list[Any], label: str, truncated: bool, numeric: frozenset[str]) -> None:
        table = _Rows(label=label, truncated=truncated, count=len(rows))
        # Each child table's rows across all the parents, read as one table:
        # four feedback records' ratings, grouped by question.
        children: dict[str, list[Any]] = {}
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                if key in _SKIP_KEYS or key.startswith("_"):
                    continue
                if isinstance(value, list):
                    if value and all(isinstance(v, dict) for v in value):
                        children.setdefault(key, []).extend(value)
                    continue
                # A rating may come as a string ("4"), from a child table's
                # field the parent's numeric fields don't name.
                got = _number(value, key in numeric or _is_rating(key))
                if got is not None:
                    table.numeric.setdefault(key, [None] * len(rows))[i] = got
                    self.cells.append((got, truncated))
                    self._note_currency(got, key, row.get("currency"))
                    self._note_kind(got, key)
                    if not truncated:
                        self._note_count(got, key)
                    if _is_rating(key) and 0 <= got <= 1:
                        # ERPNext keeps a Rating as a fraction (0.8); people
                        # and replies give it out of five (4).
                        stars = round(got * RATING_STARS, 6)
                        table.numeric.setdefault(f"{key} out of {RATING_STARS}", [None] * len(rows))[i] = stars
                        self.cells.append((stars, truncated))
                elif isinstance(value, str) and value and len(value) <= 140:
                    table.text.setdefault(key, [None] * len(rows))[i] = value
        self.tables.append(table)
        for key, child_rows in children.items():
            self.rows(child_rows, f"{label}.{key}", False, frozenset())

    def scalars(self, value: Any, numeric: frozenset[str], key: str = "") -> None:
        if key in _SKIP_KEYS or key.startswith("_"):
            return
        if isinstance(value, dict):
            for k, v in value.items():
                self.scalars(v, numeric, k)
        elif isinstance(value, list):
            for v in value:
                self.scalars(v, numeric, key)
        else:
            got = _number(value, key in numeric or _is_rating(key))
            if got is not None:
                self.cells.append((got, False))
                self.headline.append(got)
                self._note_currency(got, key)
                self._note_kind(got, key)
                self._note_count(got, key)

    def result(self, result: ErpnextResult) -> None:
        payload = result.payload
        if not isinstance(payload, dict):
            return
        truncated = payload.get("truncated") is True
        doctype = payload.get("doctype") or result.arguments.get("doctype")
        numeric = self.types.numeric(doctype) | _aggregate_names(result.arguments)
        # list_records and reports give `currency` (the company's, or the
        # report's presentation currency); get_record gives both.
        self._doctype = str(doctype or payload.get("report_name") or result.arguments.get("report_name") or "")
        company = payload.get("company_currency") or payload.get("currency")
        self._company = company if isinstance(company, str) else None
        document = payload.get("document_currency")
        self._document = document if isinstance(document, str) else None
        # list_records' dotted child fields ("parameters.rating") come back
        # under the child's own name ("rating"): numeric when the child
        # table's field is.
        fields = result.arguments.get("fields")
        for dotted in fields if isinstance(fields, list) else []:
            if isinstance(dotted, str) and dotted.count(".") == 1:
                child, _, name = dotted.partition(".")
                if name in self.types.numeric(doctype, child):
                    numeric |= {name}
        columns = payload.get("columns")
        self._currency_fields = frozenset()
        if isinstance(columns, list):
            numeric |= frozenset(
                c["fieldname"] for c in columns
                if isinstance(c, dict) and isinstance(c.get("fieldname"), str)
                and c.get("fieldtype") in _NUMERIC_FIELDTYPES
            )
            self._currency_fields = frozenset(
                c["fieldname"] for c in columns
                if isinstance(c, dict) and isinstance(c.get("fieldname"), str) and c.get("fieldtype") == "Currency"
            )
        label = str(payload.get("report_name") or doctype or result.tool)
        cut = payload.get("truncated_child_tables")
        for key, value in payload.items():
            if key == "rows" and isinstance(value, list):
                self.rows(value, label, truncated, numeric)
            elif key == "record" and isinstance(value, dict):
                for name, field_value in value.items():
                    if isinstance(field_value, list):
                        self.rows(
                            field_value, f"{label}.{name}", isinstance(cut, dict) and name in cut,
                            self.types.numeric(doctype, name),
                        )
                    else:
                        self.scalars(field_value, numeric, name)
            else:
                self.scalars(value, numeric, key)


def _aggregates(table: _Rows) -> Iterator[tuple[float, str, bool]]:
    """A list of rows' column sums, averages and counts, the same per group of
    rows sharing one text value, and the first and the largest rows' running
    sums. The flag says whether it's a whole column's (a headline value)."""
    yield float(table.count), f"count of {table.label} rows", True
    for group_key, labels in table.text.items():
        distinct = {v for v in labels if v is not None}
        if distinct:
            yield float(len(distinct)), f"count of distinct {group_key} in {table.label}", False
        if 2 <= len(distinct) <= MAX_GROUPS:
            for group in distinct:
                n = sum(1 for v in labels if v == group)
                yield float(n), f"count of {table.label} rows where {group_key} is {group}", False
    for key, cells in table.numeric.items():
        present = [(i, abs(v)) for i, v in enumerate(cells) if v is not None]
        if len(present) < 2:
            continue
        values = [v for _, v in present]
        total = sum(values)
        yield total, f"sum of {key} in {table.label}", True
        yield total / len(values), f"average of {key} in {table.label}", True
        for order, ordered in (("first", values), ("largest", sorted(values, reverse=True))):
            running = 0.0
            for n, v in enumerate(ordered[:MAX_RUN], start=1):
                running += v
                if n >= 2:
                    yield running, f"the {order} {n} rows' {key} in {table.label}", False
        for group_key, labels in table.text.items():
            distinct = {v for v in labels if v is not None}
            if not 2 <= len(distinct) <= MAX_GROUPS:
                continue
            sums: dict[str, float] = {}
            for i, v in present:
                label = labels[i]
                if label is not None:
                    sums[label] = sums.get(label, 0.0) + v
            counted: dict[str, int] = {}
            for i, _v in present:
                label = labels[i]
                if label is not None:
                    counted[label] = counted.get(label, 0) + 1
            for group, s in sums.items():
                yield s, f"sum of {key} in {table.label} where {group_key} is {group}", False
                # Each group's average too: "kept to the schedule, average 4.5"
                # over four feedback records (the pilot's Nov 6, 2026 retake).
                if counted.get(group, 0) >= 2:
                    yield s / counted[group], f"average of {key} in {table.label} where {group_key} is {group}", False


@dataclass
class Sources:
    """What a turn's figures may come from (``sources_for``)."""

    # Every ERPNext value (absolute), sorted.
    values: list[float] = field(default_factory=list)
    # Values only ever seen in rows of a result ERPNext cut off.
    partial: set[float] = field(default_factory=set)
    # Aggregates over whole lists of rows, and over cut-off ones: value -> how.
    complete: dict[float, str] = field(default_factory=dict)
    truncated: dict[float, str] = field(default_factory=dict)
    # Headline values any two of which may be combined, sorted.
    headline: list[float] = field(default_factory=list)
    # Combinations of two values, each sorted: |a - b|, a / b, |b - a| / a.
    differences: list[float] = field(default_factory=list)
    ratios: list[float] = field(default_factory=list)
    changes: list[float] = field(default_factory=list)
    given: list[float] = field(default_factory=list)
    text: list[float] = field(default_factory=list)
    # Counts of whole lists' rows, of their groups and distinct values, and
    # count fields' values, sorted: what a count of records may be.
    counts: list[tuple[float, str]] = field(default_factory=list)
    # Each ERPNext value's currencies, and what each currency mark may stand for.
    value_currencies: dict[float, set[str]] = field(default_factory=dict)
    symbols: dict[str, frozenset[str]] = field(default_factory=dict)
    # Each currency's own symbol, as answer_check_context gives it.
    marks: dict[str, str] = field(default_factory=dict)
    # Values seen only in money columns (an amount, a price, a total).
    money: set[float] = field(default_factory=set)
    _complete_keys: list[float] = field(default_factory=list)
    _truncated_keys: list[float] = field(default_factory=list)

    def currency_codes(self) -> frozenset[str]:
        """Every currency code a mark here may stand for (USD, EUR, …)."""
        codes = {c for group in _DEFAULT_SYMBOLS.values() for c in group}
        codes |= {c for group in self.symbols.values() for c in group}
        return frozenset(c for c in codes if re.fullmatch(r"[A-Z]{3}", c))

    def has_value(self, x: float) -> bool:
        """Whether ``x`` is one of ERPNext's own values."""
        return _near(self.values, abs(x))

    def is_money(self, x: float) -> bool:
        """Whether ``x`` is only ever an amount of money in the results."""
        return round(abs(x), 6) in self.money

    def in_cut_off_rows_only(self, x: float) -> bool:
        """Whether ``x`` appears only in rows of a result ERPNext cut off."""
        return any(abs(v - abs(x)) <= max(abs(x), 1.0) * 1e-9 for v in self.partial)

    def knows(self, x: float, *, percent: bool = False) -> bool:
        """Whether check 1 would itself confirm ``x``: one of ERPNext's values,
        an aggregate of a whole list, a difference of two values, or (for a
        percentage, in percent units) a share or a change. A right value check
        2 names must be one of these: arithmetic of its own choosing over real
        operands can reach almost any number."""
        x = abs(x)
        if _near(self.values, x, 1e-6) or _near(self._complete_keys, x, 1e-6) or _near(self.differences, x, 1e-6):
            return True
        return percent and (_near(self.ratios, x / 100.0, 1e-6) or _near(self.changes, x / 100.0, 1e-6))

    def plus(self, extra: Iterable[float]) -> Sources:
        """These sources with ``extra`` counted as ERPNext's own values."""
        values = sorted({*self.values, *(abs(v) for v in extra)})
        return dataclasses.replace(self, values=values)


def _near(sorted_values: list[float], x: float, rel: float = 1e-9) -> bool:
    pad = max(abs(x), 1.0) * rel
    return _any_in(sorted_values, x - pad, x + pad) is not None


def _any_in(sorted_values: list[float], lo: float, hi: float) -> float | None:
    i = bisect.bisect_left(sorted_values, lo)
    if i < len(sorted_values) and sorted_values[i] <= hi:
        return sorted_values[i]
    return None


def _combine(pairs: Iterable[tuple[float, float]], diffs: set[float], ratios: set[float], changes: set[float]) -> None:
    for a, b in pairs:
        if len(diffs) >= MAX_DERIVED:
            return
        if a == b:
            continue
        diffs.add(round(abs(a - b), 9))
        if b:
            ratios.add(round(a / b, 12))
        if a:
            ratios.add(round(b / a, 12))
            changes.add(round(abs(b - a) / a, 12))
        if b:
            changes.add(round(abs(a - b) / b, 12))


def _capped(texts: Iterable[str]) -> list[str]:
    """``texts`` within MAX_TEXT_PIECE each and MAX_TEXT_CHARS in all, the
    newest (last) first."""
    out: list[str] = []
    left = MAX_TEXT_CHARS
    for text in reversed(list(texts)):
        if left <= 0:
            break
        piece = text[: min(MAX_TEXT_PIECE, left)]
        out.append(piece)
        left -= len(piece)
    return out


def sources_for(
    results: Iterable[ErpnextResult],
    *,
    types: ResultTypes | None = None,
    given: Iterable[str] = (),
    texts: Iterable[str] = (),
    extra: Iterable[float] = (),
    fmt: NumberFormat = PLAIN,
    currencies: Any = None,
) -> Sources:
    """Everything the turn's figures may come from. ``extra`` adds values that
    count as ERPNext's own (a correction's right values, found there by code)."""
    reader = _Reader(types or ResultTypes())
    for result in results:
        reader.result(result)
        if len(reader.cells) > MAX_VALUES:
            break
    cells = reader.cells[:MAX_VALUES]
    extra = [abs(v) for v in extra]
    src = Sources()
    src.value_currencies = reader.currencies
    src.money = reader.money - reader.other
    src.symbols = _symbols(currencies)
    src.marks = {
        code: info["symbol"].strip()
        for code, info in (currencies.items() if isinstance(currencies, dict) else ())
        if isinstance(code, str) and isinstance(info, dict)
        and isinstance(info.get("symbol"), str) and info["symbol"].strip()
    }
    src.values = sorted({abs(v) for v, _ in cells} | set(extra))
    whole = {abs(v) for v, cut in cells if not cut} | set(extra)
    src.partial = {abs(v) for v, cut in cells if cut} - whole
    headline = {abs(v) for v in reader.headline} | set(extra)
    counted: set[tuple[float, str]] = set(reader.counts)
    for table in reader.tables:
        target = src.truncated if table.truncated else src.complete
        for value, how, whole_column in _aggregates(table):
            target.setdefault(round(value, 9), how)
            if not table.truncated and how.startswith("count of "):
                # Matched on the list or the field, never a row's text value
                # ("rows where customer is Invoice Masters Ltd"): a group's
                # rows count the list's records, distinct values the field's.
                label = how.lower().split(" where ", 1)[0]
                if label.startswith("count of distinct "):
                    label = label.split(" in ", 1)[0]
                counted.add((round(value, 9), label))
            if whole_column and not table.truncated:
                headline.add(abs(value))
    src._complete_keys = sorted(src.complete)
    src._truncated_keys = sorted(src.truncated)
    src.counts = sorted(counted)
    src.headline = sorted(v for v in headline if v)[:MAX_HEADLINE]
    diffs: set[float] = set()
    ratios: set[float] = set()
    changes: set[float] = set()
    # Any two headline values; two cells of one row; one column's cells in
    # neighbouring rows (month on month).
    _combine(((a, b) for i, a in enumerate(src.headline) for b in src.headline[i + 1:]), diffs, ratios, changes)
    for table in reader.tables:
        if table.truncated:
            continue
        columns = list(table.numeric.values())
        for i in range(table.count):
            row = [abs(v) for c in columns if (v := c[i]) is not None]
            _combine(((a, b) for j, a in enumerate(row) for b in row[j + 1:]), diffs, ratios, changes)
        for column in columns:
            present = [abs(v) for v in column if v is not None]
            _combine(zip(present, present[1:], strict=False), diffs, ratios, changes)
            # Each row's share of the column's total, and the first and the
            # largest rows' ("North holds 26%", "the top 3 hold 80%").
            total = sum(present)
            if total and len(present) >= 2:
                shares = [*present]
                for ordered in (present, sorted(present, reverse=True)):
                    running = 0.0
                    for v in ordered[:MAX_RUN]:
                        running += v
                        shares.append(running)
                ratios.update(round(v / total, 12) for v in shares)
    src.differences, src.ratios, src.changes = sorted(diffs), sorted(ratios), sorted(changes)
    src.given = sorted({v for text in _capped(given) for v in numbers_in(text, fmt)})
    src.text = sorted({v for text in _capped(texts) for v in numbers_in(text, fmt)})
    return src


def _symbols(currencies: Any) -> dict[str, frozenset[str]]:
    """Each currency mark's codes: answer_check_context's ``currencies`` (code
    -> symbol), over the common symbols; a code stands for itself."""
    out: dict[str, set[str]] = {k: set(v) for k, v in _DEFAULT_SYMBOLS.items()}
    if isinstance(currencies, dict):
        for code, info in currencies.items():
            symbol = info.get("symbol") if isinstance(info, dict) else None
            if isinstance(code, str) and isinstance(symbol, str) and symbol.strip():
                out.setdefault(symbol.strip(), set()).add(code)
    return {k: frozenset(v) for k, v in out.items()}


def _mark_for(currency: str, src: Sources) -> str:
    """How a reply writes ``currency``: its own symbol, else its code."""
    if currency in src.marks:
        return src.marks[currency]
    marks = [mark for mark, codes in src.symbols.items() if codes == frozenset({currency})]
    return marks[0] if marks else currency


def _check_currency(fig: Figure, src: Sources) -> None:
    """A figure confirmed by one ERPNext value, written in another currency
    than that value's own, is ``wrong_currency`` (a € sign on a dollar total)."""
    if fig.status != "confirmed" or fig.matched is None or not fig.currency_text:
        return
    mark = fig.currency_text
    written = frozenset({mark}) if mark.isalpha() and mark.isupper() else src.symbols.get(mark, frozenset())
    known = src.value_currencies.get(round(abs(fig.matched), 6))
    if not written or not known or written & known or len(known) != 1:
        return
    (actual,) = known
    fig.status, fig.currency_fix = "wrong_currency", _mark_for(actual, src)
    fig.how = f"a value in {actual}"


def _pair(pool: list[float], fig: Figure) -> tuple[float, str] | None:
    """A difference, ratio or percentage change of two ``pool`` values that
    ``fig`` matches (the figures the reply already confirmed)."""
    for lo, hi in fig.intervals():
        for a in pool:
            for b_lo, b_hi in ((a - hi, a - lo), (a + lo, a + hi)):
                b = _any_in(pool, b_lo, b_hi)
                if b is not None and b != a:
                    return abs(a - b), f"{a:g} and {b:g}, apart"
            if fig.percent and lo > 0:
                b = _any_in(pool, a / hi, a / lo)
                if b is not None:
                    return a / b, f"{a:g} of {b:g}"
                for b_lo, b_hi in ((a * (1 + lo), a * (1 + hi)), (a * (1 - hi), a * (1 - lo))):
                    b = _any_in(pool, b_lo, b_hi)
                    if b is not None and b != a and a:
                        return abs(b - a) / a, f"from {a:g} to {b:g}"
    return None


def _classify(fig: Figure, src: Sources) -> None:
    intervals = fig.intervals()

    def first(values: list[float]) -> float | None:
        return next((v for lo, hi in intervals if (v := _any_in(values, lo, hi)) is not None), None)

    if (v := first(src.given)) is not None:
        fig.status, fig.matched, fig.how = "given", v, "the person's own message"
    elif fig.count:
        words = count_words(fig.count)
        of_it = sorted({v for v, how in src.counts if any(w in how for w in words)})
        if (v := first(of_it)) is not None:
            fig.status, fig.matched, fig.how = "confirmed", v, "a count of what the turn read"
        elif (v := first(src.text)) is not None:
            fig.status, fig.matched, fig.how = "from_text", v, "other text the turn read"
        else:
            fig.status, fig.matched, fig.how = "unconfirmed", None, ""
    elif (v := first(src.values)) is not None:
        fig.status, fig.matched, fig.how = "confirmed", v, "a value in ERPNext's results"
    elif (v := first(src._complete_keys)) is not None:
        fig.status, fig.matched, fig.how = "confirmed", v, src.complete[v]
    elif fig.multiple:
        if (v := first(src.ratios)) is not None:
            fig.status, fig.matched, fig.how = "confirmed", v, "one value as a multiple of another"
        elif (v := first(src.text)) is not None:
            fig.status, fig.matched, fig.how = "from_text", v, "other text the turn read"
        else:
            fig.status, fig.matched, fig.how = "unconfirmed", None, ""
    elif (v := first(src.differences)) is not None:
        fig.status, fig.matched, fig.how = "confirmed", v, "the difference of two values"
    elif fig.percent and (v := first(src.ratios)) is not None:
        fig.status, fig.matched, fig.how = "confirmed", v, "one value as a share of another"
    elif fig.percent and (v := first(src.changes)) is not None:
        fig.status, fig.matched, fig.how = "confirmed", v, "the change from one value to another"
    elif (v := first(src._truncated_keys)) is not None:
        fig.status, fig.matched, fig.how = "from_truncated", v, src.truncated[v]
    elif (v := first(src.text)) is not None:
        fig.status, fig.matched, fig.how = "from_text", v, "other text the turn read"
    else:
        fig.status, fig.matched, fig.how = "unconfirmed", None, ""


def _run_sum(before: list[Figure], fig: Figure) -> float | None:
    """The sum of two or more figures just before ``fig``, all confirmed."""
    total = 0.0
    for n, prev in enumerate(reversed(before), start=1):
        if prev.status not in ("confirmed", "given") or prev.matched is None or prev.percent:
            return None
        total += abs(prev.matched)
        if n >= 2 and fig.matches(total):
            return total
    return None


def check_figures(text: str, sources: Sources, fmt: NumberFormat = PLAIN) -> list[Figure]:
    """Every figure of ``text`` with its status (module docstring)."""
    figures = find_figures(text, fmt)[:MAX_FIGURES]
    for fig in figures:
        _classify(fig, sources)
        _check_currency(fig, sources)
    if any(f.flagged for f in figures):
        _from_the_reply(text, figures, sources, fmt)
    _own_differences(text, figures)
    return figures


# "$3.76 a board under the BOM": a difference the reply states between the two
# figures before it. Never "over the next quarter", nor "under $X", whose
# second figure follows.
_DIFFERENCE_AFTER = re.compile(
    r"[ \t]*(?:(?:a|an|per|each)[ \t]+[A-Za-z]+[ \t]+)?"
    r"(?:under|over|below|above|less[ \t]+than|more[ \t]+than|cheaper[ \t]+than|short[ \t]+of)\b"
    r"(?![ \t]+(?:the[ \t]+)?(?:next|last|past|previous|coming|period|time|year|month|week|quarter)\b)"
    r"(?![ \t]*[\d" + _CURRENCY_SYMBOLS + r"])"
    # What it is under or over: "the BOM", "budget".
    r"[ \t]+(?:(?:the|its|our|their|a|an)[ \t]+)?(?P<noun>[A-Za-z][\w-]*)"
)
_TOTAL_CELL = re.compile(r"(?i)(?:grand[ \t]+)?totals?\b")


def _written(fig: Figure) -> float:
    return fig.readings[0][0] * fig.scale


def _in_table_line(text: str, pos: int) -> bool:
    return text[text.rfind("\n", 0, pos) + 1:pos].lstrip().startswith("|")


def _total_row_figures(text: str, figures: list[Figure]) -> list[Figure]:
    """The passed figures of the reply's own tables' total rows."""
    out: list[Figure] = []
    for fig in figures:
        line = text[text.rfind("\n", 0, fig.start) + 1:fig.start]
        if not line.lstrip().startswith("|") or fig.status not in ("confirmed", "given") or fig.percent:
            continue
        first = _cells(line + "|")[0].strip(" *_~")
        if _TOTAL_CELL.match(first):
            out.append(fig)
    return out


def _own_differences(text: str, figures: list[Figure]) -> None:
    """An opening figure the reply's own table and stated difference both
    contradict: "costs $174.90 against a BOM of $188.40 … $3.76 a board under
    the BOM" beside a table whose Total is $192.16. The difference matches
    neither the two figures before it, but does when the table's total takes
    one of their places: that one is written from the total (``arithmetic``),
    as a product the reply works out wrong is. Only then: any other mismatch
    may compare with something the sentence doesn't show (a demo reshoot).

    All three figures lie in one sentence, and what the difference is under or
    over ("the BOM") names one of the two figures right before it ("a BOM of
    $188.40"): that one is the reference, and only the other is ever written
    from the total."""
    from openexecutive.orchestrator.answer_correct import unit_of, units

    totals = _total_row_figures(text, figures)
    if not totals:
        return
    all_units = units(text)
    for i, diff in enumerate(figures):
        if not diff.currency_text or diff.percent:
            continue
        stated = _DIFFERENCE_AFTER.match(text, diff.end)
        if stated is None or _in_table_line(text, diff.start):
            continue
        sentence = unit_of(all_units, diff.start, diff.end)
        if sentence is None or sentence.kind != "sentence":
            continue
        before = [
            f for f in figures[:i]
            if sentence.start <= f.start and not f.percent and f.currency_text == diff.currency_text
        ]
        if len(before) < 2:
            continue
        a, b = before[-2], before[-1]
        if a.status not in ("confirmed", "given") or b.status not in ("confirmed", "given"):
            continue
        if diff.matches(abs(_written(a) - _written(b))):
            continue
        noun = re.compile(r"\b" + re.escape(stated.group("noun")) + r"\b[^$\d]{0,12}$", re.I)
        named = [f for f in (a, b) if noun.search(text[max(sentence.start, f.start - 40):f.start])]
        if len(named) != 1:
            continue
        (reference,) = named
        x = b if reference is a else a
        fixes = {
            round(_written(t), 6): _written(t)
            for t in totals if t.currency_text == diff.currency_text
            if not x.matches(_written(t)) and diff.matches(abs(_written(t) - _written(reference)))
        }
        if len(fixes) == 1:
            (value,) = fixes.values()
            x.status, x.matched, x.how, x.arithmetic = "unconfirmed", None, "", value


def _from_the_reply(text: str, figures: list[Figure], sources: Sources, fmt: NumberFormat) -> None:
    """Flagged figures the reply worked out from figures it already gave
    ("together $3,000", "up 12%"), held to those figures' values."""
    confirmed = sorted({abs(f.matched) for f in figures if f.status == "confirmed" and f.matched})
    pool = sorted({*confirmed, *sources.headline})
    for i, fig in enumerate(figures):
        if not fig.flagged:
            continue
        # "5 × $71.00 = $355.00": held to its own arithmetic, not to the
        # records (a product is in none of them), when every figure in the
        # expression itself passed (as _run_sum asks of a running sum). A
        # figure in the wrong currency keeps that flag; its mark is the fault.
        worked = own_arithmetic(text, fig.start, fmt, sources.currency_codes())
        results = _held_results(worked, figures) if worked and fig.status != "wrong_currency" else []
        right = next((r for r in results if fig.matches(r)), None)
        if right is not None:
            fig.status, fig.matched, fig.how = "confirmed", right, "the reply's own arithmetic"
            continue
        if len(results) == 1 and not fig.percent:
            # One reading, one value: the figure should be it. A percentage
            # is never written from a quotient (0.44 is 44%, not 0%).
            fig.arithmetic = results[0]
            continue
        run = _run_sum(figures[:i], fig)
        if run is not None:
            fig.status, fig.matched, fig.how = "confirmed", run, "the sum of the figures before it"
            continue
        pair = _pair(pool, fig) if confirmed else None
        if pair is not None:
            fig.status, fig.matched, fig.how = "confirmed", pair[0], pair[1]


# What may stand in an expression the reply works out: numbers, operators,
# brackets and spaces, once currency marks and known currency codes are out.
_EXPR_OPS = str.maketrans({"×": "*", "x": "*", "X": "*", "·": "*", "÷": "/", "−": "-", "–": "-"})
_EXPR_SYMBOLS = re.compile(r"[" + _CURRENCY_SYMBOLS + r"]")
# Where a clause starts: punctuation and a space, or a new line (a decimal
# point or a thousands comma has no space after it).
_EXPR_BOUNDARY = re.compile(r"[.;:!?,]\s|\n")
# A Markdown list marker at the clause's start, never a sign.
_EXPR_LIST_MARK = re.compile(r"\s*(?:[-–*+•]|\d+[.)])\s+")
_EXPR_NUMBER = re.compile(r"\d[\d.,'’]*\d|\d")
# A number in an expression that is no figure: a bare quantity ("5 ×"),
# never one led by a zero ("01/10" is a date).
_EXPR_QUANTITY = re.compile(r"[1-9]\d{0,2}")
_EXPR_MAX = 120


@dataclass(frozen=True)
class Worked:
    """The arithmetic a reply writes before an "=": where its first
    expression starts and ends in the text, and per way its numbers read
    (the site's format, then the plain one) the value and each number of that
    expression as (start, end, text, value)."""

    start: int
    end: int
    readings: tuple[tuple[float, tuple[tuple[int, int, str, float], ...]], ...]


def own_arithmetic(
    text: str, start: int, fmt: NumberFormat = PLAIN, codes: frozenset[str] = frozenset(),
) -> Worked | None:
    """The arithmetic the reply writes right before ``start`` and an "="
    ("13 × ($73.84 − $71.00) = …", or a chain "13 × ($73.84 − $71.00) = 13 ×
    $2.84 = …" whose every step gives the same value), or None when there is
    none to work out: only numbers, + − × ÷ and brackets, after currency
    marks and the codes in ``codes``, each step starting with a number or a
    bracket (a list marker before it is no sign), at most _EXPR_MAX
    characters back. Never evaluates anything else."""
    window_start = max(0, start - _EXPR_MAX - 2)
    stripped = text[window_start:start].rstrip()
    if not stripped.endswith("="):
        return None
    clause = stripped[:-1]
    last = None
    for found in _EXPR_BOUNDARY.finditer(clause):
        last = found
    if last is None and window_start > 0:
        return None  # longer than the window: never a number cut in half
    offset = last.end() if last else 0
    # A list marker only opens a line; after ": " or ", " a dash is a sign
    # (which the check below then refuses).
    mark = _EXPR_LIST_MARK.match(clause, offset) if last is None or last.group(0).endswith("\n") else None
    if mark:
        offset = mark.end()
    # Same length throughout, so positions in it are positions in the text.
    expr = _EXPR_SYMBOLS.sub(" ", clause[offset:])
    if codes:
        expr = re.sub(
            r"\b(?:" + "|".join(re.escape(c) for c in sorted(codes)) + r")\b",
            lambda m: " " * len(m.group(0)), expr,
        )
    expr = expr.translate(_EXPR_OPS)
    base = window_start + offset
    steps: list[tuple[int, str]] = []
    pos = 0
    for part in expr.split("="):
        steps.append((pos, part))
        pos += len(part) + 1
    for _, step in steps:
        body = step.strip()
        if not body or not re.fullmatch(r"[\d.,'’\s*/+\-()]+", body) or body[0] not in "0123456789(":
            return None
    first_at, first = steps[0]
    if not re.search(r"[*/+\-]", _EXPR_NUMBER.sub("", first)):
        return None
    readings: list[tuple[float, tuple[tuple[int, int, str, float], ...]]] = []
    for candidate in (fmt, PLAIN) if fmt != PLAIN else (fmt,):
        values: list[float] = []
        numbers: tuple[tuple[int, int, str, float], ...] = ()
        for k, (at, step) in enumerate(steps):
            got = _work_out(step, candidate)
            if got is None:
                break
            values.append(got[0])
            if k == 0:
                numbers = tuple(
                    (base + at + n_start, base + at + n_end, raw, value)
                    for n_start, n_end, raw, value in got[1]
                )
        else:
            # Every step of a chain gives the same value, to the cent.
            if all(abs(v - values[0]) <= 0.005 + 1e-9 * values[0] for v in values) and not any(
                abs(values[0] - r) <= 1e-9 * max(1.0, r) for r, _ in readings
            ):
                readings.append((values[0], numbers))
    if not readings:
        return None
    start_at = base + first_at + (len(first) - len(first.lstrip()))
    return Worked(start_at, base + first_at + len(first.rstrip()), tuple(readings))


def _work_out(step: str, fmt: NumberFormat) -> tuple[float, list[tuple[int, int, str, float]]] | None:
    """``step``'s value with every number read in ``fmt``, and its numbers;
    None when one can't be read that way or it can't be worked out."""
    numbers: list[tuple[int, int, str, float]] = []
    parts: list[str] = []
    pos = 0
    for m in _EXPR_NUMBER.finditer(step):
        got = _reading(m.group(0), fmt)
        if got is None:
            return None
        numbers.append((m.start(), m.end(), m.group(0), got[0]))
        parts.append(step[pos:m.start()])
        parts.append(repr(got[0]))
        pos = m.end()
    parts.append(step[pos:])
    if not numbers:
        return None
    try:
        return abs(_evaluate("".join(parts))), numbers
    except (ValueError, ZeroDivisionError, RecursionError, OverflowError):
        return None


def _held_results(worked: Worked, figures: list[Figure]) -> list[float]:
    """The values of ``worked`` whose numbers are what the reply's figures
    were held to: at least one figure is in it and every one passed, each
    number in a figure reads as that figure's value, and any other number is
    a bare quantity ("5 ×"), never a date, a range or a record id. A figure
    counts as in it when it overlaps it at all ("$500" where the "$" lies
    before the expression's first digit), so no figure passes as a quantity."""
    operands = [g for g in figures if g.start < worked.end and worked.start < g.end]
    if not operands or any(g.status not in ("confirmed", "given") for g in operands):
        return []
    if any(g.text.startswith("(") for g in operands):
        return []  # "($923.00)" is a negative the expression would read as positive
    out: list[float] = []
    for value, numbers in worked.readings:
        held = True
        for n_start, n_end, raw, n_value in numbers:
            owner = next((g for g in operands if g.start <= n_start and n_end <= g.end), None)
            if owner is None and any(g.start < n_end and n_start < g.end for g in figures):
                held = False  # part of a figure it doesn't lie wholly in
                break
            if owner is None:
                held = _EXPR_QUANTITY.fullmatch(raw) is not None
            else:
                held = owner.matches(n_value) and (
                    owner.matched is None or abs(abs(owner.matched) - n_value) <= 1e-9 * max(1.0, n_value)
                )
            if not held:
                break
        if held:
            out.append(value)
    return out


def _evaluate(expr: str) -> float:
    """``expr`` (numbers, + - * /, brackets) worked out by a small parser."""
    tokens = re.findall(r"\d+(?:\.\d+)?(?:e[+-]?\d+)?|[*/+\-()]", expr)
    if "".join(tokens) != re.sub(r"\s+", "", expr) or len(tokens) > 40:
        raise ValueError("not an expression")
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("cut short")
        pos += 1
        return tokens[pos - 1]

    def atom() -> float:
        tok = take()
        if tok == "(":
            value = sum_()
            if take() != ")":
                raise ValueError("unclosed")
            return value
        if tok == "-":
            return -atom()
        if re.fullmatch(r"\d+(?:\.\d+)?(?:e[+-]?\d+)?", tok):
            return float(tok)
        raise ValueError("unexpected " + tok)

    def product() -> float:
        value = atom()
        while peek() in ("*", "/"):
            if take() == "*":
                value *= atom()
            else:
                value /= atom()
        return value

    def sum_() -> float:
        value = product()
        while peek() in ("+", "-"):
            if take() == "+":
                value += product()
            else:
                value -= product()
        return value

    value = sum_()
    if pos != len(tokens):
        raise ValueError("trailing")
    return value


# ---------------------------------------------------------------------------
# Writing a value the way a figure was written


def _group(int_digits: str, fmt: NumberFormat) -> str:
    if len(int_digits) <= 3:
        return int_digits
    tail, head = int_digits[-3:], int_digits[:-3]
    size = 2 if fmt.indian else 3
    parts: list[str] = []
    while len(head) > size:
        parts.insert(0, head[-size:])
        head = head[:-size]
    if head:
        parts.insert(0, head)
    return fmt.group.join([*parts, tail])


def format_like(fig: Figure, value: float) -> str:
    """``value`` written as ``fig`` was: its currency mark, sign, scale,
    separators and decimals ("$1.2M", "1.234,50 €", "43%")."""
    decimals = fig.readings[0][1]
    scale, scale_text = fig.scale, fig.scale_text
    x = abs(value)
    mantissa = x / scale
    if scale > 1 and round(mantissa, decimals) == 0:
        # Too small for the reply's scale ("$0.0M"): write it whole.
        mantissa, scale_text, decimals = x, "", 0
    int_digits, _, frac = f"{mantissa:.{decimals}f}".partition(".")
    grouped = fig.fmt.group in fig.num or len(int_digits) > 4
    body = _group(int_digits, fig.fmt) if grouped else int_digits
    if frac:
        body += fig.fmt.decimal + frac
    pre, post = fig.pre, fig.post
    if fig.currency_fix and fig.currency_text:
        if fig.currency_text in pre:
            pre = pre.replace(fig.currency_text, fig.currency_fix, 1)
        else:
            post = post.replace(fig.currency_text, fig.currency_fix, 1)
    if fig.scale_text and scale_text != fig.scale_text:
        post = post.replace(fig.scale_text, "", 1).lstrip() if post.lstrip().startswith(fig.scale_text) else post
    return f"{pre}{body}{post}"
