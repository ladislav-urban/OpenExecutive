"""docker/docker-compose.yml keeps the web app's secrets off the API.

The api service loads the whole repo-root .env, which also holds the ui
service's secrets. Each one the UI reads that would let its holder act as a
user (the caller-signing key, the session secret, the sign-in clients'
secrets) must be shadowed empty in the api service's `environment:`, which
wins over its env_file.
"""
from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[4] / "docker" / "docker-compose.yml"
UI_ONLY_SECRETS = ("CALLER_ASSERTION_PRIVATE_KEY", "AUTH_SECRET", "AUTH_GOOGLE_SECRET", "AUTH_OIDC_SECRET")


def _environment(service: dict) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in service.get("environment", []))


def test_the_api_never_gets_the_web_apps_secrets() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    api, ui = _environment(services["api"]), _environment(services["ui"])
    for name in UI_ONLY_SECRETS:
        assert name in ui, f"{name} is no longer the UI's; update this list"
        assert api.get(name) == "", f"{name} reaches the API through its env_file"


def test_the_api_reads_none_of_them() -> None:
    # Shadowing one the API needed would break it quietly: none may be read.
    source = Path(__file__).resolve().parents[2] / "openexecutive"
    for path in source.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in UI_ONLY_SECRETS:
            assert f'"{name}"' not in text, f"{path} reads {name}"
