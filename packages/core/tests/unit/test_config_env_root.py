"""Where config finds the .env (config._env_root): never above the checkout."""
from __future__ import annotations

from pathlib import Path

from openexecutive.config import _env_root


def _tree(tmp_path: Path) -> Path:
    here = tmp_path / "runner" / "_work" / "repo" / "packages" / "core" / "openexecutive"
    here.mkdir(parents=True)
    return here


def test_the_repo_roots_env_is_found(tmp_path: Path) -> None:
    here = _tree(tmp_path)
    repo = tmp_path / "runner" / "_work" / "repo"
    (repo / ".git").mkdir()
    (repo / ".env").write_text("X=1\n")
    assert _env_root(here) == repo


def test_an_env_above_the_checkout_is_ignored(tmp_path: Path) -> None:
    # A self-hosted CI runner keeps its own .env in its root, above _work.
    here = _tree(tmp_path)
    (tmp_path / "runner" / "_work" / "repo" / ".git").mkdir()
    (tmp_path / "runner" / ".env").write_text("LANG=C.UTF-8\n")
    assert _env_root(here) is None


def test_a_worktrees_git_file_is_its_root(tmp_path: Path) -> None:
    here = _tree(tmp_path)
    repo = tmp_path / "runner" / "_work" / "repo"
    (repo / ".git").write_text("gitdir: /elsewhere\n")
    (tmp_path / "runner" / ".env").write_text("LANG=C.UTF-8\n")
    assert _env_root(here) is None


def test_without_a_checkout_the_walk_goes_up(tmp_path: Path) -> None:
    # An image without .git, as before: the nearest .env up the tree.
    here = _tree(tmp_path)
    (tmp_path / "runner" / ".env").write_text("X=1\n")
    assert _env_root(here) == tmp_path / "runner"
