"""The backend's own user-facing text names no product: what the API answers
(``api/routes``), Act as me's messages (``delegation``), Home's and the
brief's own lines, the workflows' reports, the chat bots, the CLI, the MCP
server (and its tools' descriptions) and the app-control tools say the
install's name (``utils.product_name``) or nothing,
never "the Executive", "your Executive" or "Open Executive", which a
Dynocortex install shows its people as the wrong name (the guide's lint,
``test_guide_prebuilt``, holds the User Guide the same way).

String literals only, docstrings excepted. A model's prompt elsewhere may
still say "the Executive": it is never shown."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import openexecutive

OLD_NAME = re.compile(r"\b(?:[Tt]he|[Yy]our) Executive\b|\bOpen Executive\b")
PACKAGE = Path(openexecutive.__file__).parent
# Workflows whose "the Executive" / "Open Executive" is in a model's prompt only.
_PROMPT_ONLY_WORKFLOWS = frozenset({
    "designer", "end_of_day_digest", "executive_reflection", "executive_research", "morning_brief",
})
USER_FACING = (
    *sorted((PACKAGE / "api" / "routes").rglob("*.py")),
    *sorted((PACKAGE / "delegation").rglob("*.py")),
    PACKAGE / "briefing" / "home_rules.py",
    PACKAGE / "briefing" / "value_summary.py",
    PACKAGE / "briefing" / "brief_state.py",
    # The reports workflows write (their footers), the chat bots, the CLI,
    # the MCP server and the app-control tools. The workflows below are
    # model prompts only, never shown.
    *sorted(p for p in (PACKAGE / "workflows").glob("*.py") if p.stem not in _PROMPT_ONLY_WORKFLOWS),
    PACKAGE / "integrations" / "slack_bot.py",
    PACKAGE / "integrations" / "discord_bot.py",
    PACKAGE / "cli.py",
    PACKAGE / "mcp_server" / "server.py",
    PACKAGE / "orchestrator" / "app_control.py",
)


def _old_names(source: str, where: str) -> list[str]:
    """Each string literal in ``source`` (not a docstring) naming the old product."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        f"{where}:{node.lineno}: {node.value[:80]!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        and OLD_NAME.search(node.value)
    ]


def test_the_backends_user_facing_text_names_no_product() -> None:
    assert len(USER_FACING) > 40
    found = [
        line for path in USER_FACING
        for line in _old_names(path.read_text(encoding="utf-8"), str(path.relative_to(PACKAGE)))
    ]
    assert found == []


def test_the_scan_sees_an_old_name_outside_a_docstring() -> None:
    source = (
        '"""The Executive, in a docstring."""\n'
        'def f() -> str:\n    """Your Executive, also a docstring."""\n    return "by the Executive"\n'
        'X = f"What {1} Open Executive did"\nY = "the executive branch"\n'
    )
    assert [line.split(": ", 1)[1] for line in _old_names(source, "s")] == ["'by the Executive'", "' Open Executive did'"]


def test_the_mcp_servers_tool_descriptions_name_no_product() -> None:
    # An MCP client shows each tool's docstring as its description.
    tree = ast.parse((PACKAGE / "mcp_server" / "server.py").read_text(encoding="utf-8"))
    tools = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
             and any("mcp.tool" in ast.unparse(d) for d in node.decorator_list)]
    assert len(tools) >= 4
    for tool in tools:
        assert not OLD_NAME.search(ast.get_docstring(tool) or ""), tool.name
