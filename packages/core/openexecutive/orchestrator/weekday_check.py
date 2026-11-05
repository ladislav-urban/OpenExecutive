"""A weekday a reply names beside a date, made to fit it (the answer check's
code-only last step; the pilot's Nov 5, 2026 second retake: "Northgate's
quotation expires Saturday", for Nov 8, a Sunday).

Within one sentence, list item or table row (``answer_correct.units``) that
holds exactly one date and exactly one weekday, the weekday is the date's own:
written in place, in the reply's own form ("Sat", "Saturday", "saturday").
A date without a year is read in the year that puts it nearest ``today``, as a
reply about open documents means it. More than one date or weekday in a unit
is left alone: which goes with which isn't certain."""
from __future__ import annotations

import re
from datetime import date

from openexecutive.orchestrator import answer_correct

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
_DATE_RES = (
    re.compile(_MONTH + r"[ \t]+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?[ \t]+(?P<year>20\d\d))?(?![\d])", re.I),
    re.compile(r"(?<![\d])(?P<day>\d{1,2})(?:st|nd|rd|th)?[ \t]+" + _MONTH + r"(?:,?[ \t]+(?P<year>20\d\d))?", re.I),
    re.compile(r"(?<![\d-])(?P<year>20\d\d)-(?P<num>\d{2})-(?P<day>\d{2})(?![\d])"),
)
_WEEKDAY_RE = re.compile(
    r"\b(?P<day>Mon(?:day)?|Tue(?:s(?:day)?)?|Wed(?:nesday)?|Thu(?:r(?:s(?:day)?)?)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\b",
    re.I,
)


def _dates(text: str, today: date) -> list[tuple[int, int, date]]:
    found: list[tuple[int, int, date]] = []
    for pattern in _DATE_RES:
        for m in pattern.finditer(text):
            if any(s < m.end() and m.start() < e for s, e, _ in found):
                continue
            groups = m.groupdict()
            month = int(groups["num"]) if groups.get("num") else _MONTHS.get(groups["mon"].lower()[:3])
            if month is None:
                continue
            day = int(groups["day"])
            years = [int(groups["year"])] if groups.get("year") else [today.year - 1, today.year, today.year + 1]
            candidates = []
            for year in years:
                try:
                    candidates.append(date(year, month, day))
                except ValueError:
                    continue
            if candidates:
                found.append((m.start(), m.end(), min(candidates, key=lambda d: abs((d - today).days))))
    return found


def _written_like(original: str, right: str) -> str:
    word = right if len(original) > 3 or original.lower() in _DAYS else right[:3]
    if original.isupper() and len(original) > 1:
        return word.upper()
    return word.capitalize() if original[:1].isupper() else word


def fix(text: str, today: date) -> tuple[str, int]:
    """``text`` with each misnamed weekday put right, and how many were."""
    edits: list[tuple[int, int, str]] = []
    for unit in answer_correct.units(text):
        part = text[unit.start:unit.end]
        dates = _dates(part, today)
        weekdays = [
            m for m in _WEEKDAY_RE.finditer(part)
            if not any(s <= m.start() < e for s, e, _ in dates)
            # "Mar" and "Sun" alone are words too; only a whole weekday or a
            # capitalised abbreviation counts.
            and (len(m.group("day")) > 3 or m.group("day")[:1].isupper())
            and m.group("day").lower() not in ("mar", "may")
        ]
        if len(dates) != 1 or len(weekdays) != 1:
            continue
        right = _DAYS[dates[0][2].weekday()]
        m = weekdays[0]
        if not right.startswith(m.group("day").lower()[:3]):
            edits.append((unit.start + m.start(), unit.start + m.end(), _written_like(m.group("day"), right)))
    out = text
    for start, end, new in sorted(set(edits), reverse=True):
        out = out[:start] + new + out[end:]
    return out, len(set(edits))
