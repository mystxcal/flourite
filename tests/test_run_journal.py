from __future__ import annotations

from pathlib import Path

import pytest

from frontier_harness import events as et
from frontier_harness.execution import RunJournal
from frontier_harness.execution import journal as journal_module
from frontier_harness.ledger import EventLedger


def journal(tmp_path: Path) -> RunJournal:
    return RunJournal(
        ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_test"),
        snapshot_path=tmp_path / "state.json",
        max_event_payload_bytes=1024 * 1024,
    )


def test_journal_owns_ledger_projection_and_snapshot(tmp_path: Path) -> None:
    run = journal(tmp_path)
    try:
        event = run.append(
            et.RUN_CREATED,
            {
                "created_at": "2026-08-26T00:00:00Z",
                "source_prompt": "Solve it",
            },
        )
        assert run.state.last_event_seq == event.seq
        assert run.state.source_prompt == "Solve it"
        assert (tmp_path / "state.json").is_file()
        assert run.verify() == (1, event.event_hash)
    finally:
        run.close()


def test_invalid_projection_changes_neither_history_nor_state(tmp_path: Path) -> None:
    run = journal(tmp_path)
    try:
        first = run.append(
            et.RUN_CREATED,
            {
                "created_at": "2026-08-26T00:00:00Z",
                "source_prompt": "Solve it",
            },
        )
        before = run.state.model_copy(deep=True)

        with pytest.raises(KeyError):
            run.append(
                et.ACTION_STARTED,
                {"action_id": "act_missing", "started_at": "now"},
                action_id="act_missing",
            )

        assert run.count() == 1
        assert run.state == before
        assert run.verify() == (1, first.event_hash)
    finally:
        run.close()


def test_payload_limit_is_enforced_before_a_transaction(tmp_path: Path) -> None:
    run = RunJournal(
        ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_test"),
        snapshot_path=tmp_path / "state.json",
        max_event_payload_bytes=32,
    )
    try:
        with pytest.raises(ValueError, match="externalize it as a blob"):
            run.append(et.RUN_CREATED, {"source_prompt": "x" * 100})
        assert run.count() == 0
    finally:
        run.close()


def test_snapshot_failure_cannot_turn_a_committed_event_into_a_false_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = journal(tmp_path)
    original = journal_module.atomic_write_text

    def fail_snapshot(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic snapshot outage")

    monkeypatch.setattr(journal_module, "atomic_write_text", fail_snapshot)
    try:
        event = run.append(
            et.RUN_CREATED,
            {
                "created_at": "2026-08-26T00:00:00Z",
                "source_prompt": "Commit exactly once",
            },
        )
        assert run.count() == 1
        assert run.state.last_event_seq == event.seq
        assert run.snapshot_error == "OSError: synthetic snapshot outage"

        monkeypatch.setattr(journal_module, "atomic_write_text", original)
        assert run.sync_snapshot(strict=True)
        assert run.snapshot_error is None
        assert (tmp_path / "state.json").is_file()
    finally:
        run.close()
