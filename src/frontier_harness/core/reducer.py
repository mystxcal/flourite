"""Deterministic projection and legality checks for the phase-free kernel."""

from __future__ import annotations

from collections.abc import Iterable

from ..errors import LedgerIntegrityError
from ..ledger import LedgerEvent
from .transition import AtomicMoveTransition, CompletionValidator
from .types import (
    FinishClaimed,
    MoveApplied,
    MoveProposed,
    MoveStarted,
    MoveStatus,
    ObservationKind,
    RunPaused,
    RunResumed,
    RunStarted,
    RunState,
    RunStatus,
    RunTerminated,
    SteeringReceived,
    TrajectoryStatus,
)


class KernelReducer:
    """Apply canonical events to one reconstructible run state."""

    def apply(self, state: RunState | None, event: LedgerEvent) -> RunState:
        if event.event_type == "run.started":
            return self._start(state, event)
        if state is None:
            raise LedgerIntegrityError(f"{event.event_type} occurred before run.started")
        if event.run_id != state.run_id:
            raise LedgerIntegrityError(
                f"event belongs to {event.run_id}, projected run is {state.run_id}"
            )
        if event.seq <= state.last_event_seq:
            raise LedgerIntegrityError(
                f"event sequence {event.seq} is not after {state.last_event_seq}"
            )
        if state.status.terminal:
            raise LedgerIntegrityError(
                f"{event.event_type} cannot follow terminal state {state.status}"
            )

        next_state = state.model_copy(deep=True)
        handlers = {
            "steering.received": self._steer,
            "move.proposed": self._propose_move,
            "move.started": self._start_move,
            "move.applied": self._apply_move,
            "finish.claimed": self._claim_finish,
            "run.paused": self._pause,
            "run.resumed": self._resume,
            "run.satisfied": self._terminate,
            "run.exhausted": self._terminate,
            "run.blocked": self._terminate,
            "run.stopped": self._terminate,
            "run.failed": self._terminate,
        }
        handler = handlers.get(event.event_type)
        if handler is not None:
            handler(next_state, event)
        else:
            raise LedgerIntegrityError(f"unknown canonical event type: {event.event_type}")
        next_state.last_event_seq = event.seq
        next_state.updated_at = event.timestamp
        return RunState.model_validate(
            next_state.model_dump(mode="python", exclude_computed_fields=True)
        )

    def replay(self, events: Iterable[LedgerEvent]) -> RunState:
        state: RunState | None = None
        for event in events:
            state = self.apply(state, event)
        if state is None:
            raise LedgerIntegrityError("cannot reconstruct a run from an empty ledger")
        return state

    @staticmethod
    def _start(state: RunState | None, event: LedgerEvent) -> RunState:
        if state is not None:
            raise LedgerIntegrityError("run.started may occur only once")
        payload = RunStarted.model_validate(event.payload)
        if payload.root_trajectory.parent_trajectory_id is not None:
            raise LedgerIntegrityError("root trajectory cannot have a parent")
        if payload.root_trajectory.base_workspace_id is not None:
            raise LedgerIntegrityError("root trajectory cannot begin from a workspace")
        return RunState(
            run_id=event.run_id,
            objective=payload.objective,
            status=RunStatus.ACTIVE,
            started_at=event.timestamp,
            updated_at=event.timestamp,
            root_trajectory_id=payload.root_trajectory.trajectory_id,
            trajectories={payload.root_trajectory.trajectory_id: payload.root_trajectory},
            last_event_seq=event.seq,
        )

    @staticmethod
    def _require_active(state: RunState, event_type: str) -> None:
        if state.status != RunStatus.ACTIVE:
            raise LedgerIntegrityError(f"{event_type} requires an active run")

    @staticmethod
    def _steer(state: RunState, event: LedgerEvent) -> None:
        payload = SteeringReceived.model_validate(event.payload)
        obs = payload.observation
        if obs.kind != ObservationKind.STEERING or obs.source not in {"user", "operator"}:
            raise LedgerIntegrityError("steering must be a user/operator steering observation")
        if obs.observation_id in state.observations:
            raise LedgerIntegrityError(f"duplicate observation: {obs.observation_id}")
        state.observations[obs.observation_id] = obs
        state.pending_steering_ids.append(obs.observation_id)
        state.finish_claim = None

    @classmethod
    def _propose_move(cls, state: RunState, event: LedgerEvent) -> None:
        cls._require_active(state, event.event_type)
        payload = MoveProposed.model_validate(event.payload)
        move = payload.move
        if move.move_id in state.moves:
            raise LedgerIntegrityError(f"duplicate move: {move.move_id}")
        if any(
            existing.idempotency_key == move.idempotency_key for existing in state.moves.values()
        ):
            raise LedgerIntegrityError(f"duplicate move idempotency key: {move.idempotency_key}")
        if move.status != MoveStatus.PROPOSED:
            raise LedgerIntegrityError("new move must be proposed")
        trajectory = state.trajectories.get(move.trajectory_id)
        if trajectory is None or trajectory.status != TrajectoryStatus.ACTIVE:
            raise LedgerIntegrityError("move trajectory is missing or inactive")
        if (
            move.based_on_workspace_id is not None
            and move.based_on_workspace_id not in state.workspaces
        ):
            raise LedgerIntegrityError("move base workspace is missing")
        if move.based_on_workspace_id is None and state.current_workspace_id is not None:
            raise LedgerIntegrityError("move omitted the existing workspace base")
        if len(state.active_move_ids) >= state.objective.envelope.max_parallel:
            raise LedgerIntegrityError("hard parallel move envelope reached")
        state.moves[move.move_id] = move

    @classmethod
    def _start_move(cls, state: RunState, event: LedgerEvent) -> None:
        cls._require_active(state, event.event_type)
        payload = MoveStarted.model_validate(event.payload)
        move = state.moves.get(payload.move_id)
        if move is None or move.status != MoveStatus.PROPOSED:
            raise LedgerIntegrityError("only a proposed move can start")
        if len(state.active_move_ids) >= state.objective.envelope.max_parallel:
            raise LedgerIntegrityError("hard parallel move envelope reached")
        move.status = MoveStatus.RUNNING
        move.started_at = payload.started_at
        state.active_move_ids.append(move.move_id)

    @classmethod
    def _apply_move(cls, state: RunState, event: LedgerEvent) -> None:
        """Atomically admit one externally executed move result."""

        cls._require_active(state, event.event_type)
        payload = MoveApplied.model_validate(event.payload)
        AtomicMoveTransition(state, payload, event_seq=event.seq).apply()

    @classmethod
    def _claim_finish(cls, state: RunState, event: LedgerEvent) -> None:
        cls._require_active(state, event.event_type)
        payload = FinishClaimed.model_validate(event.payload)
        claim = payload.claim
        if state.active_move_ids:
            raise LedgerIntegrityError("finish claim requires a safe move boundary")
        if claim.workspace_id != state.current_workspace_id:
            raise LedgerIntegrityError("finish claim does not describe the current workspace")
        if any(artifact_id not in state.artifacts for artifact_id in claim.artifact_head_ids):
            raise LedgerIntegrityError("finish claim references a missing artifact")
        if any(obs_id not in state.observations for obs_id in claim.evidence_refs):
            raise LedgerIntegrityError("finish claim references missing evidence")
        state.finish_claim = claim

    @classmethod
    def _pause(cls, state: RunState, event: LedgerEvent) -> None:
        cls._require_active(state, event.event_type)
        RunPaused.model_validate(event.payload)
        if state.active_move_ids:
            raise LedgerIntegrityError("run may pause only at a safe move boundary")
        state.status = RunStatus.PAUSED

    @staticmethod
    def _resume(state: RunState, event: LedgerEvent) -> None:
        RunResumed.model_validate(event.payload)
        if state.status != RunStatus.PAUSED:
            raise LedgerIntegrityError("only a paused run can resume")
        state.status = RunStatus.ACTIVE

    @staticmethod
    def _terminate(state: RunState, event: LedgerEvent) -> None:
        payload = RunTerminated.model_validate(event.payload)
        expected = event.event_type.removeprefix("run.")
        if payload.status != expected:
            raise LedgerIntegrityError("termination payload differs from event type")
        if state.active_move_ids:
            raise LedgerIntegrityError("run may terminate only at a safe move boundary")
        if payload.status == "satisfied":
            CompletionValidator(state, payload).validate()
        elif payload.status == "exhausted" and not state.usage.exhausted(state.objective.envelope):
            raise LedgerIntegrityError("run cannot be exhausted before a hard envelope")
        state.status = RunStatus(payload.status)
        state.terminal_reason = payload.reason
