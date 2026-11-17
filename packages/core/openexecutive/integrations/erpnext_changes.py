"""Cards that change an existing unsubmitted ERPNext document, or add a
comment to one (phase 0's S3, OpenExecutive's half; oe_connector's
draft_update and draft_comment, ladislav-urban/oe_connector#511).

They ride the draft cards' path (``integrations.erpnext_drafts``): the
person asks in their own words, ERPNext's ``draft_check`` reads the change
as them and fingerprints it, the card shows ERPNext's own before and after,
and approval sends exactly the stored change with that fingerprint, as the
same person. This module holds what is particular to them:

* **What may change.** Each operation is off until its setting lists a
  DocType (``ERPNEXT_DRAFT_UPDATE_DOCTYPES``, ``ERPNEXT_DRAFT_COMMENT_DOCTYPES``),
  intersected with this app's own allowlist: Purchase Invoice for updates
  (``UPDATE_FIELDS``, mirroring oe_connector's UpdateType, which checks
  every value again), and transactional DocTypes for comments, never a
  sensitive one.
* **The card shows everything approval sends.** Every change, line change
  and removed row, and the comment's whole text, from ERPNext's summary,
  with this app's own labels. A summary that names another document or
  operation, an unknown owner, or more changes than allowed leaves no card.
* **Success by operation.** An update made is ``docstatus`` 0; a comment
  made names its Comment; a repeat of the card's key echoes the target with
  ``already_done``.
"""
from __future__ import annotations

import html
import re
from typing import Any

from openexecutive.config import get_settings

# "credit" is a credit-limit raise (integrations.erpnext_credit): only ever
# left from a credit-hold alert's tap, never proposed by the model, so
# ``enabled("credit")`` lists nothing and ``check_change`` refuses it.
# "block" is ERPNext's Block Invoice on a submitted supplier invoice
# (integrations.erpnext_block): only ever left by the payment-checks job's
# code, so ``enabled("block")`` lists nothing either, and chat and ERPNext's
# panel can never propose one. "match" reconciles a bank line with its
# vouchers (integrations.erpnext_match): only ever left by the bank-matching
# job's code, and ``enabled("match")`` lists nothing too. "fill" fills a
# supplier's mailed draft Purchase Invoice from its order or receipt
# (integrations.erpnext_fill): only ever left by the invoice-capture job's
# code, so ``enabled("fill")`` lists nothing as well.
OPERATIONS = ("create", "update", "comment", "credit", "block", "match", "fill")
OPERATION_TOOLS: dict[str, str] = {
    "update": "erpnext__draft_update",
    "comment": "erpnext__draft_comment",
    "credit": "erpnext__draft_update",
    "block": "erpnext__draft_block_invoice",
    "match": "erpnext__draft_match_bank_line",
    "fill": "erpnext__draft_fill_purchase_invoice",
}
MAX_CHANGES = 40
COMMENT_MAX = 2000
TEXT_SHOWN = 1000

# oe_connector's UpdateType for Purchase Invoice (draft_changes.UPDATE_TYPES):
# the header and line fields an update may set, with this app's own labels.
UPDATE_FIELDS: dict[str, dict[str, str]] = {
    "Purchase Invoice": {
        "bill_no": "Supplier invoice no.",
        "bill_date": "Supplier invoice date",
        "due_date": "Due date",
        "posting_date": "Posting date",
        "set_posting_time": "Edit posting date and time",
        "remarks": "Remarks",
        "cost_center": "Cost center",
        "project": "Project",
        "on_hold": "On hold",
        "release_date": "Release date",
        "hold_comment": "Hold comment",
    },
}
# Fields oe_connector's UpdateType lists that a card here never changes, and
# what the person is told. A Purchase Invoice's supplier says who is owed:
# oe_connector sets it only on an empty intake draft, and no card here moves
# an invoice to another supplier.
NEVER_CHANGED: dict[str, dict[str, str]] = {
    "Purchase Invoice": {
        "supplier": ("A Purchase Invoice's supplier can't be changed on a card. "
                     "If it is wrong, change it in the ERP itself."),
    },
}
UPDATE_LINE_FIELDS: dict[str, dict[str, dict[str, str]]] = {
    "Purchase Invoice": {
        "items": {
            "qty": "Qty",
            "rate": "Rate",
            "uom": "UOM",
            "expense_account": "Expense account",
            "cost_center": "Cost center",
            "project": "Project",
            "description": "Description",
        },
    },
}
REMOVABLE_TABLES: dict[str, frozenset[str]] = {"Purchase Invoice": frozenset({"items"})}
_DATE_FIELDS = frozenset({"bill_date", "due_date", "posting_date", "release_date"})
_FLAG_FIELDS = frozenset({"set_posting_time", "on_hold"})
_NUMBER_FIELDS = {"qty": 1_000_000, "rate": 1_000_000_000}
_TEXT_CAPS = {"bill_no": 140, "remarks": 1000, "hold_comment": 1000, "description": 1000}

# The DocTypes a comment may go on: transactional records people discuss.
COMMENT_ALLOWED: frozenset[str] = frozenset({
    "Purchase Invoice", "Sales Invoice", "Purchase Order", "Sales Order", "Quotation",
    "Material Request", "Delivery Note", "Purchase Receipt", "Customer", "Supplier",
    "Item", "Project", "Task", "ToDo", "Dunning",
})
# oe_connector's SENSITIVE_DOCTYPES (mcp/common.py), mirrored: never commented
# on. The allowlist above names none of them; a test keeps it so.
SENSITIVE_DOCTYPES: frozenset[str] = frozenset({
    "Employee", "Employee Checkin", "Employee Group", "Attendance", "Leave Application",
    "Salary Slip", "Salary Structure", "Salary Structure Assignment", "Payroll Entry",
    "Additional Salary", "Employee Advance", "Expense Claim", "Sales Person", "Driver",
    "Bank Account", "Bank Guarantee", "Bank Transaction", "Bank Statement Import", "Plaid Settings",
    "Contact", "Lead", "Prospect", "User", "Call Log",
})
_WHOSE = frozenset({"yours", "assigned to you"})
# A row name as ERPNext gives one (a hash, or a name with dashes).
_ROW_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,139}")
# A document's name: ERPNext's own rule (any text up to 140 characters), with
# no control characters and no leading or trailing space ("Smith & Sons",
# "Müller GmbH", "ACC/2026/001").
_DOC_RE = re.compile(r"[^\x00-\x1f\x7f-\x9f\s](?:[^\x00-\x1f\x7f-\x9f]{0,138}[^\x00-\x1f\x7f-\x9f\s])?")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# oe_connector's _CONTROL: control characters, tab included, newline kept.
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def update_doctypes() -> tuple[str, ...]:
    listed = set(get_settings().erpnext_draft_update_doctypes)
    return tuple(d for d in sorted(UPDATE_FIELDS) if d in listed)


def comment_doctypes() -> tuple[str, ...]:
    listed = set(get_settings().erpnext_draft_comment_doctypes)
    return tuple(d for d in sorted(COMMENT_ALLOWED - SENSITIVE_DOCTYPES) if d in listed)


def enabled(operation: str) -> tuple[str, ...]:
    """The DocTypes a person may have a card of ``operation`` proposed on:
    none for "credit", "block", "match" and "fill", which only code leaves."""
    if operation == "update":
        return update_doctypes()
    if operation == "comment":
        return comment_doctypes()
    return ()


def _as_shown(value: str) -> str:
    """Text as the card shows it and as it is stored and sent: made plain
    (``prompt_blocks.plain``: compatibility forms folded by NFKC, and
    invisible, format and bidi characters dropped), control characters out
    (newlines kept), trimmed. The card shows exactly this, so nothing hidden
    reaches ERPNext that the approver didn't see: a right-to-left override
    or a zero-width space that the card would drop is never written either."""
    from openexecutive.utils.prompt_blocks import plain

    # Control characters out first, then made plain: plain() folds last, so
    # a tab deleted after it would leave a letter and a combining mark that
    # compose only when the value is checked again at approval.
    return plain(_CONTROL.sub("", value)).strip()


def comment_text(value: Any) -> str:
    """A comment's text as it is shown, stored and sent (``_as_shown``).
    Raises ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError("`text` is the comment, as plain text.")
    cleaned = _as_shown(value)
    if not cleaned:
        raise ValueError("`text` is empty.")
    if len(cleaned) > COMMENT_MAX:
        raise ValueError(f"`text` is longer than {COMMENT_MAX} characters.")
    return cleaned


def _value(field: str, value: Any, where: str) -> Any:
    if field in _FLAG_FIELDS:
        if value in (0, 1, True, False) and not isinstance(value, str):
            return int(value)
        raise ValueError(f"`{where}` is 0 or 1.")
    if field in _DATE_FIELDS:
        if isinstance(value, str) and _DATE_RE.fullmatch(value):
            return value
        raise ValueError(f"`{where}` is a date, YYYY-MM-DD.")
    if field in _NUMBER_FIELDS:
        if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= _NUMBER_FIELDS[field]:
            return value
        raise ValueError(f"`{where}` is a number from 0 to {_NUMBER_FIELDS[field]:,}.")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{where}` is text.")
    cap = _TEXT_CAPS.get(field, 140)
    text = _as_shown(value)
    if field not in _TEXT_CAPS:
        # A link (cost center, project, unit, account) is shown on one line:
        # stored and sent that way too, so two spaces or a line break can't
        # name a look-alike record the card doesn't show.
        text = " ".join(text.split())
    if not text:
        raise ValueError(f"`{where}` is text.")
    if len(text) > cap:
        raise ValueError(f"`{where}` is longer than {cap} characters.")
    return text


def check_change(operation: str, doctype: Any, name: Any, fields: Any) -> tuple[str, dict[str, Any]] | str:
    """``(name, fields)`` as they will be stored and sent for an update or a
    comment, or why they can't be. Shape and kind only: oe_connector checks
    the document, the person's right to change it and every value again."""
    if operation not in ("update", "comment"):
        # A credit-limit raise is never asked for this way: only an alert's tap leaves one.
        return "`operation` must be create, update or comment."
    allowed = enabled(operation)
    if doctype not in allowed:
        what = "changed" if operation == "update" else "commented on"
        return (f"Nothing can be {what} here." if not allowed
                else f"`doctype` must be one of {', '.join(allowed)} to be {what}.")
    if not isinstance(name, str) or not _DOC_RE.fullmatch(name.strip()) or _as_shown(name) != name.strip():
        # Never a name with a hidden or folded character (a bidi override, a
        # zero-width space): it would read as another document's on the card.
        return "`name` is the document's name, as ERPNext shows it."
    if not isinstance(fields, dict):
        return "`fields` must be an object."
    try:
        if operation == "comment":
            if set(fields) != {"text"}:
                return "A comment's fields are {text}."
            return name.strip(), {"text": comment_text(fields["text"])}
        return name.strip(), _update_fields(str(doctype), fields)
    except ValueError as exc:
        return str(exc)


def _update_fields(doctype: str, fields: dict[str, Any]) -> dict[str, Any]:
    if not fields or set(fields) - {"set", "lines", "remove_lines"}:
        raise ValueError("An update's fields are {set, lines, remove_lines}.")
    out: dict[str, Any] = {}
    count = 0
    header = fields.get("set")
    if header is not None:
        if not isinstance(header, dict):
            raise ValueError("`set` is {field: value}.")
        shown = UPDATE_FIELDS[doctype]
        never = NEVER_CHANGED.get(doctype, {})
        clean: dict[str, Any] = {}
        for field, value in header.items():
            if field in never:
                raise ValueError(never[field])
            if field not in shown:
                raise ValueError(f"`{field}` can't be changed on a {doctype}.")
            clean[field] = _value(field, value, field)
        count += len(clean)
        if clean:
            out["set"] = clean
    lines = fields.get("lines")
    if lines is not None:
        if not isinstance(lines, dict):
            raise ValueError("`lines` is {table: {row name: {field: value}}}.")
        tables = UPDATE_LINE_FIELDS[doctype]
        clean_lines: dict[str, dict[str, dict[str, Any]]] = {}
        for table, rows in lines.items():
            if table not in tables or not isinstance(rows, dict):
                raise ValueError(f"`{table}` isn't a table that can be changed on a {doctype}.")
            for row, row_fields in rows.items():
                if not isinstance(row, str) or not _ROW_RE.fullmatch(row) or not isinstance(row_fields, dict):
                    raise ValueError(f"`{table}` rows are named as ERPNext names them.")
                kept = {}
                for field, value in row_fields.items():
                    if field not in tables[table]:
                        raise ValueError(f"`{field}` can't be changed on a {table} row.")
                    kept[field] = _value(field, value, f"{table} {row} {field}")
                if kept:
                    clean_lines.setdefault(table, {})[row] = kept
                    count += len(kept)
        if clean_lines:
            out["lines"] = clean_lines
    remove = fields.get("remove_lines")
    if remove is not None:
        if not isinstance(remove, dict):
            raise ValueError("`remove_lines` is {table: [row name]}.")
        clean_remove: dict[str, list[str]] = {}
        for table, rows in remove.items():
            if table not in REMOVABLE_TABLES.get(doctype, frozenset()) or not isinstance(rows, list):
                raise ValueError(f"Rows of `{table}` can't be removed on a {doctype}.")
            names = []
            for row in rows:
                if not isinstance(row, str) or not _ROW_RE.fullmatch(row):
                    raise ValueError(f"`{table}` rows are named as ERPNext names them.")
                if row not in names:
                    names.append(row)
            if names:
                clean_remove[table] = names
                count += len(names)
        if clean_remove:
            out["remove_lines"] = clean_remove
    if not out:
        raise ValueError("Say what to change.")
    if count > MAX_CHANGES:
        raise ValueError(f"At most {MAX_CHANGES} changes on one card.")
    return out


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

def _shown(value: Any) -> str:
    from openexecutive.integrations.erpnext_drafts import _shown as shown

    return shown(value)


def _text_whole(value: Any) -> str:
    """A text value as the card shows it: whole, newlines kept, made plain
    (nothing hidden, ``_as_shown``), never cut. ``summary_refusal`` leaves no
    card with a value longer than TEXT_SHOWN, so nothing here needs cutting."""
    return _as_shown(str(value if value is not None else ""))


def _change_shown(field: str, before: Any, after: Any) -> str:
    """One change's before → after. A text field's values are whole; one
    over several lines puts the arrow on its own line."""
    if field not in _TEXT_CAPS:
        return f"{_shown(before)} → {_shown(after)}"
    old, new = _text_whole(before), _text_whole(after)
    return f"{old}\n→\n{new}" if "\n" in old or "\n" in new else f"{old} → {new}"


def _too_long(value: Any) -> bool:
    # Any value, not only text: a card shows str() of whatever came back.
    return value is not None and len(_text_whole(value)) > TEXT_SHOWN


def _date_of(value: Any) -> str | None:
    """A date as YYYY-MM-DD, from ERPNext's date or midnight datetime text."""
    if not isinstance(value, str):
        return None
    found = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ T]00:00:00(?:\.0+)?)?", value.strip())
    return found.group(1) if found else None


def _same(field: str, reported: Any, stored: Any) -> bool:
    """Whether ERPNext's after-value is the value stored to send, once dates
    and numbers are put in one form (12 is 12.0; a date is its day). A line's
    description comes back HTML-escaped, as oe_connector stores it."""
    if field in _FLAG_FIELDS or field in _NUMBER_FIELDS:
        if isinstance(reported, bool) or not isinstance(reported, int | float):
            return False
        return float(reported) == float(stored)
    if field in _DATE_FIELDS:
        return _date_of(reported) is not None and _date_of(reported) == _date_of(stored)
    if not isinstance(reported, str):
        return False
    if field == "description":
        reported = html.unescape(reported)
    return reported.strip() == stored


def _exact_changes(reported: Any, asked: dict[str, Any]) -> bool:
    """Every asked field reported once, each with the stored value after."""
    if not isinstance(reported, list) or not all(isinstance(c, dict) for c in reported):
        return False
    fields = [c.get("field") for c in reported]
    if len(fields) != len(set(fields)) or set(fields) != set(asked):
        return False
    return all(_same(str(c["field"]), c.get("after"), asked[str(c["field"])]) for c in reported)


def summary_refusal(operation: str, doctype: str, name: str, fields: dict[str, Any], checked: dict[str, Any]) -> str | None:
    """Why ERPNext's check can't become a card, or None: it must read back
    the same operation and document, a known owner, no more changes than
    allowed, and for a comment exactly the stored text. An update's reading
    must match what was asked exactly: every asked change reported, each
    after-value the one stored to send, removed rows only ones asked to be
    removed, and every text value short enough to show whole."""
    summary = checked.get("summary")
    if not isinstance(summary, dict):
        return "ERPNext didn't say what it would change, so no card was left."
    if (summary.get("operation"), summary.get("doctype"), summary.get("name")) != (operation, doctype, name):
        return "ERPNext read a different document or change, so no card was left. Ask again."
    if operation == "comment":
        if summary.get("text") != fields.get("text"):
            return "ERPNext read the comment differently, so no card was left. Ask again."
        return None
    if summary.get("whose") not in _WHOSE:
        return "ERPNext didn't say whose this document is, so no card was left."
    changes, lines, removed = summary.get("changes"), summary.get("lines"), summary.get("removed")
    if not (isinstance(changes, list) and isinstance(lines, list) and isinstance(removed, list)):
        return "ERPNext's reading of the change was incomplete, so no card was left."
    sent = fields.get("set") or {}
    if any(not isinstance(c, dict) or c.get("field") not in sent for c in changes):
        return "ERPNext's reading names a change that wasn't asked for, so no card was left."
    sent_lines = {
        (table, row): row_fields
        for table, rows in (fields.get("lines") or {}).items() for row, row_fields in rows.items()
    }
    seen: set[tuple[Any, Any]] = set()
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("changes") or [], list):
            return "ERPNext's reading of the change was incomplete, so no card was left."
        key = (line.get("table"), line.get("row"))
        asked = sent_lines.get(key, {})
        if any(not isinstance(c, dict) or c.get("field") not in asked for c in line.get("changes") or []):
            return "ERPNext's reading names a change that wasn't asked for, so no card was left."
        if key in seen or not _exact_changes(line.get("changes") or [], asked):
            return _DIFFERENT
        seen.add(key)
    if not _exact_changes(changes, sent) or seen != set(sent_lines):
        return _DIFFERENT
    may_remove = {(table, row) for table, rows in (fields.get("remove_lines") or {}).items() for row in rows}
    if any(not isinstance(r, dict) or (r.get("table"), r.get("row")) not in may_remove for r in removed):
        return "ERPNext's reading removes a row that wasn't asked to be removed, so no card was left."
    # Every asked removal read back, once: the card shows what approval sends.
    removed_keys = [(r.get("table"), r.get("row")) for r in removed]
    if len(set(removed_keys)) != len(removed_keys) or set(removed_keys) != may_remove:
        return _DIFFERENT
    line_changes = sum(len(x.get("changes") or []) for x in lines)
    total = len(changes) + line_changes + len(removed)
    if total == 0 or total > MAX_CHANGES:
        return "ERPNext's reading of the change can't be shown on one card, so none was left."
    values = [v for c in [*changes, *(c for x in lines for c in x.get("changes") or [])]
              for v in (c.get("before"), c.get("after"))]
    if any(_too_long(v) for v in values):
        return (f"A value in this change is longer than {TEXT_SHOWN} characters, so it can't be shown "
                "whole on a card and none was left.")
    return None


_DIFFERENT = ("ERPNext read the change differently from what was asked (a value left out, perhaps because "
              "it is already so, or read as another value), so no card was left. Ask again with only the "
              "values that change.")


def card_view(operation: str, doctype: str, name: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Everything approval sends, as ERPNext reads it: every change, line
    change and removed row, the totals, or the comment's whole text. A text
    field's before and after are whole, newlines kept, never cut: approval
    writes all of it (``summary_refusal`` leaves no card it can't show)."""
    shown: list[dict[str, str]] = [{"label": "Document", "value": f"{doctype} {name}"}]
    notes: list[str] = []
    if operation == "comment":
        # Whole and as stored, newlines kept: the card shows what is written.
        shown.append({"label": "Comment", "value": str(summary.get("text") or "")})
        return {"fields": shown, "lines": [], "notes": notes}
    shown.append({"label": "Whose", "value": str(summary.get("whose"))})
    if isinstance(summary.get("workflow_state"), str):
        shown.append({"label": "Workflow state", "value": _shown(summary["workflow_state"])})
    labels = UPDATE_FIELDS.get(doctype, {})
    for change in summary.get("changes") or []:
        if isinstance(change, dict):
            label = labels.get(str(change.get("field")), str(change.get("field")))
            shown.append({"label": label,
                          "value": _change_shown(str(change.get("field")), change.get("before"), change.get("after"))})
    line_labels = UPDATE_LINE_FIELDS.get(doctype, {})
    for line in summary.get("lines") or []:
        if not isinstance(line, dict):
            continue
        table_labels = line_labels.get(str(line.get("table")), {})
        where = f"Row {_shown(line.get('idx'))}" + (f" ({_shown(line.get('item_code'))})" if line.get("item_code") else "")
        for change in line.get("changes") or []:
            if isinstance(change, dict):
                label = table_labels.get(str(change.get("field")), str(change.get("field")))
                shown.append({"label": f"{where}: {label}",
                              "value": _change_shown(str(change.get("field")), change.get("before"), change.get("after"))})
    for row in summary.get("removed") or []:
        if isinstance(row, dict):
            what = f"Row {_shown(row.get('idx'))}" + (f" ({_shown(row.get('item_code'))})" if row.get("item_code") else "")
            shown.append({"label": "Removed", "value": f"{what}, qty {_shown(row.get('qty'))}, amount {_shown(row.get('amount'))}"})
    before, after = summary.get("totals_before"), summary.get("totals_after")
    if isinstance(before, dict) and isinstance(after, dict):
        currency = summary.get("currency") or ""
        company = summary.get("company_currency") or ""
        for key, label, code in (("grand_total", "Grand total", currency), ("base_grand_total", "Grand total (company currency)", company)):
            if key in before or key in after:
                shown.append({"label": label,
                              "value": f"{_shown(before.get(key))} → {_shown(after.get(key))} {_shown(code)}".strip()})
    return {"fields": shown, "lines": [], "notes": notes}


def made(operation: str, doctype: str, name: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """What approval made, from oe_connector's result (draft_changes._done),
    or None when it isn't a success: ``ok`` with the same operation and
    document, and then a fresh update's ``docstatus`` 0, a fresh comment's
    Comment name, or a repeat of the card's key (``already_done``)."""
    if result.get("ok") is not True:
        return None
    if (result.get("operation"), result.get("doctype"), result.get("name")) != (operation, doctype, name):
        return None
    repeat = result.get("already_done") is True
    if not repeat:
        if operation == "update" and result.get("docstatus") != 0:
            return None
        if operation == "comment" and not (isinstance(result.get("comment"), str) and result["comment"]):
            return None
    out: dict[str, Any] = {"doctype": doctype, "name": name, "operation": operation, "already_created": repeat}
    if operation == "comment" and isinstance(result.get("comment"), str):
        out["comment"] = result["comment"][:140]
    return out
