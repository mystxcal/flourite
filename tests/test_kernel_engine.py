from __future__ import annotations

from collections import deque
from io import StringIO
from pathlib import Path

from rich.console import Console

from frontier_harness.blobs import BlobStore
from frontier_harness.config import HarnessConfig, ProviderConfig, RunPolicy
from frontier_harness.control import CommandKind
from frontier_harness.core.types import (
    AssayStatus,
    ChallengeVerdict,
    ComputeUsage,
    Move,
    ObservationKind,
    RunState,
    RunStatus,
)
from frontier_harness.intelligence.context import ContextFrame
from frontier_harness.intelligence.contracts import (
    ArtifactDraft,
    FinishDraft,
    MoveExecutionResult,
    ObservationDraft,
    WorkspaceDraft,
)
from frontier_harness.live import KernelLiveDashboard
from frontier_harness.presentation import FLOURITE_THEME
from frontier_harness.runtime.activity import ProviderActivity
from frontier_harness.runtime.engine import KernelEngine


class EngineRunner:
    def __init__(self, outcomes: list[MoveExecutionResult]) -> None:
        self.outcomes = deque(outcomes)
        self.contexts: list[ContextFrame] = []
        self.blobs: BlobStore | None = None

    async def run(
        self,
        *,
        move: Move,
        state: RunState,
        context: ContextFrame,
        recovering: bool,
    ) -> MoveExecutionResult:
        self.contexts.append(context)
        result = self.outcomes.popleft()
        update: dict[str, object] = {}
        if result.workspace is not None:
            steering_ids = [
                item.observation_id
                for item in context.observations
                if item.kind == ObservationKind.STEERING
            ]
            if steering_ids:
                update["workspace"] = result.workspace.model_copy(
                    update={
                        "consumed_observation_ids": list(
                            dict.fromkeys(
                                [*result.workspace.consumed_observation_ids, *steering_ids]
                            )
                        )
                    }
                )
        if result.finish is not None and result.artifact is None:
            assert self.blobs is not None
            document = result.workspace.document if result.workspace is not None else "artifact"
            update["artifact"] = ArtifactDraft(
                content_ref=self.blobs.put_text(
                    document,
                    media_type="text/markdown; charset=utf-8",
                    original_name="engine-test-artifact.md",
                )
            )
        if move.mode.value == "challenge":
            claim = state.finish_claim
            assert claim is not None
            digest = state.artifacts[claim.artifact_head_ids[0]].digest
            update["observations"] = [
                item.model_copy(
                    update={
                        "artifact_digest": item.artifact_digest or digest,
                        "assay_coverage": item.assay_coverage or "the exact claimed artifact",
                        "covered_claims": item.covered_claims or claim.satisfaction_claims,
                    }
                )
                if item.challenge_verdict is not None
                else item
                for item in result.observations
            ]
        return result.model_copy(update=update)


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
                        source="fresh-challenger",
                        challenge_verdict=ChallengeVerdict.SUPPORTS,
                        assay_status=AssayStatus.VALID,
                        direct_inspection=True,
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
    runner.blobs = engine.blobs
    engine.control.enqueue(CommandKind.STEER, text="Prefer the clearest exact answer.")

    await engine.execute()

    assert engine.state.status == RunStatus.SATISFIED
    assert len(engine.sources) == 1
    assert any(
        observation.kind == ObservationKind.STEERING
        for observation in runner.contexts[0].observations
    )
    assert len(engine.state.objective.amendments) == 1
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


def test_provider_activity_projects_only_operator_relevant_events() -> None:
    assert ProviderActivity.from_event({"type": "irrelevant"}) is None
    assert ProviderActivity.from_event({"type": "session"}) == ProviderActivity(
        kind="session",
        label="session",
        message="model context opened",
    )
    assert ProviderActivity.from_event(
        {
            "type": "tool_execution_start",
            "toolName": "shell",
            "toolCallId": "call_1",
            "intent": "  inspect   the result  ",
        }
    ) == ProviderActivity(
        kind="tool_execution_start",
        label="shell",
        message="inspect the result",
        action_id="call_1",
    )
    assert ProviderActivity.from_event(
        {"type": "tool_execution_end", "toolName": "shell", "isError": True}
    ) == ProviderActivity(
        kind="tool_execution_end",
        label="shell",
        message="tool failed",
        state="warn",
    )
    assert ProviderActivity.from_event(
        {
            "type": "subagent_activity",
            "agent": "critic",
            "message": "  found   a gap ",
            "state": "invented",
        }
    ) == ProviderActivity(
        kind="subagent_activity",
        label="critic",
        message="found a gap",
    )
