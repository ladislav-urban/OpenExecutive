"""A weekday a reply names beside a date, made to fit it (the answer check's
code-only last step; the pilot's Nov 5, 2026 second retake: "Northgate's
quotation, valid till Nov 8, expires Saturday", for a Sunday).

Only where the weekday plainly is that date's: in one clause (a line split at
``.;:!?``, never inside code or a link) holding exactly one date, a full
weekday name within four words of it ("Nov 8, expires Saturday",
"Saturday, Nov 8"), or an abbreviation right beside it ("Sat, Nov 8",
"Nov 8 (Sat)"). A weekday followed by a capitalised word is a name ("Sun
Pharma", "Thu Nguyen") and is left alone, as is any clause with a second date
or weekday. A date without a year is read in the year that puts it nearest
``today``. Security review of ladislav-urban/OpenExecutive#165."""
from __future__ import annotations

import re
from datetime import date

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = (
    r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b"
)
_DATE_RES = (
    re.compile(_MONTH + r"[ \t]+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?[ \t]+(?P<year>20\d\d))?(?![\d])", re.I),
    re.compile(r"(?<![\d])(?P<day>\d{1,2})(?:st|nd|rd|th)?[ \t]+" + _MONTH + r"(?:,?[ \t]+(?P<year>20\d\d))?", re.I),
    re.compile(r"(?<![\d-])(?P<year>20\d\d)-(?P<num>\d{2})-(?P<day>\d{2})(?![\d])"),
)
_WEEKDAY_RE = re.compile(
    r"\b(?P<day>(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", re.I,
)
# Code spans, fenced code and links: never edited.
_SKIP_RE = re.compile(
    r"```.*?(?:```|\Z)|`[^`\n]{0,500}`|https?://\S{0,2000}|\[[^\]\n]{0,200}\]\([^)\s]{0,2000}\)", re.S
)
_CLAUSE_RE = re.compile(r"[^.;:!?\n]+")
# The only words that may stand between a date and its weekday: the date's
# own ("valid till Nov 8, expires Saturday", "Nov 8, which is a Sunday").
# Any other ("chase on Friday", "slipped to Friday") names another day's event.
_GAP_WORDS = frozenset({
    "expires", "expiring", "expire", "ends", "ending", "falls", "lands", "is", "was", "a", "which",
    "that", "it", "that's", "it's", "thats", "its", "being", "due",
})


def _dates(clause: str, today: date) -> list[tuple[int, int, date]]:
    found: list[tuple[int, int, date]] = []
    for pattern in _DATE_RES:
        for m in pattern.finditer(clause):
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
                if len(found) > 1:
                    # Two dates: the clause is left alone, so stop reading it.
                    return found
    return found


def _written_like(original: str, right: str) -> str:
    word = right if len(original) > 3 else right[:3]
    if original.isupper() and len(original) > 1:
        return word.upper()
    return word.capitalize() if original[:1].isupper() else word


def _beside(clause: str, day: re.Match[str], start: int, end: int) -> bool:
    """Whether the weekday ``day`` plainly names the date at ``start:end``."""
    gap = clause[end:day.start()] if day.start() >= end else clause[day.end():start]
    if any(c.isdigit() for c in gap):
        return False
    if len(day.group("day")) <= 3:
        # An abbreviation only right beside the date: "Sat, Nov 8", "Nov 8 (Sat)".
        return re.fullmatch(r"[ \t]*[,(]?[ \t]*|[ \t]*\)?[ \t]*,?[ \t]*", gap) is not None
    words = re.findall(r"[A-Za-z'’]+", gap)
    return len(words) <= 4 and all(w.lower().replace("’", "'") in _GAP_WORDS for w in words)


def fix(text: str, today: date) -> tuple[str, int]:
    """``text`` with each misnamed weekday put right, and how many were."""
    skipped = [m.span() for m in _SKIP_RE.finditer(text)]
    edits: list[tuple[int, int, str]] = []
    for clause_m in _CLAUSE_RE.finditer(text):
        c0 = clause_m.start()
        if any(s < clause_m.end() and c0 < e for s, e in skipped):
            continue
        clause = clause_m.group(0)
        dates = _dates(clause, today)
        days = [
            m for m in _WEEKDAY_RE.finditer(clause)
            if not any(s <= m.start() < e for s, e, _ in dates)
            and (len(m.group("day")) > 3 or m.group("day")[:1].isupper())
            # "Sun Pharma", "Thu Nguyen", "Mon Health": a name, not a day
            # (a capitalised word after it that isn't the date's month).
            and not re.match(r"[ \t]+(?!" + _MONTH + r")[A-Z]", clause[m.end():m.end() + 12])
        ]
        if len(dates) != 1 or len(days) != 1:
            continue
        start, end, when = dates[0]
        day = days[0]
        if not _beside(clause, day, start, end):
            continue
        right = _DAYS[when.weekday()]
        if not right.startswith(day.group("day").lower()[:3]):
            edits.append((c0 + day.start(), c0 + day.end(), _written_like(day.group("day"), right)))
    out = text
    for start, end, new in sorted(edits, reverse=True):
        out = out[:start] + new + out[end:]
    return out, len(edits)
