"""Bounded-context projectors for the canonical run state."""

from __future__ import annotations

from typing import Protocol

from ..ledger import LedgerEvent
from ..models import RunState
from .bootstrap import BootstrapProjector
from .release import ReleaseProjector
from .runtime import RuntimeProjector
from .semantics import SemanticProjector
from .work import WorkProjector


class EventProjector(Protocol):
    event_types: frozenset[str]

    def apply(self, state: RunState, event: LedgerEvent) -> None: ...


PROJECTORS: tuple[EventProjector, ...] = (
    BootstrapProjector(),
    WorkProjector(),
    SemanticProjector(),
    ReleaseProjector(),
    RuntimeProjector(),
)
