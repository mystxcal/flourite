from __future__ import annotations

import os
import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from frontier_harness.control import (
    CommandKind,
    CommandStatus,
    RunControlPlane,
    RuntimeStatus,
)
from frontier_harness.live import KernelLiveDashboard
from frontier_harness.presentation import FLOURITE_THEME
from frontier_harness.runtime.engine import KernelEngine


def test_control_inbox_is_immutable_and_receipts_are_separate(tmp_path: Path) -> None:
    control = RunControlPlane(tmp_path / "control.sqlite3", "run_test")
    try:
        command = control.enqueue(CommandKind.STEER, text="Recheck the decisive assumption.")
        assert command.status == CommandStatus.QUEUED

        connection = sqlite3.connect(control.path)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "UPDATE commands SET text='changed' WHERE command_id=?",
                    (command.command_id,),
                )
        finally:
            connection.close()

        control.mark_command(command.command_id, CommandStatus.APPLIED, "admitted")
        observed = control.commands()[0]
        assert observed.status == CommandStatus.APPLIED
        assert observed.text == "Recheck the decisive assumption."
        assert control.commands(pending_only=True) == []

        control.set_runtime(
            status=RuntimeStatus.SEALING,
            phase="release",
            pid=os.getpid(),
            detail="sealing",
        )
        with pytest.raises(ValueError, match="sealing"):
            control.enqueue(CommandKind.STEER, text="Too late.")
    finally:
        control.close()


def test_runtime_and_activity_are_bounded_transient_state(tmp_path: Path) -> None:
    control = RunControlPlane(tmp_path / "control.sqlite3", "run_test")
    try:
        runtime = control.set_runtime(
            status=RuntimeStatus.RUNNING,
            phase="active",
            pid=os.getpid(),
            detail="controller boundary",
        )
        assert runtime.process_alive
        for index in range(control.MAX_ACTIVITY_ROWS + 9):
            control.record_activity(kind="test", label="tick", message=str(index))
        rows = control.recent_activity(limit=control.MAX_ACTIVITY_ROWS)
        assert len(rows) == control.MAX_ACTIVITY_ROWS
        assert rows[0].message == "9"
        assert rows[-1].message == str(control.MAX_ACTIVITY_ROWS + 8)
    finally:
        control.close()


def _dashboard(
    engine: KernelEngine, *, width: int, height: int
) -> tuple[KernelLiveDashboard, Console, StringIO]:
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        height=height,
        force_terminal=False,
        color_system=None,
        no_color=True,
        theme=FLOURITE_THEME,
    )
    return (
        KernelLiveDashboard(run_dir=engine.run_dir, control=engine.control, console=console),
        console,
        output,
    )


def test_live_dashboard_renders_state_activity_and_controls(tmp_path: Path, fake_config) -> None:
    engine = KernelEngine.create(
        "Render the live operator surface.",
        config=fake_config(),
        adapter_name="generic",
    )
    try:
        engine.control.record_activity(
            kind="model", label="bootstrap", message="model call started"
        )
        engine.control.enqueue(CommandKind.STEER, text="Retain the decisive evidence.")
        dashboard, console, output = _dashboard(engine, width=140, height=42)
        console.print(dashboard.render())
        rendered = output.getvalue()
        assert "RUN STATE" in rendered
        assert "DECISION FRONTIER" in rendered
        assert "LIVE ACTIVITY" in rendered
        assert "OPERATOR QUEUE" in rendered
        assert "Retain the decisive" in rendered
    finally:
        engine.close()


def test_live_dashboard_preserves_history_position(tmp_path: Path, fake_config) -> None:
    engine = KernelEngine.create(
        "Keep the live viewport usable.",
        config=fake_config(),
        adapter_name="generic",
    )
    try:
        for index in range(40):
            engine.control.record_activity(
                kind="tool",
                label=f"step-{index}",
                message=f"activity {index}",
            )
        dashboard, console, _ = _dashboard(engine, width=97, height=41)
        lines = console.render_lines(
            dashboard.render(),
            console.options.update(height=None),
            pad=False,
        )
        assert len(lines) <= console.height

        dashboard._scroll_activity(-dashboard.activity_page_size)
        cursor = dashboard.activity_cursor
        assert cursor is not None
        engine.control.record_activity(kind="tool", label="new-live-step", message="new")
        dashboard.render()
        assert dashboard.activity_cursor == cursor
    finally:
        engine.close()


def test_live_dashboard_exposes_failed_controller_detail(tmp_path: Path, fake_config) -> None:
    engine = KernelEngine.create(
        "Expose a recoverable failure.",
        config=fake_config(),
        adapter_name="generic",
    )
    try:
        runtime = engine.control.set_runtime(
            status=RuntimeStatus.FAILED,
            phase="bootstrapping",
            detail="controller error",
        )
        dashboard, console, output = _dashboard(engine, width=100, height=30)
        assert dashboard._effective_status(engine.state, runtime) == "failed"
        console.print(dashboard._summary(engine.state, runtime))
        assert "controller error" in output.getvalue()
    finally:
        engine.close()
