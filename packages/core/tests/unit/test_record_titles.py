"""A record named by its id alone gets its title from the turn's reads
(orchestrator.record_titles; the local answer check of Nov 5, 2026). Made-up
companies and ids."""
from __future__ import annotations

from openexecutive.orchestrator.figure_check import ErpnextResult
from openexecutive.orchestrator.record_titles import add_titles, titles_of

ORDERS = ErpnextResult(
    "erpnext__list_records",
    {"doctype": "Purchase Order"},
    {
        "doctype": "Purchase Order",
        "rows": [{"name": "PUR-ORD-2026-0001"}, {"name": "PUR-ORD-2026-0002"}],
        "titles": {
            "Purchase Order": {"PUR-ORD-2026-0001": "Birch Switchgear", "PUR-ORD-2026-0002": "Cedar Cable Mills"},
            "Project": {"PROJ-0015": "Warehouse fit-out"},
        },
    },
)


def test_a_bare_id_gets_its_title_once() -> None:
    text = "PUR-ORD-2026-0001 is billed twice. Hold PUR-ORD-2026-0001 until it's credited."
    out, n = add_titles(text, [ORDERS])
    assert n == 1
    assert out == "PUR-ORD-2026-0001 (Birch Switchgear) is billed twice. Hold PUR-ORD-2026-0001 until it's credited."


def test_an_id_already_named_or_bracketed_is_left() -> None:
    for text in (
        "Birch Switchgear's PUR-ORD-2026-0001 is billed twice.",
        "PUR-ORD-2026-0001 (Birch) is billed twice.",
        "See `PUR-ORD-2026-0001` in the list.",
        "Open [PUR-ORD-2026-0001](https://erp.example.com/app/purchase-order/PUR-ORD-2026-0001).",
    ):
        assert add_titles(text, [ORDERS]) == (text, 0), text


def test_ids_in_a_table_and_a_longer_id_are_told_apart() -> None:
    text = "| Order | Project |\n|---|---|\n| PUR-ORD-2026-0002 | PROJ-0015 |\n| PUR-ORD-2026-00021 | — |"
    out, n = add_titles(text, [ORDERS])
    assert n == 2
    assert "| PUR-ORD-2026-0002 (Cedar Cable Mills) | PROJ-0015 (Warehouse fit-out) |" in out
    assert "PUR-ORD-2026-00021 |" in out


def test_a_title_is_written_in_as_plain_text() -> None:
    hostile = ErpnextResult(
        "erpnext__list_records", {"doctype": "Supplier"},
        {"titles": {"Purchase Order": {"PUR-ORD-2026-0009": "Evil [click](javascript:alert)\n# Heading **bold**"}}},
    )
    out, _ = add_titles("PUR-ORD-2026-0009 is late.", [hostile])
    title = out[len("PUR-ORD-2026-0009 ("):out.index(") is late")]
    assert not any(c in title for c in "[]()#*\n`<>")
    assert titles_of([hostile])["PUR-ORD-2026-0009"] == title


def test_no_titles_no_change() -> None:
    bare = ErpnextResult("erpnext__run_report", {}, {"rows": []})
    assert add_titles("PUR-ORD-2026-0001 is late.", [bare]) == ("PUR-ORD-2026-0001 is late.", 0)


# ---------------------------------------------------------------------------
# The security review of ladislav-urban/OpenExecutive#169


def _with(titles: dict[str, str]) -> ErpnextResult:
    return ErpnextResult("erpnext__list_records", {}, {"titles": {"Any": titles}})


def test_a_title_that_reads_as_an_address_is_left_out() -> None:
    for hostile in ("www.evil-pay.com/inv", "pay-acme.com/po", "billing@acme.example", "Acme.io"):
        assert add_titles("PUR-ORD-2026-0009 is late.", [_with({"PUR-ORD-2026-0009": hostile})]) == (
            "PUR-ORD-2026-0009 is late.", 0,
        ), hostile
    out, _ = add_titles("PUR-ORD-2026-0009 is late.", [_with({"PUR-ORD-2026-0009": "__URGENT__ _pay_ Acme Ltd."})])
    assert "_" not in out and "Acme Ltd" in out


def test_ids_inside_longer_ids_or_paths_get_no_title() -> None:
    results = [_with({"Acme": "Acme Inc", "Big Acme": "Big Acme Corp", "ITEM-001": "Widget", "ITEM-001/A": "Widget A"})]
    out, _ = add_titles("Big Acme owes 50,000. Item ITEM-001/A is low. Big Acme again.", results)
    assert "(Acme Inc)" not in out and "ITEM-001 (Widget)/A" not in out
    assert out.startswith("Big Acme (Big Acme Corp) owes")
    assert "ITEM-001/A (Widget A) is low" in out
    path = "See erp.example.com/app/purchase-order/ITEM-001 for it."
    assert add_titles(path, results) == (path, 0)
