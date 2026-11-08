"""An ERPNext draft card is approved only on the web card, by its own
approver, with a signed caller and the draft's fingerprint
(integrations/erpnext_drafts.py). Upstream's cards answered from chat
(SenteLabsAI/OpenExecutive#658, #722) go through decisions.resolve_in_chat for
the classes in CHAT_CARD_CLASSES only, so a draft card must stay out of that
list: a chat app's tap and the answer_card tool both find it "no longer there"."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from openexecutive.api.routes import decisions
from openexecutive.integrations import erpnext_drafts


def _draft_card() -> SimpleNamespace:
    return SimpleNamespace(
        id=41, decision_class=erpnext_drafts.DECISION_CLASS, status="proposed",
        approver_person_id=1, payload={},
    )


def test_a_draft_card_is_never_shown_in_chat() -> None:
    assert erpnext_drafts.DECISION_CLASS not in decisions.CHAT_CARD_CLASSES
    assert decisions.shown_in_chat(_draft_card()) is False  # type: ignore[arg-type]


def test_a_chat_tap_or_answer_card_cannot_approve_a_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decisions, "get_decision_instance", lambda _id: _draft_card())
    approver = SimpleNamespace(id=1)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(decisions.resolve_in_chat(41, approver, approve=True))
    assert refused.value.status_code == 404
