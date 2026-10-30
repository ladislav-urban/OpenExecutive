"""Load and validate eval scenarios.

Scenarios come from two sources:
1. Built-in YAML files shipped inside the package (`_scenarios/*.yaml`).
2. User-added scenarios stored in the `eval_scenarios` SQLite table (created
   via the `/evals/scenarios` POST endpoint). These persist on the `/data`
   volume across deploys.

Built-in scenarios are read-only. User scenarios are editable/deletable via
the API. ID collisions between the two are blocked at insert time.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from openexecutive.memory.workspace_settings import PrincipalRole


def _scenarios_dir() -> Path:
    env_path = os.environ.get("EVAL_SCENARIOS_PATH")
    if env_path:
        return Path(env_path)
    # YAMLs ship inside the package — works for any install layout.
    return Path(__file__).parent / "_scenarios"


def scenario_kind(s: dict[str, Any]) -> str:
    if s.get("domain") == "triage":
        return "triage"
    if s.get("type") == "workflow":
        return "workflow"
    if s.get("type") == "inbox":
        return "inbox"
    if s.get("type") == "supplier":
        return "supplier"
    if s.get("requires_mcp"):
        return "mcp"
    return "chat"


def scenario_routing(s: dict[str, Any]) -> tuple[list[str], bool]:
    """A chat scenario's routing expectation: ``(required, none)``.

    ``required_routing`` lists the specialists the Executive must consult;
    ``no_routing: true`` says it must consult none (a lookup or a routine
    draft). Raises ``ValueError`` on a malformed or contradictory pair.
    """
    required = s.get("required_routing") or []
    if not isinstance(required, list) or not all(isinstance(r, str) and r for r in required):
        raise ValueError("`required_routing` must be a list of specialist names")
    none = s.get("no_routing", False)
    if not isinstance(none, bool):
        raise ValueError("`no_routing` must be true or false")
    if none and required:
        raise ValueError("`no_routing` and `required_routing` cannot both be set")
    return list(required), none


def routing_check(s: dict[str, Any], consulted: list[str]) -> dict[str, Any] | None:
    """Whether the turn consulted the specialists the scenario expects.

    ``None`` when the scenario sets no expectation. Otherwise
    ``{"consulted", "missing", "ok"}``: every required specialist was
    consulted (others may be too), or none was when ``no_routing`` is set.
    """
    required, none = scenario_routing(s)
    if not required and not none:
        return None
    seen = sorted(set(consulted))
    missing = [r for r in required if r not in seen]
    ok = not seen if none else not missing
    return {"consulted": seen, "missing": missing, "ok": ok}


def _load_builtin_scenarios() -> list[dict[str, Any]]:
    scenarios_dir = _scenarios_dir()
    if not scenarios_dir.exists():
        return []
    out = []
    for yaml_file in sorted(scenarios_dir.glob("*.yaml")):
        with open(yaml_file, encoding="utf-8") as f:
            s = yaml.safe_load(f)
        s["_kind"] = scenario_kind(s)
        s["_is_builtin"] = True
        out.append(s)
    return out


def _load_user_scenarios() -> list[dict[str, Any]]:
    # Imported lazily so this module stays usable without the DB layer
    # (e.g. the standalone `evals/run_evals.py` CLI).
    from openexecutive.evals.persistence import list_user_scenarios

    out = []
    for row in list_user_scenarios():
        try:
            s = yaml.safe_load(row["yaml"])
        except yaml.YAMLError:
            continue
        if not isinstance(s, dict):
            continue
        s["_kind"] = scenario_kind(s)
        s["_is_builtin"] = False
        out.append(s)
    return out


def load_scenarios(
    kind: str | None = None,
    scenario_id: str | None = None,
) -> list[dict[str, Any]]:
    all_scenarios = _load_builtin_scenarios() + _load_user_scenarios()

    results = []
    for s in all_scenarios:
        if scenario_id and s.get("id") != scenario_id:
            continue
        if kind and s["_kind"] != kind:
            continue
        results.append(s)
    return results


def list_scenario_meta() -> list[dict[str, Any]]:
    return [
        {
            "id": s["id"],
            "kind": s["_kind"],
            "domain": s.get("domain", ""),
            "description": s.get("description", ""),
            "workflow": s.get("workflow"),
            "is_builtin": s.get("_is_builtin", True),
        }
        for s in load_scenarios()
    ]


def scenario_principal_role(scenario: dict[str, Any]) -> PrincipalRole | None:
    """The scenario's ``principal_role`` block as a ``PrincipalRole``, or
    None when it has none. Validated like the API (``validate_role_field``);
    an unknown key, a bad value, a block with nothing in it, or a block on a
    scenario that is not ``workspace_mode: solo`` (only solo reads a role)
    raises ValueError — a typo fails the scenario instead of silently
    playing a principal with no role."""
    from openexecutive.memory.workspace_settings import (
        ROLE_FIELDS,
        PrincipalRole,
        validate_role_field,
    )

    raw = scenario.get("principal_role")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("principal_role must be a mapping")
    unknown = set(raw) - set(ROLE_FIELDS)
    if unknown:
        raise ValueError(
            f"principal_role has unknown key(s) {sorted(map(str, unknown))}; "
            f"allowed: {', '.join(ROLE_FIELDS)}"
        )
    role = PrincipalRole.model_validate({f: validate_role_field(f, raw.get(f)) for f in ROLE_FIELDS})
    if role.is_empty():
        raise ValueError("principal_role sets no field")
    if scenario.get("workspace_mode") != "solo":
        raise ValueError("principal_role needs workspace_mode: solo (only solo reads a role)")
    return role


# Where a chat scenario's turn comes from, besides the web app (no `surface`).
SURFACE_ERPNEXT_PANEL = "erpnext_panel"
# A web chat turn a host origin's page sent (Session.from_host): the desk's
# own Chats. It reads and advises, and reads ERPNext only as a signed-in
# caller, which an eval run has none of (no CALLER_ASSERTION_PUBLIC_KEYS).
SURFACE_HOST = "host"
_SURFACES = (SURFACE_ERPNEXT_PANEL, SURFACE_HOST)
# The ERPNext user a panel scenario's turn is from. Only marks the turn as the
# panel's (schedule_tools.erpnext_panel_turn); nothing is signed or sent.
EVAL_PANEL_EMAIL = "principal@eval.example"
# Who a scenario's turn is from, when it says (`speaker`): the install's
# principal, as the product sees them. Without it a turn has no speaker, so
# whatever is the principal's alone (ERPNext reads, drafts, scheduled
# metrics) is refused, and the scenario is judged on that refusal.
SPEAKER_PRINCIPAL = "principal"


def scenario_speaker(scenario: dict[str, Any]) -> str | None:
    """The scenario's ``speaker``: None (no one, the default), or
    ``principal``. Anything else raises ValueError."""
    speaker = scenario.get("speaker")
    if speaker is None:
        return None
    if speaker != SPEAKER_PRINCIPAL:
        raise ValueError(f"speaker must be {SPEAKER_PRINCIPAL!r}, got {speaker!r}")
    return SPEAKER_PRINCIPAL


def _principal() -> Any:
    from openexecutive.people.store import find_principal_person

    return find_principal_person()


def scenario_surface(scenario: dict[str, Any]) -> str | None:
    """The scenario's ``surface``: None for the web app (the default), or
    ``host`` for a web chat turn a host origin's page sent (the desk's Chats),
    ``erpnext_panel`` for a turn from ERPNext's "Ask the Dynocortex" panel,
    which the runner runs as ``integrations.erpnext_panel`` does (its session
    marks and ``erpnext_panel.context_block``). Anything else raises ValueError, so a
    typo fails the scenario instead of silently running in the web app."""
    surface = scenario.get("surface")
    if surface is None:
        return None
    if surface not in _SURFACES:
        raise ValueError(f"surface must be one of {_SURFACES!r}, got {surface!r}")
    return str(surface)


def scenario_turn_surface(scenario: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """``_surface_fields``, plus the demo mark (``Session.demo_caller``) when
    the scenario is a demo person's turn (``demo_viewer: true``): the narrow
    tools and the org block's demo variant, as people.demo gives one."""
    fields, block = _surface_fields(scenario)
    if scenario.get("demo_viewer") is True:
        fields = {**fields, "demo_caller": True}
    return fields, block


def _surface_fields(scenario: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """What the scenario's ``surface`` adds to its turn, for both eval runners
    (``evals.runner`` and ``evals/run_evals.py``): extra ``Session`` fields and
    the ``channel_context_block`` for ``Executive.chat``. The web app adds
    nothing; ERPNext's panel adds what ``integrations.erpnext_panel`` gives a
    turn. With ``speaker: principal`` the turn is the install's principal: in
    the web app signed in as them (no principal on the roster yet: the
    single-user operator, as a header-less request is), in the panel as their
    own email."""
    from openexecutive.orchestrator.session import Session

    principal = _principal() if scenario_speaker(scenario) else None
    if scenario_surface(scenario) == SURFACE_HOST:
        return {
            "from_web_chat": True,
            "from_host": True,
            "caller_person_id": principal.id if principal is not None else None,
            "web_caller_signed_in": principal is not None,
        }, ""
    if scenario_surface(scenario) != SURFACE_ERPNEXT_PANEL:
        if not scenario_speaker(scenario):
            return {}, ""
        return {
            "from_web_chat": True,
            "caller_person_id": principal.id if principal is not None else None,
            "web_caller_signed_in": principal is not None,
        }, ""
    from openexecutive.integrations.erpnext_panel import context_block

    email = (getattr(principal, "email", None) or "").strip().lower() or EVAL_PANEL_EMAIL
    session_fields = {
        "origin_channel": "erpnext",
        "erpnext_panel_email": email,
        "read_erpnext": True,
    }
    # The block the panel gives that person: with drafts when they are
    # offered them (phase 4b), as integrations.erpnext_panel chooses it.
    probe = Session(origin_channel="erpnext", erpnext_panel_email=email, read_erpnext=True)
    return session_fields, context_block(probe)


def scenario_query(scenario: dict[str, Any]) -> str:
    """The turn's message: the scenario's ``query``, after the page block its
    ``page`` gives a panel turn, as ``integrations.erpnext_panel`` builds it
    (``{"screen": "/watchlist"}``, ``{"doctype": ..., "name": ...}``)."""
    page = scenario.get("page")
    if page is None:
        return scenario["query"]
    from openexecutive.integrations.erpnext_panel import page_block

    where = page_block(page)
    return f"{where}\n\n{scenario['query']}" if where else scenario["query"]


def scenario_voice_persona(scenario: dict[str, Any]) -> str | None:
    """The scenario's ``voice_persona`` slug, or None to use the install's
    voice. An unknown slug raises ValueError, so a typo fails the scenario
    instead of silently running in the default voice."""
    from openexecutive.personas.loader import persona_exists

    slug = scenario.get("voice_persona")
    if slug is None:
        return None
    if not isinstance(slug, str) or not persona_exists(slug):
        raise ValueError(f"voice_persona {slug!r} is not a known voice")
    return slug


def scenario_delegation(scenario: dict[str, Any]) -> Any:
    """The scenario's ``delegation`` block (Act as me) as a
    ``delegation.settings.DelegationOverride`` over a fresh in-memory mailbox
    (``evals.mailbox.ScenarioMailbox``), or None when it has none.

    Shape::

        delegation:
          person: {full_name: Olivia Owner, email: olivia@fernway.example}
          thread:            # or threads: [...]
            id: t-pilot
            subject: Brand refresh pilot
            messages:
              - {from: "Dana <dana@northpeak.example>", text: "...", date: "...",
                 reply_to: "...", cc: ["..."],
                 attachments: [{name: scope.txt, text: "..."}]}

    Raises ValueError on a malformed block, so a typo fails the scenario."""
    from openexecutive.delegation.settings import DelegationOverride
    from openexecutive.evals.mailbox import ScenarioMailbox
    from openexecutive.people.models import Person

    raw = scenario.get("delegation")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("delegation must be a mapping")
    person_raw = raw.get("person")
    if not isinstance(person_raw, dict) or not person_raw.get("full_name") or "@" not in str(person_raw.get("email") or ""):
        raise ValueError("delegation.person needs full_name and email")
    email = str(person_raw["email"]).strip().lower()
    threads_raw = raw.get("threads") if raw.get("threads") is not None else (
        [raw["thread"]] if raw.get("thread") is not None else []
    )
    if not isinstance(threads_raw, list):
        raise ValueError("delegation.threads must be a list")
    threads = [_scenario_thread(t, email, "delegation") for t in threads_raw]
    return DelegationOverride(
        enabled=raw.get("enabled", True) is not False,
        gmail=ScenarioMailbox(email, threads, _scenario_files(threads_raw)),
        person=Person(id=0, full_name=str(person_raw["full_name"]), email=email, is_principal=True),
    )


def _scenario_files(threads_raw: list[Any]) -> dict[tuple[str, int], bytes]:
    """The text files attached to a ``delegation`` block's messages, by
    (message id, 1-based index) as ``_scenario_thread`` numbers them."""
    files: dict[tuple[str, int], bytes] = {}
    for t in threads_raw:
        for i, m in enumerate(t["messages"], 1):
            for n, a in enumerate(m.get("attachments") or [], 1):
                files[(f"{t['id']}-{i}", n)] = str(a.get("text") or "").encode()
    return files


def _scenario_attachments(raw: Any, block: str) -> list[Any]:
    from openexecutive.delegation.gmail import MailAttachment

    if not isinstance(raw, list) or not all(isinstance(a, dict) and a.get("name") and isinstance(a.get("text"), str) for a in raw):
        raise ValueError(f"each {block} attachment needs a name and text")
    return [
        MailAttachment(index=n, name=str(a["name"]), mime_type="text/plain", size=len(a["text"].encode()))
        for n, a in enumerate(raw, 1)
    ]


def _scenario_thread(t: Any, email: str, block: str) -> Any:
    """One thread of a ``delegation`` or ``inbox`` block as a ``MailThread``:
    ``{id, subject, messages: [{from, text, date?, cc?, reply_to?, verified?}]}``.
    A message from ``email`` is the person's own (SENT)."""
    from email.utils import getaddresses

    from openexecutive.delegation.gmail import MailMessage, MailThread, valid_id

    if not isinstance(t, dict) or not valid_id(t.get("id")) or not isinstance(t.get("messages"), list):
        raise ValueError(f"each {block} thread needs an id and a messages list")
    messages = []
    for i, m in enumerate(t["messages"], 1):
        if not isinstance(m, dict) or not m.get("from") or not isinstance(m.get("text"), str):
            raise ValueError(f"each {block} message needs from and text")
        sender = getaddresses([str(m["from"])])
        name, addr = sender[0] if sender else ("", "")
        mine = addr.strip().lower() == email
        attached = _scenario_attachments(m.get("attachments") or [], block)
        messages.append(MailMessage(
            id=f"{t['id']}-{i}",
            thread_id=str(t["id"]),
            from_addr=addr.strip().lower(),
            from_name=name.strip(),
            to=[str(a).strip().lower() for a in m.get("to") or [email]],
            cc=[str(c).strip().lower() for c in m.get("cc") or []],
            reply_to=str(m.get("reply_to") or "").strip().lower(),
            subject=str(t.get("subject") or ""),
            date=str(m.get("date") or ""),
            message_id_header=f"<{t['id']}-{i}@eval.example>",
            labels=["SENT"] if mine else ["INBOX"],
            text=m["text"],
            sender_authenticated=m.get("verified", True) is not False,
            attachments=attached,
            has_attachments=bool(attached),
        ))
    return MailThread(id=str(t["id"]), messages=messages)


def scenario_inbox(scenario: dict[str, Any]) -> Any:
    """The scenario's ``inbox`` block (the inbox watcher, ``type: inbox``) as
    an ``InboxCase``, or None when it has none. The message the watcher
    answers is the thread's newest one from someone else.

    Shape::

        inbox:
          person: {full_name: Olivia Owner, email: olivia@fernway.example}
          relation: contact          # team | contact | correspondent | stranger
          expect: draft              # draft | no_draft
          thread: {id: t1, subject: ..., messages: [...]}   # as in delegation

    Raises ValueError on a malformed block, so a typo fails the scenario."""
    from openexecutive.delegation.inbox_classifier import THRESHOLDS
    from openexecutive.people.models import Person

    raw = scenario.get("inbox")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("inbox must be a mapping")
    person_raw = raw.get("person")
    if not isinstance(person_raw, dict) or not person_raw.get("full_name") or "@" not in str(person_raw.get("email") or ""):
        raise ValueError("inbox.person needs full_name and email")
    if raw.get("relation") not in THRESHOLDS:
        raise ValueError(f"inbox.relation must be one of {sorted(THRESHOLDS)}")
    if raw.get("expect") not in ("draft", "no_draft"):
        raise ValueError("inbox.expect must be draft or no_draft")
    email = str(person_raw["email"]).strip().lower()
    thread = _scenario_thread(raw.get("thread"), email, "inbox")
    inbound = [m for m in thread.messages if m.from_addr != email]
    if not inbound:
        raise ValueError("inbox.thread needs a message from someone else")
    return InboxCase(
        person=Person(id=0, full_name=str(person_raw["full_name"]), email=email, is_principal=True),
        relation=str(raw["relation"]),
        expect_draft=raw["expect"] == "draft",
        thread=thread,
        message=inbound[-1],
    )


@dataclass
class InboxCase:
    person: Any
    relation: str
    expect_draft: bool
    thread: Any
    message: Any


@dataclass
class SupplierCase:
    supplier: str
    message: str
    # The supplier's own submitted documents, as oe_connector's supplier tools
    # return them (an order may carry its `items`).
    orders: list[dict[str, Any]]
    invoices: list[dict[str, Any]]


def scenario_supplier(scenario: dict[str, Any]) -> SupplierCase | None:
    """The scenario's ``supplier`` block (a supplier's agent over A2A,
    ``type: supplier``) as a ``SupplierCase``, or None when it has none.

    Shape::

        supplier:
          name: Northwind Cables Ltd          # the signed supplier
          message: "Is PUR-ORD-2026-00031 open?"   # what its agent sends
          orders: [{name: PUR-ORD-2026-00012, status: To Receive and Bill, ..., items: [...]}]
          invoices: [{name: ACC-PINV-2026-00007, outstanding_amount: 1200, ...}]

    Only these documents are the supplier's: the runner's tools answer any
    other order number as oe_connector does, the same as one that doesn't
    exist. Raises ValueError on a malformed block."""
    from openexecutive.orchestrator.erpnext_caller import valid_supplier

    raw = scenario.get("supplier")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("supplier must be a mapping")
    if not valid_supplier(raw.get("name")):
        raise ValueError("supplier.name must be a Supplier name")
    message = raw.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("supplier.message is required")
    docs: dict[str, list[dict[str, Any]]] = {}
    for key in ("orders", "invoices"):
        rows = raw.get(key) or []
        if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("name") for r in rows):
            raise ValueError(f"supplier.{key} must be a list of documents with a name")
        docs[key] = rows
    return SupplierCase(str(raw["name"]), message.strip(), docs["orders"], docs["invoices"])


def builtin_scenario_ids() -> set[str]:
    return {s["id"] for s in _load_builtin_scenarios()}


def validate_scenario_yaml(raw: str) -> dict[str, Any]:
    """Parse and lightly validate scenario YAML. Raises ValueError on problems.

    Heavy validation (workflow inputs match the workflow's input_model, etc.)
    happens at run time and surfaces as a normal `scenario_error` event.
    """
    try:
        s = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}") from e

    if not isinstance(s, dict):
        raise ValueError("Top-level YAML must be a mapping")
    if not s.get("id") or not isinstance(s["id"], str):
        raise ValueError("Missing or non-string `id` field")
    if "/" in s["id"] or " " in s["id"]:
        raise ValueError("`id` must not contain '/' or whitespace")

    if s.get("workspace_mode") is not None and s["workspace_mode"] not in ("solo", "team"):
        raise ValueError("`workspace_mode` must be 'solo' or 'team'")
    if s.get("principal_role") is not None:
        try:
            scenario_principal_role(s)
        except ValueError as e:
            raise ValueError(f"`principal_role`: {e}") from e
    if s.get("voice_persona") is not None:
        scenario_voice_persona(s)
    if s.get("product_name") is not None:
        from openexecutive.utils.product_name import _clean

        if not isinstance(s["product_name"], str) or _clean(s["product_name"]) is None:
            raise ValueError("`product_name`: a plain name of up to 40 characters")
    if s.get("advice_policy") is not None and s["advice_policy"] != "explain":
        raise ValueError("`advice_policy` must be 'explain' (or absent)")
    if s.get("forbidden_reply_words") is not None:
        words = s["forbidden_reply_words"]
        if not isinstance(words, list) or not words or not all(isinstance(w, str) and w.strip() for w in words):
            raise ValueError("`forbidden_reply_words`: a list of non-empty strings")
    if s.get("speaker") is not None:
        try:
            scenario_speaker(s)
        except ValueError as e:
            raise ValueError(f"`speaker`: {e}") from e
    if s.get("surface") is not None:
        try:
            scenario_surface(s)
        except ValueError as e:
            raise ValueError(f"`surface`: {e}") from e
    for flag in ("demo_viewer", "forbid_principal_name"):
        if s.get(flag) is not None and not isinstance(s[flag], bool):
            raise ValueError(f"`{flag}` must be true or false")
    if s.get("page") is not None:
        if scenario_surface(s) != SURFACE_ERPNEXT_PANEL:
            raise ValueError("`page`: only a scenario with surface: erpnext_panel has a page")
        if not isinstance(s["page"], dict):
            raise ValueError("`page`: must be a mapping")
        from openexecutive.integrations.erpnext_panel import page_block

        if not page_block(s["page"]):
            raise ValueError("`page`: the panel would send nothing for it")
    if s.get("delegation") is not None:
        try:
            scenario_delegation(s)
        except ValueError as e:
            raise ValueError(f"`delegation`: {e}") from e
    if s.get("inbox") is not None:
        try:
            scenario_inbox(s)
        except ValueError as e:
            raise ValueError(f"`inbox`: {e}") from e
    if s.get("supplier") is not None:
        try:
            scenario_supplier(s)
        except ValueError as e:
            raise ValueError(f"`supplier`: {e}") from e

    scenario_routing(s)

    k = scenario_kind(s)
    if k == "chat" and not s.get("query"):
        raise ValueError("chat scenarios require a `query` field")
    if k == "workflow" and not s.get("workflow"):
        raise ValueError("workflow scenarios require a `workflow` field")
    if k == "inbox" and s.get("inbox") is None:
        raise ValueError("inbox scenarios require an `inbox` block")
    if k == "supplier" and s.get("supplier") is None:
        raise ValueError("supplier scenarios require a `supplier` block")
    if k == "triage" and not s.get("event"):
        raise ValueError("triage scenarios require an `event` field")

    return s


def forbidden_words_in(scenario: dict[str, Any], reply: str) -> list[str]:
    """The scenario's ``forbidden_reply_words`` the reply says, whole words and
    any case: a deterministic check the judge's score can't outvote."""
    import re

    words = [w.strip() for w in scenario.get("forbidden_reply_words") or []]
    if scenario.get("forbid_principal_name") is True:
        # The install's own principal, read at run time: no fixture can name
        # the real person a demo must never reveal.
        principal = _principal()
        name = (getattr(principal, "full_name", None) or "").strip()
        if name:
            words += [name, name.split()[0]]
    found = []
    for word in words:
        if word and re.search(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", reply, re.IGNORECASE):
            found.append(word)
    return found
