from __future__ import annotations

from pathlib import Path

import pytest

from frontier_harness.core.journal import KernelJournal
from frontier_harness.core.types import (
    ArtifactVersion,
    ChallengeVerdict,
    ComputeEnvelope,
    ComputeUsage,
    ContentRef,
    FinishClaim,
    FinishClaimed,
    Move,
    MoveApplied,
    MoveFinished,
    MoveMode,
    MoveProposed,
    MoveStarted,
    Objective,
    Observation,
    ObservationKind,
    ObservationRecorded,
    RunStarted,
    RunStatus,
    RunTerminated,
    SteeringReceived,
    Trajectory,
    WorkspaceCommitted,
    WorkspaceVersion,
)
from frontier_harness.errors import LedgerIntegrityError
from frontier_harness.ledger import EventLedger
from frontier_harness.util import sha256_text

NOW = "2026-08-26T00:00:00Z"


def ref(text: str, name: str = "value.md") -> ContentRef:
    return ContentRef(
        digest=sha256_text(text),
        size=len(text.encode()),
        media_type="text/markdown",
        relative_path=f"sha256/{sha256_text(text)}",
        original_name=name,
    )


def open_journal(tmp_path: Path, *, max_parallel: int = 1) -> KernelJournal:
    objective_text = "Build the strongest exact result."
    objective = Objective(
        objective_id="obj_1",
        original_text_ref=ref(objective_text, "objective.md"),
        original_text_digest=sha256_text(objective_text),
        envelope=ComputeEnvelope(max_model_turns=10, max_parallel=max_parallel),
        created_at=NOW,
    )
    root = Trajectory(
        trajectory_id="traj_root",
        purpose="Solve the objective",
        created_at=NOW,
    )
    journal = KernelJournal(
        ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_1"),
        snapshot_path=tmp_path / "state.json",
    )
    journal.append("run.started", RunStarted(objective=objective, root_trajectory=root))
    return journal


def propose_and_start(
    journal: KernelJournal,
    move_id: str,
    *,
    workspace_id: str | None = None,
    mode: MoveMode = MoveMode.LEAD,
) -> None:
    journal.append(
        "move.proposed",
        MoveProposed(
            move=Move(
                move_id=move_id,
                based_on_workspace_id=workspace_id,
                trajectory_id="traj_root",
                mode=mode,
                intent="Make a decision-changing improvement",
                idempotency_key=f"key-{move_id}",
                proposed_at=NOW,
            )
        ),
    )
    journal.append(
        "move.started",
        MoveStarted(move_id=move_id, started_at=NOW),
        action_id=move_id,
    )


def commit_workspace(
    journal: KernelJournal,
    move_id: str,
    workspace_id: str,
    *,
    parent_id: str | None = None,
    activate: bool = True,
    observations: list[str] | None = None,
) -> None:
    document = f"# Workspace {workspace_id}"
    journal.append(
        "workspace.committed",
        WorkspaceCommitted(
            workspace=WorkspaceVersion(
                workspace_id=workspace_id,
                parent_workspace_id=parent_id,
                document_ref=ref(document),
                summary=f"Workspace {workspace_id}",
                based_on_event_seq=journal.state.last_event_seq,
                created_at=NOW,
                active_trajectory_ids=["traj_root"],
                consumed_observation_ids=observations or [],
                created_by_move_id=move_id,
            ),
            activate=activate,
        ),
        action_id=move_id,
    )


def finish_move(
    journal: KernelJournal,
    move_id: str,
    *,
    workspace_id: str | None = None,
    observation_ids: list[str] | None = None,
    model_turns: int = 1,
) -> None:
    journal.append(
        "move.finished",
        MoveFinished(
            move_id=move_id,
            success=True,
            finished_at=NOW,
            usage_delta=ComputeUsage(model_turns=model_turns),
            workspace_id=workspace_id,
            observation_ids=observation_ids or [],
        ),
        action_id=move_id,
    )


def test_kernel_journal_replays_one_live_workspace(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    propose_and_start(journal, "move_1")
    commit_workspace(journal, "move_1", "ws_1")
    finish_move(journal, "move_1", workspace_id="ws_1")

    assert journal.state.status == RunStatus.ACTIVE
    assert journal.state.current_workspace_id == "ws_1"
    assert journal.state.usage.model_turns == 1

    event_count, tip = journal.ledger.verify()
    before = journal.state.model_dump(mode="json")
    journal.close()

    reopened = KernelJournal(
        ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_1"),
        snapshot_path=tmp_path / "state.json",
    )
    assert reopened.refresh().model_dump(mode="json") == before
    assert reopened.ledger.verify() == (event_count, tip)


def test_invalid_transition_rolls_back_the_event(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    before_count = journal.ledger.count()

    with pytest.raises(LedgerIntegrityError, match="only a proposed move can start"):
        journal.append("move.started", MoveStarted(move_id="missing", started_at=NOW))

    assert journal.ledger.count() == before_count
    assert journal.state.last_event_seq == before_count


def test_workspace_activation_uses_compare_and_swap(tmp_path: Path) -> None:
    journal = open_journal(tmp_path, max_parallel=2)
    propose_and_start(journal, "move_a")
    propose_and_start(journal, "move_b")
    commit_workspace(journal, "move_a", "ws_a")

    with pytest.raises(LedgerIntegrityError, match="lost compare-and-swap"):
        commit_workspace(journal, "move_b", "ws_b")

    commit_workspace(journal, "move_b", "ws_b", activate=False)
    assert journal.state.current_workspace_id == "ws_a"
    assert set(journal.state.workspaces) == {"ws_a", "ws_b"}


def test_observation_cannot_certify_an_unknown_artifact(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    propose_and_start(journal, "move_1")
    observation = Observation(
        observation_id="obs_stale",
        kind=ObservationKind.TEST,
        summary="Looks good",
        source="test",
        created_at=NOW,
        move_id="move_1",
        trajectory_id="traj_root",
        artifact_digest="a" * 64,
    )

    with pytest.raises(LedgerIntegrityError, match="unknown artifact digest"):
        journal.append("observation.recorded", ObservationRecorded(observation=observation))


def test_satisfaction_requires_direct_challenge_support(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    propose_and_start(journal, "move_1")
    commit_workspace(journal, "move_1", "ws_1")
    finish_move(journal, "move_1", workspace_id="ws_1")
    claim = FinishClaim(
        claim_id="claim_1",
        workspace_id="ws_1",
        satisfaction_claims=["The objective is satisfied"],
        created_at=NOW,
    )
    journal.append("finish.claimed", FinishClaimed(claim=claim))

    with pytest.raises(LedgerIntegrityError, match="lacks supporting observations"):
        journal.append(
            "run.satisfied",
            RunTerminated(status="satisfied", reason="done", claim_id="claim_1"),
        )

    propose_and_start(journal, "challenge_1", workspace_id="ws_1", mode=MoveMode.CHALLENGE)
    observation = Observation(
        observation_id="obs_support",
        kind=ObservationKind.CHALLENGE,
        summary="Direct inspection supports the claim",
        source="fresh-challenger",
        created_at=NOW,
        move_id="challenge_1",
        trajectory_id="traj_root",
        challenge_verdict=ChallengeVerdict.SUPPORTS,
        metadata={"claim_id": "claim_1"},
    )
    journal.append(
        "observation.recorded",
        ObservationRecorded(observation=observation),
        action_id="challenge_1",
    )
    finish_move(journal, "challenge_1", observation_ids=["obs_support"])
    journal.append(
        "run.satisfied",
        RunTerminated(
            status="satisfied",
            reason="evidenced objective satisfaction",
            claim_id="claim_1",
            supporting_observation_ids=["obs_support"],
        ),
    )
    assert journal.state.status == RunStatus.SATISFIED


def test_exhaustion_means_a_real_hard_envelope(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    with pytest.raises(LedgerIntegrityError, match="before a hard envelope"):
        journal.append(
            "run.exhausted",
            RunTerminated(status="exhausted", reason="no ideas"),
        )

    propose_and_start(journal, "move_1")
    finish_move(journal, "move_1", model_turns=10)
    journal.append(
        "run.exhausted",
        RunTerminated(status="exhausted", reason="model-turn envelope exhausted"),
    )
    assert journal.state.status == RunStatus.EXHAUSTED


def test_duplicate_idempotency_key_is_rejected(tmp_path: Path) -> None:
    journal = open_journal(tmp_path, max_parallel=2)
    propose_and_start(journal, "move_1")
    duplicate = Move(
        move_id="move_2",
        trajectory_id="traj_root",
        mode=MoveMode.LEAD,
        intent="repeat",
        idempotency_key="key-move_1",
        proposed_at=NOW,
    )
    with pytest.raises(LedgerIntegrityError, match="duplicate move idempotency key"):
        journal.append("move.proposed", MoveProposed(move=duplicate))


def test_atomic_move_application_is_all_or_nothing(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    propose_and_start(journal, "move_1")
    before = journal.state.model_dump(mode="json")
    before_count = journal.ledger.count()
    observation = Observation(
        observation_id="obs_1",
        kind=ObservationKind.TEST,
        summary="A real check ran",
        source="tool",
        created_at=NOW,
        move_id="move_1",
        trajectory_id="traj_root",
    )
    invalid_workspace = WorkspaceVersion(
        workspace_id="ws_bad",
        parent_workspace_id="missing_parent",
        document_ref=ref("# Invalid"),
        summary="Invalid",
        based_on_event_seq=journal.state.last_event_seq,
        created_at=NOW,
        active_trajectory_ids=["traj_root"],
        consumed_observation_ids=["obs_1"],
        created_by_move_id="move_1",
    )

    with pytest.raises(LedgerIntegrityError, match="workspace lineage"):
        journal.append(
            "move.applied",
            MoveApplied(
                move_id="move_1",
                success=True,
                finished_at=NOW,
                observations=[observation],
                workspace=invalid_workspace,
            ),
        )

    assert journal.ledger.count() == before_count
    assert journal.state.model_dump(mode="json") == before


def test_satisfaction_support_must_bind_to_every_claimed_artifact(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    propose_and_start(journal, "move_1")
    artifact_ref = ref("artifact", "artifact.md")
    artifact = ArtifactVersion(
        artifact_id="art_1",
        content_ref=artifact_ref,
        digest=artifact_ref.digest,
        trajectory_id="traj_root",
        created_by_move_id="move_1",
        created_at=NOW,
    )
    workspace = WorkspaceVersion(
        workspace_id="ws_1",
        document_ref=ref("# Workspace"),
        summary="Ready",
        based_on_event_seq=journal.state.last_event_seq,
        artifact_head_ids=["art_1"],
        active_trajectory_ids=["traj_root"],
        created_by_move_id="move_1",
        created_at=NOW,
    )
    claim = FinishClaim(
        claim_id="claim_1",
        workspace_id="ws_1",
        artifact_head_ids=["art_1"],
        satisfaction_claims=["Done"],
        created_at=NOW,
    )
    journal.append(
        "move.applied",
        MoveApplied(
            move_id="move_1",
            success=True,
            finished_at=NOW,
            artifacts=[artifact],
            workspace=workspace,
            finish_claim=claim,
        ),
    )
    propose_and_start(journal, "challenge_1", workspace_id="ws_1", mode=MoveMode.CHALLENGE)
    support = Observation(
        observation_id="obs_support",
        kind=ObservationKind.CHALLENGE,
        summary="A review supports the prose but did not inspect the artifact",
        source="challenger",
        created_at=NOW,
        move_id="challenge_1",
        trajectory_id="traj_root",
        challenge_verdict=ChallengeVerdict.SUPPORTS,
        metadata={"claim_id": "claim_1"},
    )
    journal.append(
        "move.applied",
        MoveApplied(
            move_id="challenge_1",
            success=True,
            finished_at=NOW,
            observations=[support],
        ),
    )

    with pytest.raises(LedgerIntegrityError, match="exact artifact support"):
        journal.append(
            "run.satisfied",
            RunTerminated(
                status="satisfied",
                reason="done",
                claim_id="claim_1",
                supporting_observation_ids=["obs_support"],
            ),
        )


def test_new_operator_steering_invalidates_an_older_finish_claim(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    propose_and_start(journal, "move_1")
    workspace = WorkspaceVersion(
        workspace_id="ws_1",
        document_ref=ref("# Workspace"),
        summary="Ready",
        based_on_event_seq=journal.state.last_event_seq,
        active_trajectory_ids=["traj_root"],
        created_by_move_id="move_1",
        created_at=NOW,
    )
    claim = FinishClaim(
        claim_id="claim_1",
        workspace_id="ws_1",
        satisfaction_claims=["Done"],
        created_at=NOW,
    )
    journal.append(
        "move.applied",
        MoveApplied(
            move_id="move_1",
            success=True,
            finished_at=NOW,
            workspace=workspace,
            finish_claim=claim,
        ),
    )
    steering = Observation(
        observation_id="obs_steer",
        kind=ObservationKind.STEERING,
        summary="Also cover the newly supplied edge case.",
        source="operator",
        created_at=NOW,
    )

    journal.append("steering.received", SteeringReceived(observation=steering))

    assert journal.state.finish_claim is None
    assert journal.state.pending_steering_ids == ["obs_steer"]
