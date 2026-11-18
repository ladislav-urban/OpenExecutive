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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import yaml

if TYPE_CHECKING:
    from openexecutive.memory.workspace_settings import PrincipalRole


# A watch job's model "reason" line (``type: watch_reason``): the scenario's
# findings go through the job's own reason step.
WATCH_REASON = "watch_reason"


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
    if s.get("type") == WATCH_REASON:
        return WATCH_REASON
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


# The most ``live: true`` prior turns a scenario may have: each is a billed
# chat() turn, with its own tool calls, run before the question every time
# the scenario runs. Two covers a follow-up that needs its first steps real
# (erpnext_064); more would make one scenario cost several.
MAX_LIVE_PRIOR_TURNS = 2


@dataclass(frozen=True)
class PriorTurn:
    """One of a scenario's ``prior_turns``: ``message`` as the turn sent it
    (its page block first), ``typed`` as the person typed it, and ``reply``
    its fixed answer, or None for a ``live: true`` turn, which the runner
    runs (``runner.run_chat_turn``)."""

    message: str
    typed: str
    reply: str | None


def parse_prior_turns(scenario: dict[str, Any]) -> list[PriorTurn]:
    """The conversation before the scenario's question (``prior_turns``):
    ``[{user, assistant, page?}]`` or ``[{user, live: true, page?}]``,
    oldest first. A turn's ``page`` puts the page block before its message,
    as ``scenario_query`` does for the question. A fixed turn is seeded into
    the session's history as written; a live one has no ``assistant`` and
    is run first, so the question follows the real reply (an ERPNext
    follow-up whose fixed answer would hold no figures, or stale ones).
    At most ``MAX_LIVE_PRIOR_TURNS`` turns may be live."""
    raw = scenario.get("prior_turns")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("`prior_turns`: a list of {user, assistant, page?} or {user, live: true, page?}")
    turns: list[PriorTurn] = []
    for turn in raw:
        if not isinstance(turn, dict) or set(turn) - {"user", "assistant", "page", "live"}:
            raise ValueError("`prior_turns`: each turn is {user, assistant, page?} or {user, live: true, page?}")
        live = turn.get("live", False)
        if not isinstance(live, bool):
            raise ValueError("`prior_turns`: `live` must be true or false")
        user, reply = turn.get("user"), turn.get("assistant")
        if not (isinstance(user, str) and user.strip()):
            raise ValueError("`prior_turns`: each turn needs a non-empty user")
        if live and "assistant" in turn:
            raise ValueError("`prior_turns`: a live turn has no assistant (its reply is the one it gets when run)")
        if not live and not (isinstance(reply, str) and reply.strip()):
            raise ValueError("`prior_turns`: each turn needs a non-empty user and assistant, unless it is live")
        message = scenario_query({"query": user, "page": turn.get("page")})
        turns.append(PriorTurn(message=message, typed=user, reply=None if live else reply))
    live_count = sum(1 for t in turns if t.reply is None)
    if live_count > MAX_LIVE_PRIOR_TURNS:
        raise ValueError(
            f"`prior_turns`: at most {MAX_LIVE_PRIOR_TURNS} live turns (each is a billed turn), not {live_count}"
        )
    return turns


def scenario_prior_turns(scenario: dict[str, Any]) -> list[tuple[str, str]]:
    """The scenario's ``prior_turns`` as (user message, reply), for a
    scenario whose turns all have their reply: fixed ones, or live ones the
    runner has run (``with_prior_replies``). Raises for a live turn not run
    yet, which has no reply to seed or show."""
    turns = parse_prior_turns(scenario)
    if any(t.reply is None for t in turns):
        raise ValueError("`prior_turns`: a live turn has no reply until it is run")
    return [(t.message, t.reply or "") for t in turns]


# Set by ``with_prior_replies`` on a scenario some of whose prior turns ran
# live, so ``prior_turns_section`` tells the judge those replies are unjudged.
PRIOR_TURNS_RAN_LIVE = "_prior_turns_ran_live"

# For the judge, after a conversation with a live turn: no live turn is
# judged itself, so the final answer can't lean on one being right.
LIVE_PRIOR_TURNS_NOTE = (
    "The earlier assistant replies above were produced live on this run and "
    "are not assumed correct. A final answer that agrees with an earlier reply "
    "passes only if its figures are right from what this turn read; a final "
    "answer that carries an earlier reply's error forward fails the criterion "
    "that error touches."
)


def with_prior_replies(scenario: dict[str, Any], replies: list[str]) -> dict[str, Any]:
    """``scenario`` as it ran: each prior turn with the reply it had, a live
    turn's being the real one, so the judge sees the conversation the
    question followed (``prior_turns_section``). When any turn ran live it
    is marked (``PRIOR_TURNS_RAN_LIVE``), for ``LIVE_PRIOR_TURNS_NOTE``."""
    raw = scenario.get("prior_turns")
    if not raw:
        return scenario
    turns = [
        {**{k: v for k, v in turn.items() if k != "live"}, "assistant": reply}
        for turn, reply in zip(raw, replies, strict=True)
    ]
    ran = {**scenario, "prior_turns": turns}
    if any(turn.get("live") is True for turn in raw):
        ran[PRIOR_TURNS_RAN_LIVE] = True
    return ran


def seed_prior_turns(session: Any, scenario: dict[str, Any]) -> None:
    """Put the scenario's fixed ``prior_turns`` in ``session``'s history."""
    for user, reply in scenario_prior_turns(scenario):
        session.add_user_message(user)
        session.add_assistant_message(reply)


def prior_turns_section(scenario: dict[str, Any]) -> str:
    """For a judge: the conversation the question follows, or "" when it
    follows none (every other judge prompt stays byte-identical). After a
    conversation with a live turn (``with_prior_replies``) it ends with
    ``LIVE_PRIOR_TURNS_NOTE``: a live reply is never judged itself."""
    turns = scenario_prior_turns(scenario)
    if not turns:
        return ""
    lines = "\n\n".join(
        f"ASKER: {user}\nASSISTANT: {reply}" for user, reply in turns
    )
    note = f"{LIVE_PRIOR_TURNS_NOTE}\n" if scenario.get(PRIOR_TURNS_RAN_LIVE) is True else ""
    return (
        "\nEARLIER IN THIS CONVERSATION (the asker sees these turns; the question "
        f"below follows them):\n{lines}\n{note}"
    )


_DECIDED_OUTCOMES = frozenset({"approved", "dismissed", "closed", "expired"})
_DECIDED_KEYS = frozenset({"doctype", "outcome", "closed_code", "replaced", "name", "hours_ago", "tried"})


def scenario_decided_cards(scenario: dict[str, Any]) -> tuple[Any, ...] | None:
    """The ERPNext draft cards this conversation left that are no longer
    waiting (``decided_cards``), as the turn's ``<decided_cards>`` note is
    told them, or None when the scenario has none. Each is
    ``{doctype, outcome, hours_ago, closed_code?, replaced?, name?, tried?}``; the
    runners hand them to ``erpnext_drafts.decided_cards_override``, so no
    card is ever written to the install's ledger. Raises ValueError on a
    malformed block."""
    from datetime import UTC, datetime, timedelta

    from openexecutive.integrations.erpnext_drafts import DECIDED_WINDOW, DecidedCard

    raw = scenario.get("decided_cards")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError("`decided_cards`: a list of {doctype, outcome, hours_ago, closed_code?, replaced?, name?}")
    now = datetime.now(UTC)
    found: list[DecidedCard] = []
    for card in raw:
        if not isinstance(card, dict) or set(card) - _DECIDED_KEYS:
            raise ValueError("`decided_cards`: each card is {doctype, outcome, hours_ago, closed_code?, replaced?, name?}")
        doctype, outcome, hours = card.get("doctype"), card.get("outcome"), card.get("hours_ago")
        if not isinstance(doctype, str) or not doctype.strip():
            raise ValueError("`decided_cards`: each card needs a doctype")
        if outcome not in _DECIDED_OUTCOMES:
            raise ValueError(f"`decided_cards`: outcome is one of {sorted(_DECIDED_OUTCOMES)}")
        if (
            not isinstance(hours, (int, float)) or isinstance(hours, bool)
            or not 0 <= hours <= DECIDED_WINDOW.total_seconds() / 3600
        ):
            raise ValueError("`decided_cards`: hours_ago is a number of hours within the note's window")
        code, replaced, name = card.get("closed_code", ""), card.get("replaced", False), card.get("name", "")
        tried = card.get("tried", False)
        if (
            not isinstance(code, str) or not isinstance(replaced, bool) or not isinstance(name, str)
            or not isinstance(tried, bool)
        ):
            raise ValueError("`decided_cards`: closed_code and name are text, replaced and tried are true or false")
        found.append(DecidedCard(
            doctype=doctype.strip(), outcome=str(outcome), decided_at=now - timedelta(hours=float(hours)),
            closed_code=code, replaced=replaced, name=name, tried=tried,
        ))
    return tuple(sorted(found, key=lambda c: c.decided_at))


def decided_cards_section(scenario: dict[str, Any]) -> str:
    """For a judge: what the assistant was told became of the conversation's
    draft cards, or "" when the scenario has none (every other judge prompt
    stays byte-identical)."""
    from openexecutive.integrations.erpnext_drafts import decided_says

    found = scenario_decided_cards(scenario)
    if not found:
        return ""
    lines = "\n".join(f"- {c.doctype} draft card: {decided_says(c)}." for c in found)
    return (
        "\nDRAFT CARDS THE ASSISTANT WAS TOLD ABOUT (left earlier in this conversation; "
        "this is all the record says about them, so any other reason it gives is invented):\n"
        f"{lines}\n"
    )


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


@dataclass
class WatchReasonFinding:
    # The scenario's name for the finding (for the judge and the result;
    # the model never sees it).
    label: str
    # The finding as the job's own code builds it (a ``CardSpec``).
    spec: Any
    # True: a line must reach the notice; False: none may; None: either.
    expect_line: bool | None


@dataclass
class WatchReasonCase:
    job: Any  # the registered ``WatchJob``
    findings: list[WatchReasonFinding]


# How a scenario gives one finding of each job it can run: (block, today) ->
# the job's own CardSpec. Only a job listed here has watch_reason scenarios.
# None is listed today: project status, the first, writes its sentence in
# code from the figures (watch.jobs.project_status.standing), so it has no
# model line to evaluate. A job with a reason step adds its builder here.
_WATCH_FINDINGS: dict[str, Any] = {}


def scenario_watch_reason(scenario: dict[str, Any]) -> WatchReasonCase | None:
    """The scenario's ``watch_reason`` block (a watch job's model line,
    ``type: watch_reason``) as a ``WatchReasonCase``, or None when it has
    none.

    Shape::

        watch_reason:
          job: <a job listed in _WATCH_FINDINGS>
          findings:                 # one or more, each its own call
            - ...                   # the job's builder's own keys
              expect_line: true     # optional: a line must (true) or
                                    # must not (false) reach the notice

    Each finding is built by the job's own code, so the model is shown its
    facts exactly as a run shows them. Raises ValueError on a malformed
    block."""
    from datetime import date

    from openexecutive.watch import registry
    from openexecutive.watch.runner import MAX_REASON_FINDINGS

    raw = scenario.get(WATCH_REASON)
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"job", "findings"}:
        raise ValueError("watch_reason must be a mapping of job and findings")
    job = registry.get(raw.get("job"))
    build = _WATCH_FINDINGS.get(str(raw.get("job")))
    if job is None or job.reason_schema is None or build is None:
        raise ValueError(f"watch_reason.job must be one of {sorted(_WATCH_FINDINGS)}")
    found = raw.get("findings")
    limit = min(job.reason_schema.max_findings, MAX_REASON_FINDINGS)
    if not isinstance(found, list) or not 1 <= len(found) <= limit:
        raise ValueError(f"watch_reason.findings: a list of 1 to {limit} findings")
    today = date.today()
    findings: list[WatchReasonFinding] = []
    for n, item in enumerate(found, 1):
        if not isinstance(item, dict):
            raise ValueError(f"watch_reason.findings[{n}] must be a mapping")
        expect = item.get("expect_line")
        if expect is not None and not isinstance(expect, bool):
            raise ValueError(f"watch_reason.findings[{n}]: `expect_line` must be true or false")
        try:
            spec = build(item, today)
        except ValueError as e:
            raise ValueError(f"watch_reason.findings[{n}]: {e}") from e
        findings.append(WatchReasonFinding(str(item["project"]), spec, expect))
    if len({f.spec.key for f in findings}) != len(findings):
        raise ValueError("watch_reason.findings: each finding needs its own project")
    return WatchReasonCase(job, findings)


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
    if s.get("reply_must_name") is not None:
        names = s["reply_must_name"]
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(n, str) and all(o.strip() for o in n.split("|")) for n in names)
        ):
            raise ValueError(
                "`reply_must_name`: a list of non-empty strings (alternatives split by |, none of them empty)"
            )
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
    if s.get("prior_turns") is not None:
        parse_prior_turns(s)
    if s.get("decided_cards") is not None:
        scenario_decided_cards(s)
    for key in ("expected_view_kinds", "forbidden_view_kinds"):
        if s.get(key) is None:
            continue
        kinds = s[key]
        if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) and k.strip() for k in kinds):
            raise ValueError(f"`{key}`: a list of non-empty strings")
        if scenario_surface(s) != SURFACE_ERPNEXT_PANEL:
            raise ValueError(f"`{key}`: only a scenario with surface: erpnext_panel opens views")
    if s.get("expected_report_filters") is not None:
        wanted = s["expected_report_filters"]
        if (
            not isinstance(wanted, dict)
            or not wanted
            or not all(isinstance(name, str) and name.strip() and _report_filters_ok(needed) for name, needed in wanted.items())
        ):
            raise ValueError(
                "`expected_report_filters`: a mapping of report name to a list of filter names, "
                "or to a mapping of filter name to its value (a string, or a list of strings it must hold)"
            )
        if scenario_surface(s) != SURFACE_ERPNEXT_PANEL:
            raise ValueError("`expected_report_filters`: only a scenario with surface: erpnext_panel opens views")
    for flag in ("demo_viewer", "forbid_principal_name", "forbid_repeated_text"):
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
    if s.get(WATCH_REASON) is not None:
        try:
            scenario_watch_reason(s)
        except ValueError as e:
            raise ValueError(f"`watch_reason`: {e}") from e

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
    if k == WATCH_REASON and s.get(WATCH_REASON) is None:
        raise ValueError("watch_reason scenarios require a `watch_reason` block")
    if k == "triage" and not s.get("event"):
        raise ValueError("triage scenarios require an `event` field")

    return s


def _report_filters_ok(needed: Any) -> bool:
    """A report's expected filters: a list of names, or names with values."""
    if isinstance(needed, list):
        return bool(needed) and all(isinstance(f, str) and f.strip() for f in needed)
    if isinstance(needed, dict):
        return bool(needed) and all(
            isinstance(f, str)
            and f.strip()
            and (
                (isinstance(v, str) and v.strip())
                or (isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v))
            )
            for f, v in needed.items()
        )
    return False


def _report_filters_met(needed: list[str] | dict[str, Any], filters: dict[str, Any]) -> bool:
    """Whether a view's filters hold every expected name, and each expected
    value: a string equal to the filter's, or a list whose every item is in
    the filter's list."""
    if isinstance(needed, list):
        return all(f in filters for f in needed)
    for name, value in needed.items():
        got = filters.get(name)
        if isinstance(value, list):
            if not isinstance(got, list) or not all(v in got for v in value):
                return False
        elif got != value:
            return False
    return True


def view_kinds_check(scenario: dict[str, Any], views: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Whether a panel turn opened the kinds of view the scenario expects
    beside the panel, from what ``erpnext_panel.note_erpnext_call`` kept (the
    show calls ERPNext accepted): deterministic, since the judge reads only the
    reply. ``None`` when the scenario sets none of ``expected_view_kinds``,
    ``forbidden_view_kinds`` and ``expected_report_filters``; otherwise
    ``{"kinds", "missing", "forbidden", "ok"}``, with ``"reports_unmet"`` (the
    reports never opened with their expected filters) when the scenario
    sets ``expected_report_filters``."""
    expected = [k.strip() for k in scenario.get("expected_view_kinds") or []]
    banned = [k.strip() for k in scenario.get("forbidden_view_kinds") or []]
    wanted = scenario.get("expected_report_filters") or {}
    if not expected and not banned and not wanted:
        return None
    kinds = sorted({str(v.get("kind")) for v in views if isinstance(v, dict) and v.get("kind")})
    missing = [k for k in expected if k not in kinds]
    forbidden = [k for k in banned if k in kinds]
    out: dict[str, Any] = {"kinds": kinds, "missing": missing, "forbidden": forbidden}
    ok = not missing and not forbidden
    if wanted:
        # A report the turn must open with these filters among its own, and
        # their values where given (oe_connector refuses a name the report
        # doesn't declare, so a refused show never counts).
        unmet = [
            name
            for name, needed in wanted.items()
            if not any(
                isinstance(v, dict)
                and v.get("kind") == "report"
                and v.get("report_name") == name
                and isinstance(v.get("filters"), dict)
                and _report_filters_met(needed, v["filters"])
                for v in views
            )
        ]
        out["reports_unmet"] = unmet
        ok = ok and not unmet
    out["ok"] = ok
    return out


def _says(word: str, reply: str) -> bool:
    import re

    return bool(word) and re.search(
        r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", reply, re.IGNORECASE
    ) is not None


def forbidden_words_in(scenario: dict[str, Any], reply: str) -> list[str]:
    """The scenario's own ``forbidden_reply_words`` the reply says, whole
    words and any case: a deterministic check the judge's score can't
    outvote. The install principal's name (``forbid_principal_name``) is
    ``principal_name_in``'s, reported apart."""
    words = [w.strip() for w in scenario.get("forbidden_reply_words") or []]
    return [word for word in words if _says(word, reply)]


# What a run records for a reply that names the principal: never the name
# itself, which the scores and results would otherwise carry.
PRINCIPAL_NAME_SAID = "the principal's name appeared in the reply"
# The same for a live prior turn's reply (runner.chat_checks), naming the
# turn by its place in ``prior_turns`` and, again, never the name.
LIVE_PRINCIPAL_NAME_SAID = "the principal's name appeared in live prior turn {n}'s reply"


def principal_name_in(scenario: dict[str, Any], reply: str) -> bool:
    """Whether the reply names the install's own principal (any of
    ``principal_name_variants``, as ``name_pattern`` matches them), for a
    ``forbid_principal_name`` scenario. Read at run time: no fixture can name
    the real person a demo must never reveal. Kept apart from the scenario's
    own words so a run that fails on a fixture name ("Rachel") never reads as
    a principal leak, and the other way round."""
    if scenario.get("forbid_principal_name") is not True:
        return False
    pattern = name_pattern(principal_name_variants())
    return pattern is not None and pattern.search(reply) is not None


class NameVariant(NamedTuple):
    """One way a reply may name a person: ``text``, matched as a whole word,
    in any case when ``any_case``, else only as written."""

    text: str
    any_case: bool


# Titles dropped from a name before it is split (with or without a dot).
_HONORIFICS = frozenset({"mr", "mrs", "ms", "mx", "dr", "prof"})
# Generational suffixes dropped from the end of a name; never a variant.
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
# A shorter name part (an initial, "Al") would match ordinary words.
_MIN_NAME_PART = 3
# An email local part is a name only when it joins parts ("o.owner"); a bare
# word ("principal", "ceo", "info") is an ordinary word in a reply.
_LOCAL_PART_SEPARATORS = (".", "_", "-")


def _is_suffix(token: str) -> bool:
    return token.strip(".,").casefold() in _SUFFIXES


def _is_credential(token: str) -> bool:
    """A suffix or a short credential after a name ("CPA", "MBA", "PhD")."""
    core = token.strip(".,")
    return _is_suffix(token) or (0 < len(core) <= 5 and sum(c.isupper() for c in core) >= 2)


def _name_tokens(full_name: str | None) -> list[str]:
    """``full_name``'s tokens, given name first: a parenthesised part ("(CEO)")
    and a comma-tail of credentials (", CPA") dropped, "Owner, Olivia" read
    as surname, given, honorifics dropped and trailing suffixes (Jr, III)
    taken off."""
    name = re.sub(r"\([^)]*\)", " ", full_name or "")
    head, *rest = (part.strip() for part in name.split(","))
    given_parts = [part for part in rest if part and not all(_is_credential(t) for t in part.split())]
    tokens = head.split()
    if len(tokens) == 1 and given_parts:
        tokens = given_parts[0].split() + tokens
    tokens = [t for t in tokens if t.rstrip(".").casefold() not in _HONORIFICS]
    while tokens and _is_suffix(tokens[-1]):
        tokens.pop()
    return [t for t in tokens if not _is_suffix(t)]


def _capitalised(token: str) -> str:
    return token[:1].upper() + token[1:]


def name_variants(full_name: str | None, *emails: str | None) -> list[NameVariant]:
    """The ways a reply may name a person, longest first. In any case: the
    full name, "Surname, Given", the given name (first token), each whole
    email address and each address's local part when it has a separator
    (``_LOCAL_PART_SEPARATORS``). Capitalised only: the surname (last token)
    and, when it is hyphenated, each of its parts, so "Owner approves" names
    Olivia Owner and "the account owner" doesn't. The name is read by
    ``_name_tokens`` (no honorific, suffix, credential or parenthesised
    part), and a part shorter than ``_MIN_NAME_PART`` characters is left
    out ("Al" alone, or "Owner, Al")."""
    found: dict[str, bool] = {}

    def add(text: str, any_case: bool) -> None:
        if len(text) >= _MIN_NAME_PART:
            found[text] = found.get(text, False) or any_case

    tokens = _name_tokens(full_name)
    if tokens:
        given, surname = tokens[0], tokens[-1]
        add(given, True)
        if len(tokens) > 1:
            add(" ".join(tokens), True)
            if len(given) >= _MIN_NAME_PART and len(surname) >= _MIN_NAME_PART:
                add(f"{surname}, {given}", True)
            add(_capitalised(surname), False)
            if "-" in surname:
                for part in surname.split("-"):
                    add(_capitalised(part), False)
    for email in emails:
        address = (email or "").strip()
        if not address:
            continue
        add(address, True)
        local = address.partition("@")[0]
        if any(sep in local for sep in _LOCAL_PART_SEPARATORS):
            add(local, True)
    return sorted((NameVariant(t, a) for t, a in found.items()), key=lambda v: (-len(v.text), v.text))


def name_pattern(variants: list[NameVariant]) -> re.Pattern[str] | None:
    """One pattern for ``variants``, each a whole word, longest first, in any
    case only where ``any_case``; None when there are none."""
    if not variants:
        return None
    alternatives = [
        f"(?i:{re.escape(v.text)})" if v.any_case else re.escape(v.text)
        for v in sorted(variants, key=lambda v: -len(v.text))
    ]
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alternatives) + r")(?![A-Za-z0-9])")


def _principal_names_and_addresses() -> tuple[list[str], list[str]]:
    from openexecutive.people.demo import principal_names_and_addresses

    return principal_names_and_addresses()


def principal_name_variants() -> list[NameVariant]:
    """Every principal row's ``name_variants`` (archived and suspended rows
    too, every address and alias), read from the roster at run time, merged
    with what the demo guard hides (``people.demo.principal_name_words``:
    each whole address and full name, in any case), so the check holds a
    reply to at least that guard. The guard's raw first token is read by
    ``_name_tokens`` instead, so an honorific ("Dr.") or a surname before a
    comma ("Owner,") never becomes a word of its own. ``principal_name_in``
    checks for them and ``scrub_principal_name`` replaces them. Empty
    without a principal; raises when the roster can't be read."""
    from openexecutive.people.demo import principal_name_words

    names, addresses = _principal_names_and_addresses()
    found: dict[str, bool] = {}
    variants = [v for name in names for v in name_variants(name)] + name_variants(None, *addresses)
    variants += [
        NameVariant(word, True)
        for word in principal_name_words(names, addresses)
        if (" " in word or "@" in word) and len(word) >= _MIN_NAME_PART
    ]
    for v in variants:
        found[v.text] = found.get(v.text, False) or v.any_case
    return sorted((NameVariant(t, a) for t, a in found.items()), key=lambda v: (-len(v.text), v.text))


# What a stored eval result says in place of the principal's name.
PRINCIPAL_SCRUBBED = "[the principal]"


def scrub_principal_name(value: Any, names: list[NameVariant] | None = None) -> Any:
    """``value`` with every mention of the principal's name (``names``, else
    ``principal_name_variants()``, as ``name_pattern`` matches them) replaced
    by ``PRINCIPAL_SCRUBBED``: a string, or a copy of a dict or list with
    each string inside it scrubbed (a judge's scores, a result's live turns).
    Anything else is returned as it is. Keeps the principal's name out of
    what an eval run stores, whatever the scenario's flags; the checks read
    the reply before it is scrubbed."""
    pattern = name_pattern(principal_name_variants() if names is None else names)
    if pattern is None:
        return value

    def scrub(item: Any) -> Any:
        if isinstance(item, str):
            return pattern.sub(PRINCIPAL_SCRUBBED, item)
        if isinstance(item, dict):
            return {key: scrub(v) for key, v in item.items()}
        if isinstance(item, list):
            return [scrub(v) for v in item]
        return item

    return scrub(value)


def _plain(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text).strip().casefold()


def reply_must_name_missing(scenario: dict[str, Any], reply: str) -> list[str]:
    """The scenario's ``reply_must_name`` items the reply leaves out: each is
    there when it, or one of its ``|``-separated alternatives ("Panel
    wiring|DEMO-WIR-25"), appears anywhere in the reply, in any case and
    spacing. A deterministic check the judge's score can't outvote: a table
    that drops one line of four fails, however good the rest reads."""
    said = _plain(reply)
    missing = []
    for item in scenario.get("reply_must_name") or []:
        options = [_plain(o) for o in str(item).split("|") if o.strip()]
        if options and not any(o in said for o in options):
            missing.append(str(item))
    return missing


def reply_check_failures(scenario: dict[str, Any], reply: str, *, repeated: bool = True) -> dict[str, list[str]]:
    """The deterministic checks every runner holds a reply to, whatever the
    judge's score, keyed as they are recorded in the run's scores: a
    ``forbidden_reply_words`` word it says, the principal's name under
    ``principal_name`` (with ``forbid_principal_name``; the name itself is
    never recorded), a sentence it says twice (with
    ``forbid_repeated_text``; ``repeated=False`` for text that isn't one
    reply, such as a watch notice's lines) and a ``reply_must_name`` item it
    leaves out. Empty when the reply passes them all."""
    from openexecutive.utils.repeats import repeated_sentences

    out: dict[str, list[str]] = {}
    said = forbidden_words_in(scenario, reply)
    if said:
        out["forbidden_reply_words"] = said
    if principal_name_in(scenario, reply):
        out["principal_name"] = [PRINCIPAL_NAME_SAID]
    if repeated and scenario.get("forbid_repeated_text") is True:
        again = repeated_sentences(reply)
        if again:
            out["repeated_text"] = again
    missing = reply_must_name_missing(scenario, reply)
    if missing:
        out["reply_must_name_missing"] = missing
    return out
