"""Check 1 of the answer check: every figure in a reply against the turn's sources.

oe_connector's ``docs/answer-check-plan.md``, section 2. No model call: code
reads each figure the reply gives (an amount, a percentage, a scaled number, a
quantity of something) and looks for where it came from, among:

- **ERPNext's values**: a JSON number in an ``erpnext__*`` result of the turn,
  or a numeric string in a field or report column whose type is numeric. A
  number written inside a text field (a remark, a description) never counts:
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
import functools
import json
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["confirmed", "given", "from_text", "from_truncated", "unconfirmed"]
FLAGGED: frozenset[str] = frozenset({"from_truncated", "unconfirmed"})

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
# Words before a plain whole number that make it an ordinal or a label.
_LABEL_BEFORE = frozenset({
    "top", "first", "last", "next", "past", "step", "phase", "option", "number", "no", "page", "line",
    "version", "chapter", "section", "rank", "ranked", "level", "tier", "round", "week", "day",
    "quarter", "q", "fy", "h", "item", "row", "table", "figure", "question", "point", "part",
})
# Three capitals before or after a number that are words, not currency codes.
_NOT_CURRENCY = frozenset({"THE", "AND", "FOR", "NOT", "BUT", "ALL", "ANY", "ERP", "API", "PDF", "CSV", "YOY"})

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
    # The value it was matched to, when one was (a source value or a combination).
    matched: float | None = None
    how: str = ""

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
    return spans


def _word_before(text: str, pos: int) -> str:
    m = re.search(r"([A-Za-z]+)[\s#.:]*$", text[max(0, pos - 40):pos])
    return m.group(1).lower() if m else ""


def _word_after(text: str, pos: int) -> str:
    words = _WORD_RE.findall(text[pos:pos + 40])
    return words[0].lower() if words else ""


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


def find_figures(text: str, fmt: NumberFormat = PLAIN, *, everything: bool = False) -> list[Figure]:
    """The figures ``text`` gives, in order. ``everything`` reads every number
    that isn't a date, an id or list numbering (a source's text, where a plain
    "14" may be what the reply repeats)."""
    masked = _masked(text)
    figures: list[Figure] = []
    for m in _figure_re(fmt.group == " ").finditer(text):
        num = m.group("num")
        num_start, num_end = m.span("num")
        if any(s < num_end and num_start < e for s, e in masked):
            continue
        before = text[num_start - 1] if num_start > 0 else ""
        after = text[num_end] if num_end < len(text) else ""
        pre, post = m.group("pre") or "", m.group("post") or ""
        scale_text, pct = m.group("scale") or "", bool(m.group("pct"))
        if pre.strip().isalpha() and (pre.strip() in _NOT_CURRENCY or not pre.strip().isupper()):
            pre = ""
        if post.strip().isalpha() and post.strip() in _NOT_CURRENCY:
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
            if 1900 <= value <= 2100 and len(num) == 4:
                continue  # a year
            if not everything:
                if _word_before(text, m.start()) in _MONTHS or _word_after(text, num_end) in _MONTHS:
                    continue  # "Nov 4", "4 November"
                if not (_in_table_cell(text, num_start, num_end) or value >= 10_000):
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
            )
        )
    return figures


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

    def rows(self, rows: list[Any], label: str, truncated: bool, numeric: frozenset[str]) -> None:
        table = _Rows(label=label, truncated=truncated, count=len(rows))
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                if key in _SKIP_KEYS or key.startswith("_"):
                    continue
                if isinstance(value, list):
                    if value and all(isinstance(v, dict) for v in value):
                        self.rows(value, f"{label}.{key}", False, frozenset())
                    continue
                got = _number(value, key in numeric)
                if got is not None:
                    table.numeric.setdefault(key, [None] * len(rows))[i] = got
                    self.cells.append((got, truncated))
                elif isinstance(value, str) and value and len(value) <= 140:
                    table.text.setdefault(key, [None] * len(rows))[i] = value
        self.tables.append(table)

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
            got = _number(value, key in numeric)
            if got is not None:
                self.cells.append((got, False))
                self.headline.append(got)

    def result(self, result: ErpnextResult) -> None:
        payload = result.payload
        if not isinstance(payload, dict):
            return
        truncated = payload.get("truncated") is True
        doctype = payload.get("doctype") or result.arguments.get("doctype")
        numeric = self.types.numeric(doctype) | _aggregate_names(result.arguments)
        columns = payload.get("columns")
        if isinstance(columns, list):
            numeric |= frozenset(
                c["fieldname"] for c in columns
                if isinstance(c, dict) and isinstance(c.get("fieldname"), str)
                and c.get("fieldtype") in _NUMERIC_FIELDTYPES
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
            for group, s in sums.items():
                yield s, f"sum of {key} in {table.label} where {group_key} is {group}", False


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
    _complete_keys: list[float] = field(default_factory=list)
    _truncated_keys: list[float] = field(default_factory=list)

    def has_value(self, x: float) -> bool:
        """Whether ``x`` is one of ERPNext's own values."""
        return _near(self.values, abs(x))

    def in_cut_off_rows_only(self, x: float) -> bool:
        """Whether ``x`` appears only in rows of a result ERPNext cut off."""
        return any(abs(v - abs(x)) <= max(abs(x), 1.0) * 1e-9 for v in self.partial)


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


def sources_for(
    results: Iterable[ErpnextResult],
    *,
    types: ResultTypes | None = None,
    given: Iterable[str] = (),
    texts: Iterable[str] = (),
    extra: Iterable[float] = (),
    fmt: NumberFormat = PLAIN,
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
    src.values = sorted({abs(v) for v, _ in cells} | set(extra))
    whole = {abs(v) for v, cut in cells if not cut} | set(extra)
    src.partial = {abs(v) for v, cut in cells if cut} - whole
    headline = {abs(v) for v in reader.headline} | set(extra)
    for table in reader.tables:
        target = src.truncated if table.truncated else src.complete
        for value, how, whole_column in _aggregates(table):
            target.setdefault(round(value, 9), how)
            if whole_column and not table.truncated:
                headline.add(abs(value))
    src._complete_keys = sorted(src.complete)
    src._truncated_keys = sorted(src.truncated)
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
    src.differences, src.ratios, src.changes = sorted(diffs), sorted(ratios), sorted(changes)
    src.given = sorted({v for text in given for v in numbers_in(text, fmt)})
    src.text = sorted({v for text in texts for v in numbers_in(text, fmt)})
    return src


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
    elif (v := first(src.values)) is not None:
        fig.status, fig.matched, fig.how = "confirmed", v, "a value in ERPNext's results"
    elif (v := first(src._complete_keys)) is not None:
        fig.status, fig.matched, fig.how = "confirmed", v, src.complete[v]
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
    if not any(f.flagged for f in figures):
        return figures
    # What the reply worked out from figures it already gave ("together
    # $3,000", "up 12%"), from those figures' values.
    confirmed = sorted({abs(f.matched) for f in figures if f.status == "confirmed" and f.matched})
    pool = sorted({*confirmed, *sources.headline})
    for i, fig in enumerate(figures):
        if not fig.flagged:
            continue
        run = _run_sum(figures[:i], fig)
        if run is not None:
            fig.status, fig.matched, fig.how = "confirmed", run, "the sum of the figures before it"
            continue
        pair = _pair(pool, fig) if confirmed else None
        if pair is not None:
            fig.status, fig.matched, fig.how = "confirmed", pair[0], pair[1]
    return figures


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
    post = fig.post
    if fig.scale_text and scale_text != fig.scale_text:
        post = post.replace(fig.scale_text, "", 1).lstrip() if post.lstrip().startswith(fig.scale_text) else post
    return f"{fig.pre}{body}{post}"
