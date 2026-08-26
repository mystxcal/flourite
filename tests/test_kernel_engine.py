from __future__ import annotations

from collections import deque
from io import StringIO
from pathlib import Path

from rich.console import Console

from frontier_harness.config import HarnessConfig, ProviderConfig, RunPolicy
from frontier_harness.control import CommandKind
from frontier_harness.core.types import (
    ChallengeVerdict,
    ComputeUsage,
    Move,
    ObservationKind,
    RunState,
    RunStatus,
)
from frontier_harness.intelligence.context import ContextFrame
from frontier_harness.intelligence.contracts import (
    FinishDraft,
    MoveExecutionResult,
    ObservationDraft,
    WorkspaceDraft,
)
from frontier_harness.live import KernelLiveDashboard
from frontier_harness.presentation import FLOURITE_THEME
from frontier_harness.runtime.engine import KernelEngine


class EngineRunner:
    def __init__(self, outcomes: list[MoveExecutionResult]) -> None:
        self.outcomes = deque(outcomes)
        self.contexts: list[ContextFrame] = []

    async def run(
        self,
        *,
        move: Move,
        state: RunState,
        context: ContextFrame,
        recovering: bool,
    ) -> MoveExecutionResult:
        self.contexts.append(context)
        return self.outcomes.popleft()


def config_for(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        run=RunPolicy(run_root=tmp_path / "runs"),
        provider=ProviderConfig(kind="fake"),
    )


async def test_kernel_engine_create_control_execute_verify_and_materialize(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source truth", encoding="utf-8")
    runner = EngineRunner(
        [
            MoveExecutionResult(
                workspace=WorkspaceDraft(
                    document="# Current best\n\nDone.\n",
                    summary="Done",
                ),
                finish=FinishDraft(satisfaction_claims=["The task is done"]),
                usage=ComputeUsage(model_turns=1),
            ),
            MoveExecutionResult(
                observations=[
                    ObservationDraft(
                        kind=ObservationKind.CHALLENGE,
                        summary="Direct review supports completion",
                        source="challenger",
                        challenge_verdict=ChallengeVerdict.SUPPORTS,
                    )
                ],
                usage=ComputeUsage(model_turns=1),
            ),
        ]
    )
    engine = KernelEngine.create(
        "Use the source and solve the task.",
        config=config_for(tmp_path),
        adapter_name="generic",
        source_paths=[source],
        runner=runner,
    )
    engine.control.enqueue(CommandKind.STEER, text="Prefer the clearest exact answer.")

    await engine.execute()

    assert engine.state.status == RunStatus.SATISFIED
    assert len(engine.sources) == 1
    assert any(
        observation.kind == ObservationKind.STEERING
        for observation in runner.contexts[0].observations
    )
    materialized = engine.materialize_current()
    assert materialized.read_text(encoding="utf-8").startswith("# Current best")
    assert engine.verify()[0] == engine.journal.ledger.count()


async def test_pause_and_resume_are_safe_boundary_events(tmp_path: Path) -> None:
    runner = EngineRunner(
        [
            MoveExecutionResult(
                workspace=WorkspaceDraft(document="# Work", summary="Work"),
                usage=ComputeUsage(model_turns=1),
            )
        ]
    )
    engine = KernelEngine.create(
        "Do work.",
        config=config_for(tmp_path),
        adapter_name="generic",
        runner=runner,
    )
    engine.control.enqueue(CommandKind.PAUSE)

    await engine.execute()
    assert engine.state.status == RunStatus.PAUSED
    assert not runner.contexts

    engine.control.enqueue(CommandKind.RESUME)
    await engine.execute(max_steps=1)
    assert engine.state.status == RunStatus.ACTIVE
    assert len(runner.contexts) == 1


def test_kernel_live_dashboard_renders_the_actual_kernel_projection(tmp_path: Path) -> None:
    engine = KernelEngine.create(
        "Render the real kernel state.",
        config=config_for(tmp_path),
        adapter_name="generic",
        runner=EngineRunner([]),
    )
    output = StringIO()
    console = Console(
        file=output,
        width=180,
        height=36,
        force_terminal=False,
        color_system=None,
        no_color=True,
        theme=FLOURITE_THEME,
    )
    try:
        dashboard = KernelLiveDashboard(
            run_dir=engine.run_dir,
            control=engine.control,
            console=console,
        )
        console.print(dashboard.render())
        rendered = output.getvalue()
    finally:
        engine.close()

    assert "RESTING" in rendered
    assert "active" in rendered
    assert "1 live/history" in rendered
    assert "queued" in rendered
    assert "lead" in rendered
