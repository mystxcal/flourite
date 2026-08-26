from __future__ import annotations

import asyncio
import os
import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from frontier_harness import events as et
from frontier_harness.control import (
    CommandKind,
    CommandStatus,
    RunControlPlane,
    RuntimeStatus,
)
from frontier_harness.engine import FrontierEngine
from frontier_harness.errors import OperatorStop
from frontier_harness.live import LiveDashboard
from frontier_harness.models import WorkerEnvelope
from frontier_harness.presentation import FLOURITE_THEME
from frontier_harness.providers.fake import FakeProvider


class WorkerGate(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.worker_started = asyncio.Event()
        self.release_worker = asyncio.Event()

    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.response_model is WorkerEnvelope:
            self.worker_started.set()
            await self.release_worker.wait()
        return await super().run(request)


class ActivityFake(FakeProvider):
    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.activity_callback is not None:
            request.activity_callback(
                {
                    "type": "tool_execution_start",
                    "toolName": "bash",
                    "intent": "inspect the candidate",
                    "arguments": {"sha256": "a" * 64, "keys": ["command"]},
                }
            )
            request.activity_callback(
                {
                    "type": "message_end",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "PRIVATE_THOUGHT"},
                            {"type": "text", "text": "PRIVATE_RESPONSE"},
                        ],
                        "usage": {"input": 2, "output": 1},
                        "stopReason": "stop",
                    },
                }
            )
        return await super().run(request)


class NestedActivityFake(FakeProvider):
    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.activity_callback is not None:
            request.activity_callback(
                {
                    "type": "message_end",
                    "message": {
                        "content": [
                            {
                                "type": "toolCall",
                                "name": "task",
                                "arguments": {
                                    "sha256": "a" * 64,
                                    "keys": ["tasks"],
                                    "task_names": ["ArtDirection", "TruthResearch"],
                                },
                            }
                        ],
                        "usage": {},
                        "stopReason": "toolUse",
                    },
                }
            )
            request.activity_callback(
                {
                    "type": "subagent_activity",
                    "agent": "ArtDirection",
                    "state": "done",
                    "message": "completed assigned work",
                }
            )
        return await super().run(request)


def test_control_inbox_is_immutable_and_receipts_are_separate(tmp_path: Path) -> None:
    control = RunControlPlane(tmp_path / "control.sqlite3", "run_test")
    try:
        command = control.enqueue(CommandKind.STEER, text="Recheck the decisive assumption.")
        assert command.status == CommandStatus.QUEUED
        assert control.commands(pending_only=True)[0].text == command.text

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
            control.record_activity(
                kind="test",
                label="tick",
                message=str(index),
            )
        rows = control.recent_activity(limit=control.MAX_ACTIVITY_ROWS)
        assert len(rows) == control.MAX_ACTIVITY_ROWS
        assert rows[0].message == "9"
        assert rows[-1].message == str(control.MAX_ACTIVITY_ROWS + 8)
    finally:
        control.close()


def test_steer_is_admitted_once_after_inflight_work(tmp_path: Path, fake_config) -> None:
    provider = WorkerGate()
    engine = FrontierEngine.create(
        "Build the best exact answer.",
        config=fake_config(),
        provider=provider,
    )

    async def scenario() -> Path:
        task = asyncio.create_task(engine.execute())
        await asyncio.wait_for(provider.worker_started.wait(), timeout=2)
        command = engine.control.enqueue(
            CommandKind.STEER,
            text="Make the final comparison explicit and evidence-backed.",
        )
        assert engine.control.commands(pending_only=True)[0].command_id == command.command_id
        provider.release_worker.set()
        output = await asyncio.wait_for(task, timeout=5)
        assert engine.control.commands()[0].status == CommandStatus.APPLIED
        return output

    try:
        assert asyncio.run(scenario()).is_file()
        amendments = engine.state.task_source.amendments if engine.state.task_source else []
        assert engine.state.task_source is not None
        assert engine.state.task_source.original_text == "Build the best exact answer."
        assert [item.text for item in amendments] == [
            "Make the final comparison explicit and evidence-backed."
        ]
        events = engine.events()
        amended = [item for item in events if item.event_type == et.TASK_SOURCE_AMENDED]
        assert len(amended) == 1
        action_done_seq = max(
            item.seq for item in events if item.event_type == et.ACTION_COMPLETED
        )
        assert amended[0].seq > action_done_seq
        assert engine.state.metadata.get("steering_replan_pending") is None
    finally:
        engine.close()


def test_pause_accepts_steer_then_resumes_without_losing_state(
    tmp_path: Path, fake_config
) -> None:
    provider = WorkerGate()
    engine = FrontierEngine.create(
        "Build an exact resumable answer.",
        config=fake_config(),
        provider=provider,
    )

    async def wait_for_status(status: RuntimeStatus) -> None:
        for _ in range(100):
            if engine.control.runtime().status == status:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"runtime never reached {status}")

    async def scenario() -> Path:
        task = asyncio.create_task(engine.execute())
        await asyncio.wait_for(provider.worker_started.wait(), timeout=2)
        engine.control.enqueue(CommandKind.PAUSE)
        provider.release_worker.set()
        await wait_for_status(RuntimeStatus.PAUSED)
        assert not task.done()
        engine.control.enqueue(CommandKind.STEER, text="Preserve the strongest counterexample.")
        engine.control.enqueue(CommandKind.RESUME)
        return await asyncio.wait_for(task, timeout=5)

    try:
        assert asyncio.run(scenario()).is_file()
        event_types = [item.event_type for item in engine.events()]
        pause_index = event_types.index(et.RUN_PAUSED)
        steer_index = event_types.index(et.TASK_SOURCE_AMENDED)
        resume_index = event_types.index(et.RUN_RESUMED)
        assert pause_index < steer_index < resume_index
        assert engine.control.runtime().status == RuntimeStatus.COMPLETE
    finally:
        engine.close()


def test_stop_is_resumable_and_new_controller_records_resume(tmp_path: Path, fake_config) -> None:
    provider = WorkerGate()
    engine = FrontierEngine.create(
        "Stop and resume without losing work.",
        config=fake_config(),
        provider=provider,
    )
    run_dir = engine.run_dir

    async def scenario() -> None:
        task = asyncio.create_task(engine.execute())
        await asyncio.wait_for(provider.worker_started.wait(), timeout=2)
        engine.control.enqueue(CommandKind.STOP)
        provider.release_worker.set()
        with pytest.raises(OperatorStop):
            await asyncio.wait_for(task, timeout=5)

    try:
        asyncio.run(scenario())
        assert engine.state.phase.value == "active"
        assert engine.control.runtime().status == RuntimeStatus.STOPPED
    finally:
        engine.close()

    resumed = FrontierEngine.load(run_dir, provider=FakeProvider())
    try:
        assert asyncio.run(resumed.execute()).is_file()
        event_types = [item.event_type for item in resumed.events()]
        assert et.RUN_STOPPED in event_types
        assert et.RUN_RESUMED in event_types
        assert event_types.index(et.RUN_STOPPED) < event_types.index(et.RUN_RESUMED)
    finally:
        resumed.close()


def test_control_receipt_reconciles_after_post_append_crash(tmp_path: Path, fake_config) -> None:
    engine = FrontierEngine.create("Preserve command idempotency.", config=fake_config())
    try:
        command = engine.control.enqueue(CommandKind.STEER, text="Keep the decisive evidence.")
        # Simulate a process ending after the authoritative ledger append but
        # before the sidecar receipt was updated.
        engine._admit_steering(command.command_id, command.text)
        assert engine.control.commands()[0].status == CommandStatus.QUEUED
        assert asyncio.run(engine._control_boundary()) is False
        assert engine.control.commands()[0].status == CommandStatus.APPLIED
        assert len(
            [item for item in engine.events() if item.event_type == et.TASK_SOURCE_AMENDED]
        ) == 1
    finally:
        engine.close()


def test_live_projection_never_persists_raw_model_content(tmp_path: Path, fake_config) -> None:
    engine = FrontierEngine.create(
        "Keep observability sanitized.",
        config=fake_config(),
        provider=ActivityFake(),
    )
    try:
        asyncio.run(engine.execute())
        rows = engine.control.recent_activity(limit=engine.control.MAX_ACTIVITY_ROWS)
        serialized = "\n".join(item.model_dump_json() for item in rows)
        assert "inspect the candidate" in serialized
        assert "PRIVATE_THOUGHT" not in serialized
        assert "PRIVATE_RESPONSE" not in serialized
    finally:
        engine.close()


def test_nested_provider_work_is_projected_as_subagent_activity(
    tmp_path: Path, fake_config
) -> None:
    engine = FrontierEngine.create(
        "Expose nested progress safely.",
        config=fake_config(),
        provider=NestedActivityFake(),
    )
    try:
        asyncio.run(engine.execute())
        subagent_rows = [
            item
            for item in engine.control.recent_activity(
                limit=engine.control.MAX_ACTIVITY_ROWS
            )
            if item.kind == "subagent"
        ]
        assert [(item.label, item.state, item.message) for item in subagent_rows[:3]] == [
            ("ArtDirection", "active", "started assigned work"),
            ("TruthResearch", "active", "started assigned work"),
            ("ArtDirection", "done", "completed assigned work"),
        ]
    finally:
        engine.close()


def test_live_dashboard_renders_dense_state_activity_and_controls(
    tmp_path: Path, fake_config
) -> None:
    engine = FrontierEngine.create("Render the live operator surface.", config=fake_config())
    output = StringIO()
    console = Console(
        file=output,
        width=140,
        height=42,
        force_terminal=False,
        color_system=None,
        no_color=True,
        theme=FLOURITE_THEME,
    )
    try:
        engine.control.record_activity(
            kind="model",
            label="bootstrap",
            message="model call started",
        )
        engine.control.enqueue(CommandKind.STEER, text="Retain the decisive evidence.")
        dashboard = LiveDashboard(
            run_dir=engine.run_dir,
            control=engine.control,
            console=console,
        )
        console.print(dashboard.render())
        rendered = output.getvalue()
        assert "RUN STATE" in rendered
        assert "DECISION FRONTIER" in rendered
        assert "LIVE ACTIVITY" in rendered
        assert "OPERATOR QUEUE" in rendered
        assert "pause/resume" in rendered
        assert "Retain the decisive" in rendered
        assert "evidence." in rendered
    finally:
        engine.close()


def test_live_dashboard_fits_a_narrow_pane_and_preserves_history_position(
    tmp_path: Path, fake_config
) -> None:
    engine = FrontierEngine.create("Keep the live viewport usable.", config=fake_config())
    output = StringIO()
    console = Console(
        file=output,
        width=97,
        height=41,
        force_terminal=True,
        color_system=None,
        no_color=True,
        theme=FLOURITE_THEME,
    )
    try:
        for index in range(40):
            engine.control.record_activity(
                kind="tool",
                label=f"step-{index}",
                message=f"activity {index}",
            )
        dashboard = LiveDashboard(
            run_dir=engine.run_dir,
            control=engine.control,
            console=console,
        )
        rendered = dashboard.render()
        lines = console.render_lines(
            rendered,
            console.options.update(height=None),
            pad=False,
        )
        assert len(lines) <= console.height

        dashboard._scroll_activity(-dashboard.activity_page_size)
        cursor = dashboard.activity_cursor
        assert cursor is not None
        engine.control.record_activity(
            kind="tool",
            label="new-live-step",
            message="must not move history",
        )
        dashboard.render()
        assert dashboard.activity_cursor == cursor

        dashboard._scroll_activity(dashboard.control.MAX_ACTIVITY_ROWS)
        assert dashboard.activity_cursor is None
    finally:
        engine.close()


def test_live_dashboard_labels_a_dead_failed_controller_and_its_recovery(
    tmp_path: Path, fake_config
) -> None:
    engine = FrontierEngine.create("Expose a recoverable failure.", config=fake_config())
    output = StringIO()
    console = Console(
        file=output,
        width=100,
        height=30,
        force_terminal=False,
        color_system=None,
        no_color=True,
        theme=FLOURITE_THEME,
    )
    try:
        runtime = engine.control.set_runtime(
            status=RuntimeStatus.FAILED,
            phase="bootstrapping",
            detail="controller error",
        )
        state = engine.state.model_copy(deep=True)
        state.runtime.bootstrap.error = "provider slice ended without a boundary"
        dashboard = LiveDashboard(
            run_dir=engine.run_dir,
            control=engine.control,
            console=console,
        )

        assert dashboard._effective_status(state, runtime) == "failed"
        console.print(dashboard._summary(state, runtime))
        rendered = output.getvalue()
        assert "last error" in rendered
        assert "provider slice ended without a boundary" in rendered
        assert "press r" in rendered

        output.seek(0)
        output.truncate(0)
        runtime = engine.control.set_runtime(
            status=RuntimeStatus.RUNNING,
            phase="bootstrapping",
            pid=os.getpid(),
            detail="recovering provider session",
        )
        console.print(dashboard._summary(state, runtime))
        rendered = output.getvalue()
        assert "in progress" in rendered
        assert "press r" not in rendered
    finally:
        engine.close()
