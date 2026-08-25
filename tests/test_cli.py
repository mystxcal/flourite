from __future__ import annotations

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from frontier_harness.cli import app
from frontier_harness.config import load_config
from frontier_harness.control import CommandKind
from frontier_harness.engine import FrontierEngine

runner = CliRunner()


def test_init_and_doctor_fake(tmp_path: Path) -> None:
    config = tmp_path / "flourite.toml"
    result = runner.invoke(app, ["init", str(config)])
    assert result.exit_code == 0, result.output
    assert config.exists()
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
    assert "F L O U R I T E" in result.output
    assert "◇ ORIENT" in result.output
    assert "◆ SEALED" in result.output

    run_id = next(item.name for item in run_root.iterdir() if item.is_dir())
    status = runner.invoke(
        app,
        ["status", run_id, "--run-root", str(run_root), "--json"],
    )
    assert status.exit_code == 0, status.output
    assert "FLOURITE" not in status.output
    assert json.loads(status.output)["phase"] == "complete"


def test_cli_fake_run_status_verify_and_export(tmp_path: Path) -> None:
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
    assert "FLOURITE" not in result.output
    assert output.exists()
    run_dirs = [item for item in run_root.iterdir() if item.is_dir()]
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name

    status = runner.invoke(app, ["status", run_id, "--run-root", str(run_root)])
    assert status.exit_code == 0, status.output
    assert "complete" in status.output

    verify = runner.invoke(app, ["verify", run_id, "--run-root", str(run_root)])
    assert verify.exit_code == 0, verify.output
    assert '"sealed": true' in verify.output

    archive = tmp_path / "diagnostic.zip"
    exported = runner.invoke(
        app,
        [
            "export",
            run_id,
            "--run-root",
            str(run_root),
            "--output",
            str(archive),
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert archive.exists()


def test_evolution_check_surfaces_a_withheld_promotion(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.json"
    trials = tmp_path / "trials.json"
    candidate.write_text(
        json.dumps(
            {
                "candidate_id": "cand",
                "baseline_fingerprint": "base",
                "candidate_fingerprint": "next",
                "failure_mode": "repeated stagnation",
                "causal_change": "trigger mutation from runtime stalls",
                "predicted_effect": "fewer repeated attempts",
                "prediction_scope": "software",
            }
        ),
        encoding="utf-8",
    )
    trials.write_text("[]", encoding="utf-8")
    result = runner.invoke(
        app,
        ["evolution-check", str(candidate), str(trials), "--json"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["promotable"] is False
    assert "insufficient distinct held-out cases" in payload["reasons"]


def test_cli_steers_a_resting_run_and_rejects_dead_runtime_controls(
    tmp_path: Path, fake_config
) -> None:
    engine = FrontierEngine.create("Build exactly this.", config=fake_config())
    run_dir = engine.run_dir
    try:
        steered = runner.invoke(app, ["steer", str(run_dir), "Retain the hard edge case."])
        assert steered.exit_code == 0, steered.output
        queued = engine.control.commands(pending_only=True)
        assert len(queued) == 1
        assert queued[0].kind == CommandKind.STEER
        assert queued[0].text == "Retain the hard edge case."

        paused = runner.invoke(app, ["pause", str(run_dir)])
        assert paused.exit_code != 0
        assert "No live controller" in paused.output
        stopped = runner.invoke(app, ["stop", str(run_dir)])
        assert stopped.exit_code != 0
        assert "No live controller" in stopped.output
    finally:
        engine.close()


def test_cli_rejects_steering_a_completed_run(tmp_path: Path, fake_config) -> None:
    engine = FrontierEngine.create("Finish before steering.", config=fake_config())
    run_dir = engine.run_dir
    try:
        asyncio.run(engine.execute())
    finally:
        engine.close()
    result = runner.invoke(app, ["steer", str(run_dir), "Too late."])
    assert result.exit_code != 0
    assert "run is complete" in result.output
