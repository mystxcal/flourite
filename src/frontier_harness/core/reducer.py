"""Deterministic projection and legality checks for the phase-free kernel."""

from __future__ import annotations

from collections.abc import Iterable

from ..errors import LedgerIntegrityError
from ..ledger import LedgerEvent
from .types import (
    ArtifactCommitted,
    ChallengeVerdict,
    FinishClaimed,
    MoveApplied,
    MoveFinished,
    MoveProposed,
    MoveStarted,
    MoveStatus,
    ObservationKind,
    ObservationRecorded,
    RunPaused,
    RunResumed,
    RunStarted,
    RunState,
    RunStatus,
    RunTerminated,
    SteeringReceived,
    TrajectoryStatus,
    WorkspaceCommitted,
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
            "observation.recorded": self._record_observation,
            "artifact.committed": self._commit_artifact,
            "workspace.committed": self._commit_workspace,
            "move.finished": self._finish_move,
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
        if handler is None:
            raise LedgerIntegrityError(f"unknown canonical event type: {event.event_type}")
        handler(next_state, event)
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
        if any(existing.idempotency_key == move.idempotency_key for existing in state.moves.values()):
            raise LedgerIntegrityError(f"duplicate move idempotency key: {move.idempotency_key}")
        if move.status != MoveStatus.PROPOSED:
            raise LedgerIntegrityError("new move must be proposed")
        trajectory = state.trajectories.get(move.trajectory_id)
        if trajectory is None or trajectory.status != TrajectoryStatus.ACTIVE:
            raise LedgerIntegrityError("move trajectory is missing or inactive")
        if move.based_on_workspace_id is not None and move.based_on_workspace_id not in state.workspaces:
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

    @staticmethod
    def _record_observation(state: RunState, event: LedgerEvent) -> None:
        payload = ObservationRecorded.model_validate(event.payload)
        obs = payload.observation
        if obs.observation_id in state.observations:
            raise LedgerIntegrityError(f"duplicate observation: {obs.observation_id}")
        if obs.move_id is not None:
            move = state.moves.get(obs.move_id)
            if move is None or move.status != MoveStatus.RUNNING:
                raise LedgerIntegrityError("move observation requires its running move")
            if obs.trajectory_id not in {None, move.trajectory_id}:
                raise LedgerIntegrityError("observation trajectory differs from its move")
        if obs.trajectory_id is not None and obs.trajectory_id not in state.trajectories:
            raise LedgerIntegrityError("observation trajectory is missing")
        if obs.artifact_digest is not None and not any(
            artifact.digest == obs.artifact_digest for artifact in state.artifacts.values()
        ):
            raise LedgerIntegrityError("observation is bound to an unknown artifact digest")
        state.observations[obs.observation_id] = obs

    @staticmethod
    def _commit_artifact(state: RunState, event: LedgerEvent) -> None:
        payload = ArtifactCommitted.model_validate(event.payload)
        artifact = payload.artifact
        if artifact.artifact_id in state.artifacts:
            raise LedgerIntegrityError(f"duplicate artifact: {artifact.artifact_id}")
        if artifact.digest != artifact.content_ref.digest:
            raise LedgerIntegrityError("artifact digest differs from its content reference")
        move = state.moves.get(artifact.created_by_move_id)
        if move is None or move.status != MoveStatus.RUNNING:
            raise LedgerIntegrityError("artifact requires its running move")
        if artifact.trajectory_id != move.trajectory_id:
            raise LedgerIntegrityError("artifact trajectory differs from its move")
        if any(parent not in state.artifacts for parent in artifact.parent_artifact_ids):
            raise LedgerIntegrityError("artifact parent is missing")
        state.artifacts[artifact.artifact_id] = artifact
        state.trajectories[artifact.trajectory_id].artifact_head_id = artifact.artifact_id

    @staticmethod
    def _commit_workspace(state: RunState, event: LedgerEvent) -> None:
        payload = WorkspaceCommitted.model_validate(event.payload)
        workspace = payload.workspace
        if workspace.workspace_id in state.workspaces:
            raise LedgerIntegrityError(f"duplicate workspace: {workspace.workspace_id}")
        if workspace.based_on_event_seq >= event.seq:
            raise LedgerIntegrityError("workspace cannot depend on its own or a future event")
        if (
            workspace.parent_workspace_id is not None
            and workspace.parent_workspace_id not in state.workspaces
        ):
            raise LedgerIntegrityError("workspace parent is missing")
        if workspace.created_by_move_id is not None:
            move = state.moves.get(workspace.created_by_move_id)
            if move is None or move.status != MoveStatus.RUNNING:
                raise LedgerIntegrityError("workspace requires its running move")
            if workspace.parent_workspace_id != move.based_on_workspace_id:
                raise LedgerIntegrityError("workspace lineage differs from its move base")
        if any(artifact_id not in state.artifacts for artifact_id in workspace.artifact_head_ids):
            raise LedgerIntegrityError("workspace references a missing artifact")
        if any(
            trajectory_id not in state.trajectories
            for trajectory_id in workspace.active_trajectory_ids
        ):
            raise LedgerIntegrityError("workspace references a missing trajectory")
        if any(obs_id not in state.observations for obs_id in workspace.consumed_observation_ids):
            raise LedgerIntegrityError("workspace consumed a missing observation")
        if payload.activate and workspace.parent_workspace_id != state.current_workspace_id:
            raise LedgerIntegrityError("workspace activation lost compare-and-swap")
        state.workspaces[workspace.workspace_id] = workspace
        if payload.activate:
            state.current_workspace_id = workspace.workspace_id
            if (
                state.finish_claim is not None
                and state.finish_claim.workspace_id != workspace.workspace_id
            ):
                state.finish_claim = None
            consumed_steering = set(workspace.consumed_observation_ids)
            state.pending_steering_ids = [
                item for item in state.pending_steering_ids if item not in consumed_steering
            ]

    @staticmethod
    def _finish_move(state: RunState, event: LedgerEvent) -> None:
        payload = MoveFinished.model_validate(event.payload)
        move = state.moves.get(payload.move_id)
        if move is None or move.status != MoveStatus.RUNNING:
            raise LedgerIntegrityError("only a running move can finish")
        if payload.success and payload.error:
            raise LedgerIntegrityError("successful move cannot carry an error")
        if not payload.success and not payload.error:
            raise LedgerIntegrityError("failed move must carry an error")
        for obs_id in payload.observation_ids:
            obs = state.observations.get(obs_id)
            if obs is None or obs.move_id != move.move_id:
                raise LedgerIntegrityError("move output observation is missing or has another owner")
        for artifact_id in payload.artifact_ids:
            artifact = state.artifacts.get(artifact_id)
            if artifact is None or artifact.created_by_move_id != move.move_id:
                raise LedgerIntegrityError("move output artifact is missing or has another owner")
        if payload.workspace_id is not None:
            workspace = state.workspaces.get(payload.workspace_id)
            if workspace is None or workspace.created_by_move_id != move.move_id:
                raise LedgerIntegrityError("move output workspace is missing or has another owner")
        move.status = MoveStatus.SUCCEEDED if payload.success else MoveStatus.FAILED
        move.finished_at = payload.finished_at
        move.observation_ids = list(payload.observation_ids)
        move.artifact_ids = list(payload.artifact_ids)
        move.workspace_id = payload.workspace_id
        move.error = payload.error
        state.active_move_ids.remove(move.move_id)
        state.usage = state.usage.plus(payload.usage_delta)

    @classmethod
    def _apply_move(cls, state: RunState, event: LedgerEvent) -> None:
        """Atomically admit one externally executed move result."""

        cls._require_active(state, event.event_type)
        payload = MoveApplied.model_validate(event.payload)
        move = state.moves.get(payload.move_id)
        if move is None or move.status != MoveStatus.RUNNING:
            raise LedgerIntegrityError("only a running move can be atomically applied")

        observation_ids = [item.observation_id for item in payload.observations]
        artifact_ids = [item.artifact_id for item in payload.artifacts]
        if len(observation_ids) != len(set(observation_ids)):
            raise LedgerIntegrityError("move application contains duplicate observations")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise LedgerIntegrityError("move application contains duplicate artifacts")
        if any(item in state.observations for item in observation_ids):
            raise LedgerIntegrityError("move application reuses an observation id")
        if any(item in state.artifacts for item in artifact_ids):
            raise LedgerIntegrityError("move application reuses an artifact id")

        trajectory_ids = [item.trajectory_id for item in payload.new_trajectories]
        if len(trajectory_ids) != len(set(trajectory_ids)):
            raise LedgerIntegrityError("move application contains duplicate trajectories")
        if any(item in state.trajectories for item in trajectory_ids):
            raise LedgerIntegrityError("move application reuses a trajectory id")
        possible_workspace_ids = set(state.workspaces)
        if payload.workspace is not None:
            possible_workspace_ids.add(payload.workspace.workspace_id)
        for trajectory in payload.new_trajectories:
            if trajectory.status != TrajectoryStatus.ACTIVE:
                raise LedgerIntegrityError("new trajectory must be active")
            if trajectory.parent_trajectory_id not in state.trajectories:
                raise LedgerIntegrityError("new trajectory parent is missing")
            if (
                trajectory.base_workspace_id is not None
                and trajectory.base_workspace_id not in possible_workspace_ids
            ):
                raise LedgerIntegrityError("new trajectory base workspace is missing")

        known_artifacts = set(state.artifacts) | set(artifact_ids)
        known_digests = {item.digest for item in state.artifacts.values()} | {
            item.digest for item in payload.artifacts
        }
        for artifact in payload.artifacts:
            if artifact.digest != artifact.content_ref.digest:
                raise LedgerIntegrityError("artifact digest differs from its content reference")
            if artifact.created_by_move_id != move.move_id:
                raise LedgerIntegrityError("move application artifact has another owner")
            if artifact.trajectory_id != move.trajectory_id:
                raise LedgerIntegrityError("move application artifact has another trajectory")
            if any(parent not in known_artifacts for parent in artifact.parent_artifact_ids):
                raise LedgerIntegrityError("move application artifact parent is missing")

        for observation in payload.observations:
            if observation.move_id != move.move_id:
                raise LedgerIntegrityError("move application observation has another owner")
            if observation.trajectory_id not in {None, move.trajectory_id}:
                raise LedgerIntegrityError("move application observation has another trajectory")
            if (
                observation.artifact_digest is not None
                and observation.artifact_digest not in known_digests
            ):
                raise LedgerIntegrityError("move observation is bound to an unknown artifact")

        resulting_workspace_id = state.current_workspace_id
        workspace = payload.workspace
        if workspace is not None:
            if workspace.workspace_id in state.workspaces:
                raise LedgerIntegrityError("move application reuses a workspace id")
            if workspace.created_by_move_id != move.move_id:
                raise LedgerIntegrityError("move application workspace has another owner")
            if workspace.parent_workspace_id != move.based_on_workspace_id:
                raise LedgerIntegrityError("workspace lineage differs from its move base")
            if payload.activate_workspace and workspace.parent_workspace_id != state.current_workspace_id:
                raise LedgerIntegrityError("workspace activation lost compare-and-swap")
            if workspace.based_on_event_seq >= event.seq:
                raise LedgerIntegrityError("workspace cannot depend on its own or a future event")
            if any(item not in known_artifacts for item in workspace.artifact_head_ids):
                raise LedgerIntegrityError("workspace references a missing artifact")
            known_trajectories = set(state.trajectories) | set(trajectory_ids)
            if any(item not in known_trajectories for item in workspace.active_trajectory_ids):
                raise LedgerIntegrityError("workspace references a missing trajectory")
            known_observations = set(state.observations) | set(observation_ids)
            if any(item not in known_observations for item in workspace.consumed_observation_ids):
                raise LedgerIntegrityError("workspace consumed a missing observation")
            if payload.activate_workspace:
                resulting_workspace_id = workspace.workspace_id

        existing_move_keys = {item.idempotency_key for item in state.moves.values()}
        new_move_ids: set[str] = set()
        new_move_keys: set[str] = set()
        for next_move in payload.next_moves:
            if next_move.move_id in state.moves or next_move.move_id in new_move_ids:
                raise LedgerIntegrityError("move application reuses a next-move id")
            if (
                next_move.idempotency_key in existing_move_keys
                or next_move.idempotency_key in new_move_keys
            ):
                raise LedgerIntegrityError("move application duplicates a move idempotency key")
            if next_move.status != MoveStatus.PROPOSED:
                raise LedgerIntegrityError("move application continuation must be proposed")
            existing_trajectory = state.trajectories.get(next_move.trajectory_id)
            is_new_trajectory = next_move.trajectory_id in trajectory_ids
            if (
                not is_new_trajectory
                and (
                    existing_trajectory is None
                    or existing_trajectory.status != TrajectoryStatus.ACTIVE
                )
            ):
                raise LedgerIntegrityError("move continuation trajectory is missing or inactive")
            known_workspaces = set(state.workspaces)
            if workspace is not None:
                known_workspaces.add(workspace.workspace_id)
            if (
                next_move.based_on_workspace_id is not None
                and next_move.based_on_workspace_id not in known_workspaces
            ):
                raise LedgerIntegrityError("move continuation workspace is missing")
            if next_move.based_on_workspace_id is None and resulting_workspace_id is not None:
                raise LedgerIntegrityError("move continuation omitted an available workspace")
            new_move_ids.add(next_move.move_id)
            new_move_keys.add(next_move.idempotency_key)

        finish_claim = payload.finish_claim
        known_observations = set(state.observations) | set(observation_ids)
        if finish_claim is not None:
            if finish_claim.workspace_id != resulting_workspace_id:
                raise LedgerIntegrityError("finish claim does not describe the resulting workspace")
            if any(item not in known_artifacts for item in finish_claim.artifact_head_ids):
                raise LedgerIntegrityError("finish claim references a missing artifact")
            if any(item not in known_observations for item in finish_claim.evidence_refs):
                raise LedgerIntegrityError("finish claim references missing evidence")
        if any(item not in known_observations for item in payload.blocker_evidence_refs):
            raise LedgerIntegrityError("blocker references missing evidence")

        for trajectory in payload.new_trajectories:
            state.trajectories[trajectory.trajectory_id] = trajectory
        for artifact in payload.artifacts:
            state.artifacts[artifact.artifact_id] = artifact
            state.trajectories[artifact.trajectory_id].artifact_head_id = artifact.artifact_id
        for observation in payload.observations:
            state.observations[observation.observation_id] = observation
        if workspace is not None:
            state.workspaces[workspace.workspace_id] = workspace
            if payload.activate_workspace:
                state.current_workspace_id = workspace.workspace_id
                if (
                    state.finish_claim is not None
                    and state.finish_claim.workspace_id != workspace.workspace_id
                ):
                    state.finish_claim = None
                consumed = set(workspace.consumed_observation_ids)
                state.pending_steering_ids = [
                    item for item in state.pending_steering_ids if item not in consumed
                ]

        move.status = MoveStatus.SUCCEEDED if payload.success else MoveStatus.FAILED
        move.finished_at = payload.finished_at
        move.observation_ids = observation_ids
        move.artifact_ids = artifact_ids
        move.workspace_id = workspace.workspace_id if workspace is not None else None
        move.error = payload.error
        state.active_move_ids.remove(move.move_id)
        state.usage = state.usage.plus(payload.usage_delta)

        if finish_claim is not None:
            state.finish_claim = finish_claim
        for next_move in payload.next_moves:
            state.moves[next_move.move_id] = next_move
        if payload.blocked_reason is not None:
            state.status = RunStatus.BLOCKED
            state.terminal_reason = payload.blocked_reason

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
            if state.finish_claim is None or payload.claim_id != state.finish_claim.claim_id:
                raise LedgerIntegrityError("satisfied run lacks its current finish claim")
            claim_observations = [
                item
                for item in state.observations.values()
                if item.kind == ObservationKind.CHALLENGE
                and item.metadata.get("claim_id") == payload.claim_id
            ]
            if any(
                item.challenge_verdict
                in {ChallengeVerdict.CHALLENGES, ChallengeVerdict.UNCERTAIN}
                and item.metadata.get("material_to_claim", True) is not False
                for item in claim_observations
            ):
                raise LedgerIntegrityError("satisfied run has unresolved challenge evidence")
            support = [state.observations.get(item) for item in payload.supporting_observation_ids]
            if not support or any(item is None for item in support):
                raise LedgerIntegrityError("satisfied run lacks supporting observations")
            if not any(
                item is not None
                and item.kind == ObservationKind.CHALLENGE
                and item.challenge_verdict == ChallengeVerdict.SUPPORTS
                and item.metadata.get("claim_id") == payload.claim_id
                for item in support
            ):
                raise LedgerIntegrityError("satisfied run lacks direct challenge support")
            claimed_digests = {
                state.artifacts[item].digest for item in state.finish_claim.artifact_head_ids
            }
            supported_digests = {
                item.artifact_digest
                for item in support
                if item is not None
                and item.kind == ObservationKind.CHALLENGE
                and item.challenge_verdict == ChallengeVerdict.SUPPORTS
                and item.metadata.get("claim_id") == payload.claim_id
            }
            if not claimed_digests.issubset(supported_digests):
                raise LedgerIntegrityError("satisfied run lacks exact artifact support")
        elif payload.status == "exhausted":
            if not state.usage.exhausted(state.objective.envelope):
                raise LedgerIntegrityError("run cannot be exhausted before a hard envelope")
        state.status = RunStatus(payload.status)
        state.terminal_reason = payload.reason
