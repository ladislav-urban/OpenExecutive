"""ERPNext's chat tools, registered as an extension (orchestrator/extensions.py).

List this module in ``OPENEXECUTIVE_EXTENSIONS`` to offer them:

- ``propose_erpnext_draft`` (``integrations.erpnext_drafts``): an ERPNext draft
  left on a card the person approves. Asked on a turn with no verified caller
  too (``known_caller=False``), since ERPNext's panel names its user without
  OpenExecutive knowing who is speaking; ``erpnext_drafts.offered`` decides
  who may propose there, and a card asked for in the panel is only approved
  in the OpenExecutive app, signed in. Its dispatch row is private: it quotes
  the asker's own ERPNext view, as every ERPNext read does.
- ``schedule_erp_metric`` and ``cancel_erp_metric`` (``briefing.erp_metrics``):
  the principal's scheduled ERP metrics, on their own chat turns.

What each is offered on is its own ``offered``. As extension tools they are
never offered on an unattended run, a turn private to the principal, a turn
from another AI app or a replay, and Act as me's lockdown refuses them once
the turn has read the owner's mail.
"""
from __future__ import annotations

from openexecutive.briefing import erp_metrics
from openexecutive.integrations import erpnext_drafts
from openexecutive.orchestrator.extensions import ExtraTool, register_tool


def register() -> None:
    (propose,) = erpnext_drafts.draft_tools()
    register_tool(
        ExtraTool(
            definition=propose,
            handler=erpnext_drafts.handle_propose,
            offered=erpnext_drafts.offered,
            chip=erpnext_drafts.chip,
            known_caller=False,
            private_row=True,
            activity_label="Checking the draft with ERPNext…",
        )
    )
    for definition in erp_metrics.METRIC_TOOLS:
        register_tool(
            ExtraTool(
                definition=definition,
                handler=erp_metrics.METRIC_TOOL_HANDLERS[definition["name"]],
                offered=erp_metrics.offered,
            )
        )
