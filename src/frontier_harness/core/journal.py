"""Atomic journal and disposable snapshot for the new intelligence kernel."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..errors import LedgerIntegrityError
from ..ledger import EventLedger, LedgerEvent
from ..util import atomic_write_text, canonical_json
from .reducer import KernelReducer
from .types import RunState

logger = logging.getLogger(__name__)


class KernelJournal:
    """The only semantic write boundary for a kernel run."""

    def __init__(
        self,
        *,
        ledger: EventLedger,
        snapshot_path: Path,
        max_event_payload_bytes: int = 256 * 1024,
        state: RunState | None = None,
        reducer: KernelReducer | None = None,
    ) -> None:
        self._ledger = ledger
        self._snapshot_path = snapshot_path
        self._max_event_payload_bytes = max_event_payload_bytes
        self._state = state
        self._reducer = reducer or KernelReducer()
        self._snapshot_error: str | None = None

    @property
    def state(self) -> RunState:
        if self._state is None:
            raise LedgerIntegrityError("kernel journal has no projected state")
        return self._state

    @property
    def snapshot_error(self) -> str | None:
        return self._snapshot_error

    @property
    def ledger(self) -> EventLedger:
        return self._ledger

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any] | BaseModel,
        *,
        actor: str = "kernel",
        action_id: str | None = None,
    ) -> LedgerEvent:
        normalized = (
            payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        )
        encoded_size = len(canonical_json(normalized).encode())
        if encoded_size > self._max_event_payload_bytes:
            raise ValueError(
                f"event payload is {encoded_size:,} bytes, above the "
                f"{self._max_event_payload_bytes:,}-byte boundary"
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
            raise LedgerIntegrityError("committed event was not projected")
        self._state = projected
        self.sync_snapshot()
        return event

    def refresh(self) -> RunState:
        self._state = self._reducer.replay(self._ledger.verified_events())
        self.sync_snapshot()
        return self._state

    def events(self) -> Iterator[LedgerEvent]:
        return self._ledger.events()

    def verified_projection(self) -> tuple[list[LedgerEvent], RunState]:
        events = self._ledger.verified_events()
        return events, self._reducer.replay(events)

    def sync_snapshot(self, *, strict: bool = False) -> bool:
        try:
            atomic_write_text(
                self._snapshot_path,
                json.dumps(
                    self.state.model_dump(mode="json", exclude_computed_fields=True),
                    indent=2,
                    ensure_ascii=False,
                ),
            )
        except OSError as exc:
            self._snapshot_error = f"{type(exc).__name__}: {exc}"
            if strict:
                raise
            logger.warning("kernel event committed but snapshot refresh failed: %s", exc)
            return False
        self._snapshot_error = None
        return True

    def close(self) -> None:
        self._ledger.close()

    def __enter__(self) -> KernelJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
