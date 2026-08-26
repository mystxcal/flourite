"""Atomic validation and application of one completed intelligence move."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import LedgerIntegrityError
from .types import (
    ChallengeVerdict,
    FinishClaim,
    Move,
    MoveApplied,
    MoveStatus,
    Observation,
    ObservationKind,
    RunState,
    RunStatus,
    RunTerminated,
    TrajectoryStatus,
)


@dataclass(frozen=True, slots=True)
class TransitionIndex:
    """Names visible to every validator in one not-yet-committed transition."""

    observation_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    trajectory_ids: tuple[str, ...]
    known_observations: frozenset[str]
    known_artifacts: frozenset[str]
    known_artifact_digests: frozenset[str]
    known_trajectories: frozenset[str]
    known_workspaces: frozenset[str]
    resulting_workspace_id: str | None

    @classmethod
    def build(cls, state: RunState, payload: MoveApplied) -> TransitionIndex:
        observation_ids = tuple(item.observation_id for item in payload.observations)
        artifact_ids = tuple(item.artifact_id for item in payload.artifacts)
        trajectory_ids = tuple(item.trajectory_id for item in payload.new_trajectories)
        workspace_id = payload.workspace.workspace_id if payload.workspace is not None else None
        resulting_workspace_id = (
            workspace_id if payload.activate_workspace else state.current_workspace_id
        )
        return cls(
            observation_ids=observation_ids,
            artifact_ids=artifact_ids,
            trajectory_ids=trajectory_ids,
            known_observations=frozenset(state.observations) | frozenset(observation_ids),
            known_artifacts=frozenset(state.artifacts) | frozenset(artifact_ids),
            known_artifact_digests=frozenset(
                item.digest for item in (*state.artifacts.values(), *payload.artifacts)
            ),
            known_trajectories=frozenset(state.trajectories) | frozenset(trajectory_ids),
            known_workspaces=frozenset(state.workspaces)
            | ({workspace_id} if workspace_id is not None else set()),
            resulting_workspace_id=resulting_workspace_id,
        )


class AtomicMoveTransition:
    """Validate a complete move result, then mutate state exactly once.

    Validators are intentionally organized by domain aggregate.  None of them
    mutate the projection, so a rejected transition cannot partially leak into
    state even when this class is used outside the journal's transaction.
    """

    def __init__(self, state: RunState, payload: MoveApplied, *, event_seq: int) -> None:
        self.state = state
        self.payload = payload
        self.event_seq = event_seq
        self.move = self._running_move()
        self.index = TransitionIndex.build(state, payload)

    def apply(self) -> None:
        self._validate_identity_sets()
        self._validate_trajectories()
        self._validate_artifacts()
        self._validate_observations()
        self._validate_workspace()
        self._validate_continuations()
        self._validate_terminal_intent()
        self._commit()

    def _running_move(self) -> Move:
        move = self.state.moves.get(self.payload.move_id)
        if move is None or move.status != MoveStatus.RUNNING:
            raise LedgerIntegrityError("only a running move can be atomically applied")
        return move

    def _validate_identity_sets(self) -> None:
        self._require_unique(self.index.observation_ids, "duplicate observations")
        self._require_unique(self.index.artifact_ids, "duplicate artifacts")
        self._require_unique(self.index.trajectory_ids, "duplicate trajectories")
        if set(self.index.observation_ids) & self.state.observations.keys():
            raise LedgerIntegrityError("move application reuses an observation id")
        if set(self.index.artifact_ids) & self.state.artifacts.keys():
            raise LedgerIntegrityError("move application reuses an artifact id")
        if set(self.index.trajectory_ids) & self.state.trajectories.keys():
            raise LedgerIntegrityError("move application reuses a trajectory id")

    @staticmethod
    def _require_unique(values: tuple[str, ...], label: str) -> None:
        if len(values) != len(set(values)):
            raise LedgerIntegrityError(f"move application contains {label}")

    def _validate_trajectories(self) -> None:
        for trajectory in self.payload.new_trajectories:
            if trajectory.status != TrajectoryStatus.ACTIVE:
                raise LedgerIntegrityError("new trajectory must be active")
            if trajectory.parent_trajectory_id not in self.state.trajectories:
                raise LedgerIntegrityError("new trajectory parent is missing")
            if (
                trajectory.base_workspace_id is not None
                and trajectory.base_workspace_id not in self.index.known_workspaces
            ):
                raise LedgerIntegrityError("new trajectory base workspace is missing")

    def _validate_artifacts(self) -> None:
        for artifact in self.payload.artifacts:
            if artifact.digest != artifact.content_ref.digest:
                raise LedgerIntegrityError("artifact digest differs from its content reference")
            if artifact.created_by_move_id != self.move.move_id:
                raise LedgerIntegrityError("move application artifact has another owner")
            if artifact.trajectory_id != self.move.trajectory_id:
                raise LedgerIntegrityError("move application artifact has another trajectory")
            if any(
                parent not in self.index.known_artifacts for parent in artifact.parent_artifact_ids
            ):
                raise LedgerIntegrityError("move application artifact parent is missing")

    def _validate_observations(self) -> None:
        for observation in self.payload.observations:
            if observation.move_id != self.move.move_id:
                raise LedgerIntegrityError("move application observation has another owner")
            if observation.trajectory_id not in {None, self.move.trajectory_id}:
                raise LedgerIntegrityError("move application observation has another trajectory")
            if (
                observation.artifact_digest is not None
                and observation.artifact_digest not in self.index.known_artifact_digests
            ):
                raise LedgerIntegrityError("move observation is bound to an unknown artifact")

    def _validate_workspace(self) -> None:
        workspace = self.payload.workspace
        if workspace is None:
            return
        if workspace.workspace_id in self.state.workspaces:
            raise LedgerIntegrityError("move application reuses a workspace id")
        if workspace.created_by_move_id != self.move.move_id:
            raise LedgerIntegrityError("move application workspace has another owner")
        if workspace.parent_workspace_id != self.move.based_on_workspace_id:
            raise LedgerIntegrityError("workspace lineage differs from its move base")
        if (
            self.payload.activate_workspace
            and workspace.parent_workspace_id != self.state.current_workspace_id
        ):
            raise LedgerIntegrityError("workspace activation lost compare-and-swap")
        if workspace.based_on_event_seq >= self.event_seq:
            raise LedgerIntegrityError("workspace cannot depend on its own or a future event")
        if any(item not in self.index.known_artifacts for item in workspace.artifact_head_ids):
            raise LedgerIntegrityError("workspace references a missing artifact")
        if any(
            item not in self.index.known_trajectories for item in workspace.active_trajectory_ids
        ):
            raise LedgerIntegrityError("workspace references a missing trajectory")
        if any(
            item not in self.index.known_observations for item in workspace.consumed_observation_ids
        ):
            raise LedgerIntegrityError("workspace consumed a missing observation")

    def _validate_continuations(self) -> None:
        existing_keys = {item.idempotency_key for item in self.state.moves.values()}
        new_ids: set[str] = set()
        new_keys: set[str] = set()
        for move in self.payload.next_moves:
            if move.move_id in self.state.moves or move.move_id in new_ids:
                raise LedgerIntegrityError("move application reuses a next-move id")
            if move.idempotency_key in existing_keys or move.idempotency_key in new_keys:
                raise LedgerIntegrityError("move application duplicates a move idempotency key")
            self._validate_continuation_scope(move)
            new_ids.add(move.move_id)
            new_keys.add(move.idempotency_key)

    def _validate_continuation_scope(self, move: Move) -> None:
        if move.status != MoveStatus.PROPOSED:
            raise LedgerIntegrityError("move application continuation must be proposed")
        trajectory = self.state.trajectories.get(move.trajectory_id)
        if move.trajectory_id not in self.index.trajectory_ids and (
            trajectory is None or trajectory.status != TrajectoryStatus.ACTIVE
        ):
            raise LedgerIntegrityError("move continuation trajectory is missing or inactive")
        if (
            move.based_on_workspace_id is not None
            and move.based_on_workspace_id not in self.index.known_workspaces
        ):
            raise LedgerIntegrityError("move continuation workspace is missing")
        if move.based_on_workspace_id is None and self.index.resulting_workspace_id is not None:
            raise LedgerIntegrityError("move continuation omitted an available workspace")

    def _validate_terminal_intent(self) -> None:
        claim = self.payload.finish_claim
        if claim is not None:
            if claim.workspace_id != self.index.resulting_workspace_id:
                raise LedgerIntegrityError("finish claim does not describe the resulting workspace")
            if any(item not in self.index.known_artifacts for item in claim.artifact_head_ids):
                raise LedgerIntegrityError("finish claim references a missing artifact")
            if any(item not in self.index.known_observations for item in claim.evidence_refs):
                raise LedgerIntegrityError("finish claim references missing evidence")
        if any(
            item not in self.index.known_observations for item in self.payload.blocker_evidence_refs
        ):
            raise LedgerIntegrityError("blocker references missing evidence")

    def _commit(self) -> None:
        for trajectory in self.payload.new_trajectories:
            self.state.trajectories[trajectory.trajectory_id] = trajectory
        for artifact in self.payload.artifacts:
            self.state.artifacts[artifact.artifact_id] = artifact
            self.state.trajectories[artifact.trajectory_id].artifact_head_id = artifact.artifact_id
        for observation in self.payload.observations:
            self.state.observations[observation.observation_id] = observation
        self._commit_workspace()
        self._finish_move()
        self.state.finish_claim = self.payload.finish_claim or self.state.finish_claim
        for move in self.payload.next_moves:
            self.state.moves[move.move_id] = move
        if self.payload.blocked_reason is not None:
            self.state.status = RunStatus.BLOCKED
            self.state.terminal_reason = self.payload.blocked_reason

    def _commit_workspace(self) -> None:
        workspace = self.payload.workspace
        if workspace is None:
            return
        self.state.workspaces[workspace.workspace_id] = workspace
        if not self.payload.activate_workspace:
            return
        self.state.current_workspace_id = workspace.workspace_id
        if (
            self.state.finish_claim is not None
            and self.state.finish_claim.workspace_id != workspace.workspace_id
        ):
            self.state.finish_claim = None
        consumed = set(workspace.consumed_observation_ids)
        self.state.pending_steering_ids = [
            item for item in self.state.pending_steering_ids if item not in consumed
        ]

    def _finish_move(self) -> None:
        self.move.status = MoveStatus.SUCCEEDED if self.payload.success else MoveStatus.FAILED
        self.move.finished_at = self.payload.finished_at
        self.move.observation_ids = list(self.index.observation_ids)
        self.move.artifact_ids = list(self.index.artifact_ids)
        self.move.workspace_id = (
            self.payload.workspace.workspace_id if self.payload.workspace is not None else None
        )
        self.move.error = self.payload.error
        self.state.active_move_ids.remove(self.move.move_id)
        self.state.usage = self.state.usage.plus(self.payload.usage_delta)


class CompletionValidator:
    """Prove that a satisfaction event closes the exact live finish claim."""

    def __init__(self, state: RunState, payload: RunTerminated) -> None:
        self.state = state
        self.payload = payload

    def validate(self) -> None:
        claim = self._current_claim()
        relevant = self._challenge_observations(claim)
        if any(self._is_material_challenge(item) for item in relevant):
            raise LedgerIntegrityError("satisfied run has unresolved challenge evidence")
        support = self._supporting_observations()
        if not any(self._supports_claim(item, claim) for item in support):
            raise LedgerIntegrityError("satisfied run lacks direct challenge support")
        claimed_digests = {self.state.artifacts[item].digest for item in claim.artifact_head_ids}
        supported_digests = {
            item.artifact_digest for item in support if self._supports_claim(item, claim)
        }
        if not claimed_digests.issubset(supported_digests):
            raise LedgerIntegrityError("satisfied run lacks exact artifact support")

    def _current_claim(self) -> FinishClaim:
        claim = self.state.finish_claim
        if claim is None or self.payload.claim_id != claim.claim_id:
            raise LedgerIntegrityError("satisfied run lacks its current finish claim")
        return claim

    def _challenge_observations(self, claim: FinishClaim) -> list[Observation]:
        return [
            item
            for item in self.state.observations.values()
            if item.kind == ObservationKind.CHALLENGE
            and item.metadata.get("claim_id") == claim.claim_id
        ]

    @staticmethod
    def _is_material_challenge(observation: Observation) -> bool:
        return (
            observation.challenge_verdict
            in {ChallengeVerdict.CHALLENGES, ChallengeVerdict.UNCERTAIN}
            and observation.metadata.get("material_to_claim", True) is not False
        )

    def _supporting_observations(self) -> list[Observation]:
        support = [
            self.state.observations.get(item) for item in self.payload.supporting_observation_ids
        ]
        if not support or any(item is None for item in support):
            raise LedgerIntegrityError("satisfied run lacks supporting observations")
        return [item for item in support if item is not None]

    @staticmethod
    def _supports_claim(observation: Observation, claim: FinishClaim) -> bool:
        return (
            observation.kind == ObservationKind.CHALLENGE
            and observation.challenge_verdict == ChallengeVerdict.SUPPORTS
            and observation.metadata.get("claim_id") == claim.claim_id
        )
