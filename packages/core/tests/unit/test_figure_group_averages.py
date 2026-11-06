"""Averages per group, ratings out of five, and child tables across records
(the pilot's Nov 6, 2026 retake, quality-2: three of four per-question
averages over four feedback records came out "—", because the check held no
group averages and read ERPNext's ratings only as fractions). Made-up
customers."""
from __future__ import annotations

from openexecutive.orchestrator.figure_check import (
    ErpnextResult,
    NumberFormat,
    check_figures,
    sources_for,
)

FEEDBACK = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Quality Feedback"},
    {
        "doctype": "Quality Feedback",
        "rows": [
            {"name": f"QF-{n}", "customer": c, "parameters": [
                {"parameter": "Quality of the work", "rating": q},
                {"parameter": "Kept to the schedule", "rating": s},
            ]}
            for n, (c, q, s) in enumerate(
                [("Alder", 1.0, 0.8), ("Birch", 1.0, 1.0), ("Cedar", 0.8, 0.4), ("Dune", 1.0, 1.0)]
            )
        ],
        "truncated": False,
    },
)

TABLE = "| Point | Average |\n|---|---|\n"


def _statuses(text: str) -> dict[str, str]:
    fmt = NumberFormat.parse(None)
    return {f.text: f.status for f in check_figures(text, sources_for([FEEDBACK], fmt=fmt), fmt)}


def test_a_group_average_on_the_five_star_scale_is_confirmed() -> None:
    # Quality: (5 + 5 + 4 + 5) / 4 = 4.75; schedule: (4 + 5 + 2 + 5) / 4 = 4.0.
    got = _statuses(TABLE + "| Quality of the work | 4.75 |\n| Kept to the schedule | 4.0 |\n")
    assert got == {"4.75": "confirmed", "4.0": "confirmed"}


def test_a_wrong_average_still_fails() -> None:
    assert _statuses(TABLE + "| Quality of the work | 3.9 |\n") == {"3.9": "unconfirmed"}


def test_a_single_rating_out_of_five_is_confirmed() -> None:
    assert _statuses("| Customer | Schedule |\n|---|---|\n| Cedar | 2 |\n")["2"] == "confirmed"
