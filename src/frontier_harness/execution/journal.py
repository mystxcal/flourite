"""Atomic ownership of a run's ledger, projection, and derived snapshot."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..errors import LedgerIntegrityError
from ..ledger import EventLedger, LedgerEvent
from ..models import RunState
from ..state import StateReducer
from ..util import atomic_write_text, canonical_json


class RunJournal:
    """The single consistency boundary around authoritative run history.

    The immutable ledger is authoritative; ``state.json`` is only a derived
    cache. An append is accepted only if the reducer can project it before the
    SQLite transaction commits. State, history, and snapshot therefore cannot
    describe three different runs after a malformed internal transition.
    """

    def __init__(
        self,
        *,
        ledger: EventLedger,
        snapshot_path: Path,
        max_event_payload_bytes: int,
        state: RunState | None = None,
        reducer: StateReducer | None = None,
    ) -> None:
        self._ledger = ledger
        self._snapshot_path = snapshot_path
        self._max_event_payload_bytes = max_event_payload_bytes
        self._reducer = reducer or StateReducer()
        self._state = state

    @property
    def state(self) -> RunState:
        if self._state is None:
            raise LedgerIntegrityError("Run journal has no projected state")
        return self._state

    @property
    def compatibility_ledger(self) -> EventLedger:
        """Temporary read/backup bridge for pre-journal extension code.

        New code should use RunJournal's bounded methods. The property exists
        so audit exporters and third-party integrations do not break during
        the architectural migration; writes still belong to ``append``.
        """

        return self._ledger

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any] | BaseModel,
        *,
        actor: str = "runtime",
        action_id: str | None = None,
    ) -> LedgerEvent:
        normalized = (
            payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        )
        encoded_size = len(canonical_json(normalized).encode("utf-8"))
        if encoded_size > self._max_event_payload_bytes:
            raise ValueError(
                f"Event payload is {encoded_size:,} bytes, above the configured "
                f"{self._max_event_payload_bytes:,}-byte boundary; externalize it as a blob"
            )

        projected: RunState | None = None

        def project(event: LedgerEvent) -> None:
            nonlocal projected
            base = self._state.model_copy(deep=True) if self._state is not None else None
            projected = self._reducer.apply(base, event)

        event = self._ledger.append(
            event_type,
            normalized,
            actor=actor,
            action_id=action_id,
            validate=project,
        )
        if projected is None:
            raise LedgerIntegrityError("Committed event was not projected")
        self._state = projected
        self._write_snapshot()
        return event

    def refresh(self, *, expected_run_id: str | None = None) -> RunState:
        projected = self._reducer.replay(self._ledger.verified_events())
        if expected_run_id is not None and projected.run_id != expected_run_id:
            raise LedgerIntegrityError(
                f"Loaded run {expected_run_id}, but the ledger reconstructs {projected.run_id}"
            )
        self._state = projected
        self._write_snapshot()
        return projected

    def verified_projection(self) -> tuple[list[LedgerEvent], RunState]:
        events = self._ledger.verified_events()
        return events, self._reducer.replay(events)

    def events(self) -> Iterator[LedgerEvent]:
        return self._ledger.events()

    def count(self) -> int:
        return self._ledger.count()

    def verify(self) -> tuple[int, str]:
        return self._ledger.verify()

    def close(self) -> None:
        self._ledger.close()

    def _write_snapshot(self) -> None:
        atomic_write_text(
            self._snapshot_path,
            json.dumps(self.state.model_dump(mode="json"), indent=2, ensure_ascii=False),
        )
