from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from frontier_harness.config import HarnessConfig


@pytest.fixture
def fake_config(tmp_path: Path):
    def factory(**overrides: Any) -> HarnessConfig:
        base: dict[str, Any] = {
            "run": {
                "run_root": str(tmp_path / "runs"),
                "budget": {
                    "max_rounds": 3,
                    "max_calls": 10,
                    "max_parallel": 2,
                    "synthesis_reserve_calls": 3,
                },
            },
            "provider": {"kind": "fake"},
            # Preserve the compact, exact call traces expected by the legacy
            # engine tests. Adaptive allocation has focused coverage of its own.
            "resource": {"mode": "static"},
            "frontier": {"max_actions_per_batch": 2},
        }
        for section, value in overrides.items():
            if isinstance(value, dict) and isinstance(base.get(section), dict):
                base[section].update(value)
            else:
                base[section] = value
        return HarnessConfig.model_validate(base)

    return factory


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=check,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-m", "initial")
    return repo
