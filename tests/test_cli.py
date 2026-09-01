from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

import frontier_harness.cli as cli
from frontier_harness.cli import app
from frontier_harness.config import HarnessConfig, load_config

runner = CliRunner()


def test_unmetered_dollar_envelope_is_rejected() -> None:
    with pytest.raises(ValueError, match="authoritative monetary cost"):
        HarnessConfig.model_validate({"kernel": {"max_cost_usd": 1}})


def test_failed_engine_returns_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Engine:
        run_dir = tmp_path / "run"

        def close(self) -> None:
            pass

    async def execute(_engine: object, _output: object, *, follow: bool):
        assert follow is False
        return SimpleNamespace(status=SimpleNamespace(value="failed")), None

    monkeypatch.setattr(cli, "_execute_with_activity", execute)
    with pytest.raises(typer.Exit) as failure:
        cli._run_engine(Engine(), None, quiet=True)  # type: ignore[arg-type]
    assert failure.value.exit_code == 1


def test_init_and_doctor_fake(tmp_path: Path) -> None:
    config = tmp_path / "flourite.toml"
    result = runner.invoke(app, ["init", str(config)])
    assert result.exit_code == 0, result.output
    loaded = load_config(config)
    assert loaded.provider.kind == "omp-codex"
    assert loaded.provider.command == "omp"

    result = runner.invoke(app, ["doctor", "--fake"])
    assert result.exit_code == 0, result.output
    assert "offline" in result.output


def test_flourite_identity_and_terminal_language(tmp_path: Path) -> None:
    version = runner.invoke(app, ["--version"])
    assert version.exit_code == 0, version.output
    assert version.output.strip() == "flourite 0.6.0"

    run_root = tmp_path / "runs"
    result = runner.invoke(
        app,
        ["run", "Render the product shell.", "--fake", "--run-root", str(run_root)],
    )
    assert result.exit_code == 0, result.output
    assert "FLOURITE" in result.output
    assert "OFFLINE RUN" in result.output
    assert "SATISFIED" in result.output

    run_id = next(item.name for item in run_root.iterdir() if item.is_dir())
    status = runner.invoke(app, ["status", run_id, "--run-root", str(run_root), "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["status"] == "satisfied"


def test_cli_fake_run_status_verify_export_and_inspect(tmp_path: Path) -> None:
    run_root = tmp_path / "runs"
    output = tmp_path / "answer.md"
    result = runner.invoke(
        app,
        [
            "run",
            "Build a useful artifact.",
            "--fake",
            "--run-root",
            str(run_root),
            "--output",
            str(output),
            "--quiet",
        ],
    )
    assert result.exit_code == 0, result.output
    assert output.exists()
    run_id = next(item.name for item in run_root.iterdir() if item.is_dir())

    assert runner.invoke(app, ["status", run_id, "--run-root", str(run_root)]).exit_code == 0
    verify = runner.invoke(app, ["verify", run_id, "--run-root", str(run_root)])
    assert verify.exit_code == 0, verify.output
    assert '"state": "verified"' in verify.output

    archive = tmp_path / "diagnostic.zip"
    exported = runner.invoke(
        app,
        ["export", run_id, "--run-root", str(run_root), "--output", str(archive)],
    )
    assert exported.exit_code == 0, exported.output
    assert archive.exists()

    inspected = runner.invoke(app, ["inspect", run_id, "--run-root", str(run_root)])
    assert inspected.exit_code == 0, inspected.output
    assert "TRAJECTORIES" in inspected.output
    assert "MOVES" in inspected.output
