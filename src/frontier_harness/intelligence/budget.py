"""Empirical, task-agnostic resource signals for semantic transitions."""

from __future__ import annotations

import math
from datetime import datetime

from ..core.types import ComputeUsage, CoreModel, MoveMode, MoveStatus, RunState


class CausalBoundarySignal(CoreModel):
    """The remaining wall envelope now fits at most one representative Lead move."""

    remaining_wall_seconds: float
    empirical_move_seconds: float
    completed_lead_moves: int


def causal_boundary_signal(
    state: RunState,
    *,
    prospective_usage: ComputeUsage | None = None,
    prospective_lead_seconds: float | None = None,
) -> CausalBoundarySignal | None:
    """Derive a final-move boundary from committed Lead durations.

    Four observations are the minimum needed to locate an upper quartile without
    inventing a task-specific reserve. The nearest-rank upper quartile starts the
    checkpoint when the remaining envelope no longer covers a representative
    expensive Lead move.
    """

    wall_limit = state.objective.envelope.max_wall_seconds
    if wall_limit is None:
        return None
    usage = state.usage.plus(prospective_usage or ComputeUsage())
    remaining = wall_limit - usage.wall_seconds
    if remaining <= 0:
        return None

    durations = _committed_lead_durations(state)
    if prospective_lead_seconds is not None and prospective_lead_seconds > 0:
        durations.append(prospective_lead_seconds)
    if len(durations) < 4:
        return None

    durations.sort()
    rank = math.ceil(0.75 * len(durations)) - 1
    representative = durations[rank]
    if remaining > representative:
        return None
    return CausalBoundarySignal(
        remaining_wall_seconds=remaining,
        empirical_move_seconds=representative,
        completed_lead_moves=len(durations),
    )


def _committed_lead_durations(state: RunState) -> list[float]:
    durations: list[float] = []
    for move in state.moves.values():
        if (
            move.mode != MoveMode.LEAD
            or move.trajectory_id != state.root_trajectory_id
            or move.status != MoveStatus.SUCCEEDED
            or move.started_at is None
            or move.finished_at is None
        ):
            continue
        elapsed = (_instant(move.finished_at) - _instant(move.started_at)).total_seconds()
        if elapsed > 0:
            durations.append(elapsed)
    return durations


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
