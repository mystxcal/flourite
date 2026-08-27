from __future__ import annotations

from pathlib import Path

import pytest

from frontier_harness.core.journal import KernelJournal
from frontier_harness.core.types import (
    ComputeEnvelope,
    ContentRef,
    Move,
    MoveApplied,
    MoveMode,
    MoveProposed,
    MoveStarted,
    Objective,
    RunStarted,
    Trajectory,
    WorkspaceVersion,
)
from frontier_harness.errors import LedgerIntegrityError
from frontier_harness.ledger import EventLedger
from frontier_harness.util import sha256_text

NOW = "2026-08-26T00:00:00Z"


def _ref(text: str) -> ContentRef:
    digest = sha256_text(text)
    return ContentRef(
        digest=digest,
        size=len(text.encode()),
        media_type="text/markdown",
        relative_path=f"sha256/{digest}",
    )


def _journal(tmp_path: Path) -> KernelJournal:
    task = "Build the strongest exact result."
    journal = KernelJournal(
        ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_1"),
        snapshot_path=tmp_path / "state.json",
    )
    journal.append(
        "run.started",
        RunStarted(
            objective=Objective(
                objective_id="obj_1",
                original_text_ref=_ref(task),
                original_text_digest=sha256_text(task),
                envelope=ComputeEnvelope(max_model_turns=10),
                created_at=NOW,
            ),
            root_trajectory=Trajectory(
                trajectory_id="traj_root",
                purpose="Solve the objective",
                created_at=NOW,
            ),
        ),
    )
    journal.append(
        "move.proposed",
        MoveProposed(
            move=Move(
                move_id="move_1",
                trajectory_id="traj_root",
                mode=MoveMode.LEAD,
                intent="Improve the result",
                idempotency_key="move-key",
                proposed_at=NOW,
            )
        ),
    )
    journal.append("move.started", MoveStarted(move_id="move_1", started_at=NOW))
    return journal


def _workspace(journal: KernelJournal, *, parent: str | None = None) -> WorkspaceVersion:
    return WorkspaceVersion(
        workspace_id="ws_1",
        parent_workspace_id=parent,
        document_ref=_ref("# Result"),
        summary="Result",
        based_on_event_seq=journal.state.last_event_seq,
        active_trajectory_ids=["traj_root"],
        created_by_move_id="move_1",
        created_at=NOW,
    )


def test_invalid_atomic_move_changes_neither_history_nor_state(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    before = journal.state.model_dump(mode="json")
    count = journal.ledger.count()

    with pytest.raises(LedgerIntegrityError, match="workspace lineage"):
        journal.append(
            "move.applied",
            MoveApplied(
                move_id="move_1",
                success=True,
                finished_at=NOW,
                workspace=_workspace(journal, parent="missing"),
            ),
        )

    assert journal.ledger.count() == count
    assert journal.state.model_dump(mode="json") == before
    journal.close()


def test_atomic_move_replays_to_the_same_projection(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.append(
        "move.applied",
        MoveApplied(
            move_id="move_1",
            success=True,
            finished_at=NOW,
            workspace=_workspace(journal),
        ),
    )
    expected = journal.state.model_dump(mode="json")
    event_count = journal.ledger.count()
    journal.close()

    reopened = KernelJournal(
        ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_1"),
        snapshot_path=tmp_path / "state.json",
    )
    try:
        assert reopened.refresh().model_dump(mode="json") == expected
        assert reopened.ledger.count() == event_count
    finally:
        reopened.close()


def test_projection_checkpoint_is_accepted_only_for_the_exact_journal_head(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    expected = journal.state.model_dump(mode="json")
    journal.close()

    ledger = EventLedger(tmp_path / "ledger.sqlite3", "run_1")
    try:
        cached = KernelJournal.load_checkpoint(
            ledger=ledger,
            snapshot_path=tmp_path / "state.json",
        )
        assert cached is not None
        assert cached.model_dump(mode="json") == expected

        (tmp_path / "state.json").write_text("{}", encoding="utf-8")
        assert (
            KernelJournal.load_checkpoint(
                ledger=ledger,
                snapshot_path=tmp_path / "state.json",
            )
            is None
        )
    finally:
        ledger.close()
