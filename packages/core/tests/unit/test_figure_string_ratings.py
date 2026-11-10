"""Ratings ERPNext gives as strings ("4"), from a child table's dotted fields
(``parameters.rating``), are numbers to average (a demo reshoot). Made-up
names and figures."""

from __future__ import annotations

from openexecutive.orchestrator import answer_review
from openexecutive.orchestrator.answer_review import Review
from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    NumberFormat,
    ResultTypes,
    check_figures,
    find_figures,
    sources_for,
)

FMT = NumberFormat.parse(None)

# ---------------------------------------------------------------------------
# Ratings ERPNext gives as strings, from a child table's dotted fields

FEEDBACK = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Quality Feedback", "fields": ["name", "document_name", "parameters.parameter", "parameters.rating"]},
    {
        "doctype": "Quality Feedback",
        "rows": [
            {"name": f"FB-{n}", "document_name": "Juniper Hall", "parameter": parameter, "rating": rating}
            for n, (parameter, rating) in enumerate([
                ("Kept to the schedule", "4"), ("Kept to the schedule", "5"),
                ("Kept to the schedule", "4"), ("Kept to the schedule", "6"),
                ("Left the site tidy", "3"), ("Left the site tidy", "3"),
                ("Left the site tidy", "4"), ("Left the site tidy", "3"),
            ])
        ],
    },
)


def test_string_ratings_average_per_question() -> None:
    src = sources_for([FEEDBACK], types=ResultTypes({"Quality Feedback": {"numeric": []}}), fmt=FMT)
    text = "Kept to the schedule averages 4.75 and Left the site tidy 3.25."
    got = {f.text: f.status for f in check_figures(text, src, FMT)}
    assert got == {"4.75": "confirmed", "3.25": "confirmed"}


def test_a_dotted_child_field_reads_as_the_child_tables_numeric_field() -> None:
    result = ErpnextResult(
        "erpnext__list_records",
        {"doctype": "Delivery Trip", "fields": ["name", "stops.distance"]},
        {"doctype": "Delivery Trip", "rows": [{"name": "T-1", "distance": "12.5"}, {"name": "T-2", "distance": "30"}]},
    )
    types = ResultTypes({"Delivery Trip": {"numeric": [], "tables": {"stops": {"numeric": ["distance"]}}}})
    src = sources_for([result], types=types, fmt=FMT)
    (fig,) = check_figures("The stops add up to 42.5 km.", src, FMT)
    assert fig.status == "confirmed"


def test_a_reviewer_may_name_a_rating_as_erpnext_wrote_it() -> None:
    src = sources_for([FEEDBACK], fmt=FMT)
    (fig,) = find_figures("Schedule averaged 4.5 overall.")
    got = answer_review.read_verdict({"verdict": "wrong", "op": "average", "operands": ["4", "5", "4", "6"]}, fig, src)
    assert got == Review("wrong", 4.75)
