from __future__ import annotations

from typing import Any

import pytest

from frontier_harness import events as et
from frontier_harness.ledger import GENESIS_HASH, LedgerEvent
from frontier_harness.state import StateReducer


def event(event_type: str, payload: dict[str, Any], *, seq: int = 1) -> LedgerEvent:
    return LedgerEvent(
        seq=seq,
        event_id=f"evt_{seq}",
        run_id="run_test",
        timestamp="2026-08-26T00:00:00Z",
        event_type=event_type,
        actor="test",
        action_id=None,
        payload=payload,
        payload_hash="a" * 64,
        previous_hash=GENESIS_HASH,
        event_hash="b" * 64,
    )


def created_state() -> tuple[StateReducer, object]:
    reducer = StateReducer()
    state = reducer.apply(
        None,
        event(
            et.RUN_CREATED,
            {
                "created_at": "2026-08-26T00:00:00Z",
                "source_prompt": "Solve it",
            },
        ),
    )
    return reducer, state


def test_unknown_event_cannot_silently_enter_projection() -> None:
    reducer, state = created_state()
    with pytest.raises(ValueError, match="Unsupported event type"):
        reducer.apply(state, event("future.unprojected", {}, seq=2))  # type: ignore[arg-type]


@pytest.mark.parametrize("event_type", sorted(et.OBSERVATION_ONLY_EVENT_TYPES))
def test_observation_only_events_are_explicit_noops(event_type: str) -> None:
    reducer, state = created_state()
    projected = reducer.apply(state, event(event_type, {}, seq=2))  # type: ignore[arg-type]
    assert projected.last_event_seq == 2
    assert projected.last_event_hash == "b" * 64


def test_every_declared_event_has_an_explicit_projection_category() -> None:
    assert et.OBSERVATION_ONLY_EVENT_TYPES <= et.EVENT_TYPES
    assert len(et.EVENT_TYPES) == 54
