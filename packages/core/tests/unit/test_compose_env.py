"""docker/docker-compose.yml keeps the web app's secrets off the API.

The api service loads the whole repo-root .env, which also holds the ui
service's secrets. Each one the UI reads that would let its holder act as a
user (the caller-signing key, the session secret, the sign-in clients'
secrets) must be shadowed empty in the api service's `environment:`, which
wins over its env_file.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[4] / "docker" / "docker-compose.yml"
# Secret-looking names the ui service sets that the API needs too.
SHARED = {"BACKEND_SHARED_SECRET"}
SECRET_NAME = re.compile(r"_(SECRET|PRIVATE_KEY|TOKEN)$")


def _environment(service: dict) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in service.get("environment", []))


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def ui_only_secrets() -> set[str]:
    """Read from the ui service, so a secret added there is covered at once
    (*_SECRET, *_PRIVATE_KEY, *_TOKEN)."""
    return {name for name in _environment(_services()["ui"]) if SECRET_NAME.search(name)} - SHARED


def test_the_api_never_gets_the_web_apps_secrets() -> None:
    secrets = ui_only_secrets()
    assert {"CALLER_ASSERTION_PRIVATE_KEY", "AUTH_SECRET", "AUTH_GOOGLE_SECRET", "AUTH_OIDC_SECRET"} <= secrets
    api = _environment(_services()["api"])
    for name in sorted(secrets):
        assert api.get(name) == "", f"{name} reaches the API through its env_file: shadow it empty there"


def test_the_api_reads_none_of_them() -> None:
    # Shadowing one the API needed would break it quietly: none may be read.
    # A read names the variable alone in quotes, either kind: os.getenv('X'),
    # environ["X"], Field(alias="X"). Messages that mention it don't count.
    source = Path(__file__).resolve().parents[2] / "openexecutive"
    reads = {name: re.compile(rf"""['"]{name}['"]""") for name in ui_only_secrets()}
    for path in source.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name, read in reads.items():
            assert not read.search(text), f"{path} reads {name}"


def test_no_setting_reads_one_either() -> None:
    # Settings is a pydantic BaseSettings: a field reads the variable named
    # after it (case-insensitively) or its alias, with no quoted name in sight.
    from openexecutive.config import Settings

    secrets = ui_only_secrets()
    for name, field in Settings.model_fields.items():
        names = {name, field.alias, field.validation_alias if isinstance(field.validation_alias, str) else None}
        read = {n.upper() for n in names if n} & secrets
        assert not read, f"Settings.{name} reads {read}"
