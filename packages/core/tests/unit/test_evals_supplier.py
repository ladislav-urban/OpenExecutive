"""A ``type: supplier`` scenario runs the narrow A2A agent that answers a
supplier's agent (integrations/erpnext_a2a_agent.py) over the scenario's own
documents, its tools answering as oe_connector's mcp/suppliers.py does."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from openexecutive.evals import runner as runner_module
from openexecutive.evals.runner import run_supplier_scenario, supplier_tools
from openexecutive.evals.scenarios import load_scenarios, scenario_supplier, validate_scenario_yaml
from openexecutive.integrations import erpnext_a2a_agent

ORDER = {"name": "PUR-ORD-2026-00012", "status": "To Receive and Bill", "items": [{"item_code": "X", "qty": 1}]}
DONE = {"name": "PUR-ORD-2026-00003", "status": "Completed"}
SCENARIO = {
    "id": "s",
    "type": "supplier",
    "supplier": {
        "name": "Northwind Cables Ltd",
        "message": "Open orders?",
        "orders": [ORDER, DONE],
        "invoices": [
            {"name": "ACC-PINV-1", "outstanding_amount": 10.0},
            {"name": "ACC-PINV-2", "outstanding_amount": 0.0},
        ],
    },
}


def _call(name: str, arguments: dict[str, Any]) -> Any:
    case = scenario_supplier(SCENARIO)
    assert case is not None
    return asyncio.run(supplier_tools(case)(name, arguments))


def test_the_tools_answer_only_the_suppliers_own_documents() -> None:
    open_orders = json.loads(_call("erpnext__supplier_orders", {}))["orders"]
    assert [o["name"] for o in open_orders] == ["PUR-ORD-2026-00012"] and "items" not in open_orders[0]
    every = json.loads(_call("erpnext__supplier_orders", {"include_done": True}))["orders"]
    assert [o["name"] for o in every] == ["PUR-ORD-2026-00012", "PUR-ORD-2026-00003"]
    assert json.loads(_call("erpnext__supplier_order", {"name": "PUR-ORD-2026-00012"}))["order"]["items"]
    # Another supplier's order answers the same as one that doesn't exist.
    assert _call("erpnext__supplier_order", {"name": "PUR-ORD-2026-00031"}) == (
        "No submitted Purchase Order of yours has that number."
    )
    unpaid = json.loads(_call("erpnext__supplier_invoices", {"unpaid_only": True}))["invoices"]
    assert [i["name"] for i in unpaid] == ["ACC-PINV-1"]


def test_a_scenario_runs_the_agent_and_its_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_answer(supplier: str, message: str, call: Any, **_k: Any) -> Any:
        seen["asked"] = (supplier, message)
        seen["tool"] = await call("erpnext__supplier_order", {"name": "nope"})
        return erpnext_a2a_agent.SupplierAnswer("No order of yours has that number.", ["erpnext__supplier_order"])

    async def fake_judge(_scenario: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        seen["outcome"] = outcome
        return {"overall": 5}

    monkeypatch.setattr(erpnext_a2a_agent, "answer", fake_answer)
    monkeypatch.setattr(runner_module, "judge_supplier", fake_judge)
    result = asyncio.run(run_supplier_scenario(SCENARIO))
    assert result["passed"] is True
    assert seen["asked"] == ("Northwind Cables Ltd", "Open orders?")
    assert seen["tool"].startswith("No submitted Purchase Order")
    assert seen["outcome"] == {"reply": "No order of yours has that number.", "tools": ["erpnext__supplier_order"]}


@pytest.mark.parametrize(
    "block",
    [
        "supplier: x",
        "supplier: {name: ' Padded', message: hi}",
        "supplier: {name: Northwind, message: ''}",
        "supplier: {name: Northwind, message: hi, orders: [{status: Open}]}",
    ],
)
def test_a_malformed_block_fails_validation(block: str) -> None:
    with pytest.raises(ValueError, match="supplier"):
        validate_scenario_yaml(f"id: s\ntype: supplier\n{block}\n")


def test_a_supplier_scenario_needs_its_block() -> None:
    with pytest.raises(ValueError, match="`supplier` block"):
        validate_scenario_yaml("id: s\ntype: supplier\n")


def test_the_builtin_supplier_scenarios_are_the_supplier_kind() -> None:
    ids = {s["id"] for s in load_scenarios(kind="supplier")}
    assert {"erpnext_017", "erpnext_018"} <= ids
    for scenario in load_scenarios(kind="supplier"):
        assert scenario_supplier(scenario) is not None
