from __future__ import annotations

from pathlib import Path

from frontier_harness.blobs import BlobStore
from frontier_harness.core.types import (
    ComputeEnvelope,
    ComputeUsage,
    Move,
    MoveMode,
    MoveStatus,
    Objective,
    RunState,
    RunStatus,
    Trajectory,
)
from frontier_harness.intelligence.budget import causal_boundary_signal
from frontier_harness.intelligence.compiler import MoveResultCompiler
from frontier_harness.intelligence.contracts import MoveDirective, MoveExecutionResult
from frontier_harness.util import sha256_text


def _state(tmp_path: Path, *, used_wall_seconds: float) -> tuple[RunState, BlobStore]:
    blobs = BlobStore(tmp_path / "blobs")
    objective_ref = blobs.put_text("Build the result.", original_name="objective.md")
    root = "traj_root"
    durations = [10, 20, 30, 40]
    moves = {
        f"move_{index}": Move(
            move_id=f"move_{index}",
            trajectory_id=root,
            mode=MoveMode.LEAD,
            intent="Improve",
            idempotency_key=f"key_{index}",
            status=MoveStatus.SUCCEEDED,
            proposed_at=f"2026-01-01T00:0{index}:00Z",
            started_at=f"2026-01-01T00:0{index}:00Z",
            finished_at=f"2026-01-01T00:0{index}:{duration:02d}Z",
        )
        for index, duration in enumerate(durations, start=1)
    }
    current = Move(
        move_id="move_current",
        trajectory_id=root,
        mode=MoveMode.LEAD,
        intent="Continue local repair",
        idempotency_key="key_current",
        status=MoveStatus.RUNNING,
        proposed_at="2026-01-01T00:10:00Z",
        started_at="2026-01-01T00:10:00Z",
    )
    moves[current.move_id] = current
    state = RunState(
        run_id="run_1",
        objective=Objective(
            objective_id="obj_1",
            original_text_ref=objective_ref,
            original_text_digest=sha256_text("Build the result."),
            envelope=ComputeEnvelope(max_wall_seconds=100),
            created_at="2026-01-01T00:00:00Z",
        ),
        status=RunStatus.ACTIVE,
        started_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:10:00Z",
        root_trajectory_id=root,
        trajectories={
            root: Trajectory(
                trajectory_id=root,
                purpose="Solve",
                created_at="2026-01-01T00:00:00Z",
            )
        },
        moves=moves,
        active_move_ids=[current.move_id],
        usage=ComputeUsage(wall_seconds=used_wall_seconds),
        last_event_seq=10,
    )
    return state, blobs


def _build_move(
    directive: MoveDirective,
    *,
    based_on_workspace_id: str | None,
    additional_trajectory_ids: set[str] | None = None,
) -> Move:
    del additional_trajectory_ids
    return Move(
        move_id="move_next",
        based_on_workspace_id=based_on_workspace_id,
        based_on_event_seq=10,
        trajectory_id=directive.trajectory_id or "traj_root",
        mode=directive.mode,
        intent=directive.intent,
        instructions=directive.instructions,
        causal_checkpoint=directive.causal_checkpoint,
        idempotency_key="key_next",
        proposed_at="2026-01-01T00:11:00Z",
    )


def test_boundary_uses_empirical_upper_quartile(tmp_path: Path) -> None:
    state, _blobs = _state(tmp_path, used_wall_seconds=69)
    assert causal_boundary_signal(state) is None

    state.usage = ComputeUsage(wall_seconds=75)
    signal = causal_boundary_signal(state)

    assert signal is not None
    assert signal.remaining_wall_seconds == 25
    assert signal.empirical_move_seconds == 30
    assert signal.completed_lead_moves == 4


def test_boundary_replaces_ordinary_continuation_with_one_causal_checkpoint(
    tmp_path: Path,
) -> None:
    state, blobs = _state(tmp_path, used_wall_seconds=75)
    current = state.moves["move_current"]
    payload = MoveResultCompiler(
        state=state,
        blobs=blobs,
        build_move=_build_move,
    ).compile(
        current,
        MoveExecutionResult(
            next_move=MoveDirective(
                mode=MoveMode.LEAD,
                intent="Polish the latest symptom",
            ),
            usage=ComputeUsage(wall_seconds=1),
        ),
        visible_observation_ids=set(),
    )

    assert len(payload.next_moves) == 1
    checkpoint = payload.next_moves[0]
    assert checkpoint.causal_checkpoint is True
    assert checkpoint.intent == "Settle the earliest causal boundary before the hard envelope"
    assert "Polish the latest symptom" in checkpoint.instructions


def test_checkpoint_cannot_chain_and_preserves_a_durable_terminal_snapshot(
    tmp_path: Path,
) -> None:
    state, blobs = _state(tmp_path, used_wall_seconds=75)
    checkpoint = state.moves["move_current"].model_copy(update={"causal_checkpoint": True})
    state.moves[checkpoint.move_id] = checkpoint
    payload = MoveResultCompiler(
        state=state,
        blobs=blobs,
        build_move=_build_move,
    ).compile(
        checkpoint,
        MoveExecutionResult(
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Try once more"),
            usage=ComputeUsage(wall_seconds=1),
        ),
        visible_observation_ids=set(),
    )

    assert payload.next_moves == []
    assert payload.blocked_reason is not None
    assert len(payload.blocker_evidence_refs) == 1
    evidence = payload.observations[-1]
    assert evidence.observation_id == payload.blocker_evidence_refs[0]
    assert evidence.raw_ref == state.objective.original_text_ref
    assert evidence.metadata["kernel_signal"] == "causal_boundary_settled"
