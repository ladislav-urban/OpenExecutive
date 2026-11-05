"""Names in a reply held to the turn's ERPNext results (oe_connector's
``docs/answer-check-plan.md``, section 2, "Names"; PR (b)).

Decided Nov 4, 2026 (the owner): "just the numbers and names must be exact".
Every name of a record the reply gives (a customer, supplier, item, project,
account, warehouse, or a document's own name such as ``SINV-00042``) must
match a name in the turn's results exactly.

**Where a name may come from.** Only fields that hold names: each row's or
document's ``name``, its Link and Dynamic Link fields and its title field (from
``answer_check_context``'s ``names``, child tables included), a report's Link
columns and its ``*_name`` columns, and a result's ``company``. Never a remark,
a description or any other text field. What the person gave counts too (their
message, the page), and so does other text the turn read (the org block's
People, the conversation so far), as ``from_text``.

**What counts as a name in the reply**, so ordinary capitalised words are
never checked:

- a document name: letters, a dash, digits (``SINV-00042``, ``PO-2026-0012``,
  ``ACC-PINV-2026-00007``), on any naming series. Never matched loosely:
  ``PO-0012`` is the name or it isn't;
- a cell of a Markdown table column whose header is a name field's label
  ("Customer", "Item", a report's Link column's label);
- bold text that opens a list item with a figure on it ("- **Northwind
  Supplies**: $12,500"), unless every word of it is an ordinary label word;
- any other run of capitalised words, only when it is close to a source name
  without being one (below).

**Statuses.** ``confirmed`` (exactly a source name), ``given``, ``from_text``,
``near`` (the same as exactly one source name ignoring case, spacing and
punctuation, or within a few edits: none up to 5 characters, 1 up to 12, 2
above, so "ACME" never becomes "ACNE"; ``right`` is that name and code writes
it in), and ``unconfirmed`` (a document name, a cell or a bold name with no
source, close to two or more, or only contained in longer ones; its right
value is "not known", so the correction leaves it out). Only ``near`` and
``unconfirmed`` are flagged.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from openexecutive.orchestrator.figure_check import ErpnextResult

NameStatus = Literal["confirmed", "given", "from_text", "near", "unconfirmed"]
NameKind = Literal["document", "cell", "bold", "phrase"]

# Names kept from one turn's results.
MAX_NAMES = 5_000
MAX_NAME_CHARS = 140
# Names checked in one reply.
MAX_CANDIDATES = 120

_DOCUMENT_RE = re.compile(r"(?<![\w-])[A-Za-z]{1,12}(?:-[A-Za-z]{1,12})*-(?:\d{2,}-)*\d{3,}(?:-\d{1,3})?(?![\w-])")
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_URL_RE = re.compile(r"\]\([^)]*\)|https?://\S+")
_BOLD_ITEM_RE = re.compile(r"(?m)^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\*\*([^*\n]{2,100})\*\*(.*)$")
_CAP_WORD = r"(?:[A-Z][\w&'’.]*|[A-Z0-9][A-Z0-9&]+)"
_CONNECTOR = r"(?:of|and|the|de|la|du|van|von|&|for)"
_PHRASE_RE = re.compile(rf"{_CAP_WORD}(?:[ ](?:{_CONNECTOR}[ ])?{_CAP_WORD}){{0,6}}")
_NORM_DROP = re.compile(r"[^\w\s&]")
_SPACES = re.compile(r"\s+")

# Column labels that hold names, besides a report's own Link columns' labels
# and the names of the DocTypes the turn read.
NAME_LABELS = frozenset({
    "customer", "customer name", "supplier", "supplier name", "party", "party name", "item", "item code",
    "item name", "project", "project name", "account", "account name", "warehouse", "company",
    "invoice", "order", "document", "sales invoice", "purchase invoice", "sales order", "purchase order",
    "quotation", "material request", "delivery note", "purchase receipt", "payment entry", "journal entry",
    "territory", "customer group", "supplier group", "item group", "cost center", "task", "lead",
    "opportunity", "debtor", "creditor", "vendor", "client", "product", "sku",
})
# Words a bold list label may be made of without naming a record.
_LABEL_WORDS = frozenset({
    "total", "totals", "grand", "net", "gross", "outstanding", "overdue", "due", "paid", "unpaid", "open",
    "closed", "pending", "draft", "submitted", "cancelled", "status", "summary", "revenue", "sales",
    "purchases", "profit", "loss", "margin", "cash", "bank", "balance", "stock", "inventory", "value",
    "receivables", "receivable", "payables", "payable", "invoices", "invoice", "orders", "order", "items",
    "item", "customers", "suppliers", "amount", "amounts", "cost", "costs", "expenses", "income", "this",
    "last", "next", "month", "week", "year", "quarter", "today", "yesterday", "current", "previous", "key",
    "top", "risk", "risks", "action", "actions", "note", "notes", "warning", "average", "count", "number",
    "days", "aging", "ageing", "bucket", "period", "ytd", "mtd", "change", "growth", "decline", "share",
    "largest", "biggest", "smallest", "other", "others", "rest", "all", "new", "old", "in", "of", "and",
    "the", "a", "to", "from", "for", "by", "on", "at", "per", "vs", "across", "trend", "forecast",
    "budget", "actual", "target", "variance", "debit", "credit", "debits", "credits", "units", "quantity",
    "qty", "rate", "price", "discount", "tax", "taxes", "fees", "payments", "payment", "received",
    "billed", "delivered", "ordered", "shipped", "returned", "returns", "credit notes",
})
# A source name's field, when answer_check_context isn't there to say.
_FALLBACK_NAME_FIELDS = frozenset({
    "name", "customer", "supplier", "party", "item_code", "project", "account", "warehouse", "company",
    "cost_center", "territory", "customer_group", "supplier_group", "item_group", "against_voucher",
    "voucher_no", "sales_order", "purchase_order", "delivery_note", "parent",
})
_LINK_FIELDTYPES = frozenset({"Link", "Dynamic Link"})


def normalize(text: str) -> str:
    """A name ignoring case, spacing and punctuation."""
    return _SPACES.sub(" ", _NORM_DROP.sub(" ", text.lower())).strip()


def allowed_edits(normalized: str) -> int:
    n = len(normalized)
    return 0 if n <= 5 else 1 if n <= 12 else 2


def within(a: str, b: str, k: int) -> bool:
    """Whether ``a`` and ``b`` are at most ``k`` edits apart (banded)."""
    if abs(len(a) - len(b)) > k:
        return False
    if k == 0:
        return a == b
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        lo, hi = max(1, i - k), min(len(b), i + k)
        cur = [k + 1] * (len(b) + 1)
        cur[0] = i if i <= k else k + 1
        for j in range(lo, hi + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        if min(cur[lo - 1: hi + 1]) > k:
            return False
        prev = cur
    return prev[len(b)] <= k


@dataclass
class Name:
    """One name in a reply: where it is, what kind, and its status."""

    start: int
    end: int
    text: str
    kind: NameKind
    status: NameStatus = "unconfirmed"
    # The source name it should be, for ``near``.
    right: str | None = None

    @property
    def flagged(self) -> bool:
        return self.status in ("near", "unconfirmed")


@dataclass
class NameSources:
    """The names a turn's reply may give (``name_sources_for``)."""

    # normalized -> the source names written so
    by_norm: dict[str, set[str]] = field(default_factory=dict)
    # a normalized word -> the normalized names holding it
    by_word: dict[str, set[str]] = field(default_factory=dict)
    documents: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)
    given: str = ""
    texts: str = ""

    def add(self, raw: str) -> None:
        name = raw.strip()
        if not name or len(name) > MAX_NAME_CHARS or len(self.by_norm) >= MAX_NAMES:
            return
        norm = normalize(name)
        if not norm or norm.replace(" ", "").isdigit():
            return
        self.by_norm.setdefault(norm, set()).add(name)
        for word in norm.split():
            if len(word) >= 3:
                self.by_word.setdefault(word, set()).add(norm)
        if _DOCUMENT_RE.fullmatch(name):
            self.documents.add(name.upper())

    def plus(self, extra: Iterable[str]) -> NameSources:
        out = NameSources(
            by_norm={k: set(v) for k, v in self.by_norm.items()},
            by_word={k: set(v) for k, v in self.by_word.items()},
            documents=set(self.documents), labels=self.labels, given=self.given, texts=self.texts,
        )
        for name in extra:
            out.add(name)
        return out


def _name_fields(doctypes: dict[str, Any], doctype: object, table: str | None = None) -> frozenset[str]:
    described = doctypes.get(doctype) if isinstance(doctype, str) else None
    if table and isinstance(described, dict):
        tables = described.get("tables")
        described = tables.get(table) if isinstance(tables, dict) else None
    names = described.get("names") if isinstance(described, dict) else None
    if isinstance(names, list):
        return frozenset(n for n in names if isinstance(n, str))
    return _FALLBACK_NAME_FIELDS


def _is_name_field(key: str, fields: frozenset[str]) -> bool:
    return key in fields or key.endswith("_name")


def _rows(src: NameSources, rows: list[Any], fields: frozenset[str], doctypes: dict[str, Any], doctype: object) -> None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if isinstance(value, str) and _is_name_field(key, fields):
                src.add(value)
            elif isinstance(value, list):
                _rows(src, value, _name_fields(doctypes, doctype, key), doctypes, None)


def name_sources_for(
    results: Iterable[ErpnextResult],
    *,
    doctypes: dict[str, Any] | None = None,
    given: Iterable[str] = (),
    texts: Iterable[str] = (),
) -> NameSources:
    """Every name the turn's reply may give."""
    doctypes = doctypes or {}
    src = NameSources()
    src.labels = set(NAME_LABELS)
    for result in results:
        payload = result.payload
        if not isinstance(payload, dict):
            continue
        doctype = payload.get("doctype") or result.arguments.get("doctype")
        if isinstance(doctype, str):
            src.labels.add(doctype.lower())
        fields = _name_fields(doctypes, doctype)
        columns = payload.get("columns")
        if isinstance(columns, list):
            link_columns = {
                c["fieldname"] for c in columns
                if isinstance(c, dict) and isinstance(c.get("fieldname"), str)
                and (c.get("fieldtype") in _LINK_FIELDTYPES or c["fieldname"].endswith("_name"))
            }
            fields = fields | link_columns
            src.labels.update(
                normalize(str(c.get("label") or "")) for c in columns
                if isinstance(c, dict) and c.get("fieldname") in link_columns and c.get("label")
            )
        if isinstance(payload.get("company"), str):
            src.add(payload["company"])
        if isinstance(payload.get("name"), str):
            src.add(payload["name"])
        rows = payload.get("rows")
        if isinstance(rows, list):
            _rows(src, rows, fields, doctypes, doctype)
        record = payload.get("record")
        if isinstance(record, dict):
            _rows(src, [record], fields, doctypes, doctype)
    src.labels.discard("")
    src.given = "\n".join(t.lower() for t in given)[:300_000]
    src.texts = "\n".join(t.lower() for t in texts)[-300_000:]
    return src


def _masked(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _CODE_RE.finditer(text)] + [m.span() for m in _URL_RE.finditer(text)]


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s < end and start < e for s, e in spans)


def _clean_cell(cell: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)
    return text.strip().strip("*_`~ ").strip()


def _table_cells(text: str, labels: set[str]) -> list[tuple[int, int, str]]:
    """The cells of each table column whose header is a name label."""
    out: list[tuple[int, int, str]] = []
    lines = text.splitlines(keepends=True)
    pos = 0
    offsets = []
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].strip().startswith("|"):
            j += 1
        header = [normalize(_clean_cell(c)) for c in lines[i].strip().strip("|").split("|")]
        columns = {n for n, label in enumerate(header) if label in labels}
        for k in range(i + 1, j):
            row = lines[k]
            if set(row.strip()) <= set("|:- "):
                continue
            base = offsets[k] + row.index("|") + 1
            cells = row.strip().strip("|").split("|")
            cursor = base
            for n, cell in enumerate(cells):
                if n in columns:
                    value = _clean_cell(cell)
                    if value and value not in ("-", "—", "–") and not re.fullmatch(r"[\d\s.,%$€£¥₹()+-]+", value):
                        start = text.find(value, cursor)
                        if start != -1 and start < cursor + len(cell) + 1:
                            out.append((start, start + len(value), value))
                cursor += len(cell) + 1
        i = j
    return out


def _status(src: NameSources, name: str, *, loose: bool) -> tuple[NameStatus, str | None]:
    """What ``name`` is among the sources. ``loose`` allows a near match."""
    norm = normalize(name)
    exact = src.by_norm.get(norm)
    if exact and name.strip() in exact:
        return "confirmed", None
    lowered = name.strip().lower()
    if lowered and lowered in src.given:
        return "given", None
    if exact:
        return ("near", next(iter(exact))) if len(exact) == 1 else ("confirmed", None)
    if lowered and lowered in src.texts:
        return "from_text", None
    if not loose:
        return "unconfirmed", None
    k = allowed_edits(norm)
    close: set[str] = set()
    if k:
        for word in norm.split():
            for candidate in src.by_word.get(word, ()):
                if candidate not in close and within(norm, candidate, k):
                    close.add(candidate)
        if not close and len(norm.split()) == 1:
            for candidate in src.by_norm:
                if candidate[:1] == norm[:1] and within(norm, candidate, k):
                    close.add(candidate)
    if len(close) == 1:
        (only,) = close
        names = src.by_norm[only]
        if len(names) == 1:
            return "near", next(iter(names))
    return "unconfirmed", None


def check_names(text: str, src: NameSources) -> list[Name]:
    """Every name ``text`` gives that the rules above check, with its status.
    Prose phrases are listed only when they are near a source name."""
    masked = _masked(text)
    found: list[Name] = []
    taken: list[tuple[int, int]] = []

    def take(start: int, end: int, value: str, kind: NameKind, loose: bool) -> None:
        if len(found) >= MAX_CANDIDATES or _overlaps(masked, start, end) or _overlaps(taken, start, end):
            return
        status, right = _status(src, value, loose=loose)
        taken.append((start, end))
        found.append(Name(start, end, value, kind, status, right))

    for m in _DOCUMENT_RE.finditer(text):
        # Exact or nothing: PO-0012 is never corrected to PO-0021.
        take(m.start(), m.end(), m.group(0), "document", loose=False)
    for start, end, value in _table_cells(text, src.labels):
        take(start, end, value, "cell", loose=True)
    for m in _BOLD_ITEM_RE.finditer(text):
        value = m.group(1).strip()
        words = normalize(value).split()
        if value.endswith(":") or not words or all(w in _LABEL_WORDS for w in words):
            continue
        if not re.search(r"\d", m.group(2)):
            continue
        start = m.start(1) + (len(m.group(1)) - len(m.group(1).lstrip()))
        take(start, start + len(value), value, "bold", loose=True)
    for m in _PHRASE_RE.finditer(text):
        _phrase(text, m, src, masked, taken, found)
    found.sort(key=lambda n: n.start)
    return found


def _phrase(
    text: str, m: re.Match[str], src: NameSources, masked: list[tuple[int, int]],
    taken: list[tuple[int, int]], found: list[Name],
) -> None:
    """A run of capitalised words: listed when it, or the run less its first
    word (a sentence's opening word), is near a source name."""
    start, end = m.span()
    if _overlaps(masked, start, end) or _overlaps(taken, start, end):
        return
    tokens = [(t.start() + start, t.end() + start) for t in re.finditer(r"\S+", m.group(0))]
    for a in range(min(2, len(tokens))):
        s, e = tokens[a][0], tokens[-1][1]
        value = text[s:e].rstrip(".,;:'’")
        e = s + len(value)
        norm = normalize(value)
        if len(norm) < 6 or (len(tokens) - a == 1 and len(norm) < 6):
            continue
        status, right = _status(src, value, loose=True)
        if status == "confirmed" or status in ("given", "from_text"):
            taken.append((s, e))
            return
        if status == "near" and len(found) < MAX_CANDIDATES:
            taken.append((s, e))
            found.append(Name(s, e, value, "phrase", "near", right))
            return
