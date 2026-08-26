"""Replay adapter for the pre-atomic kernel event stream.

New execution commits one ``move.applied`` transaction.  These granular event
handlers exist only so already-written ledgers remain reconstructible; they are
not part of the canonical execution path.
"""

from __future__ import annotations

from ..errors import LedgerIntegrityError
from ..ledger import LedgerEvent
from .types import (
    ArtifactCommitted,
    MoveFinished,
    MoveStatus,
    ObservationRecorded,
    RunState,
    WorkspaceCommitted,
)

LEGACY_MOVE_EVENTS = frozenset(
    {
        "observation.recorded",
        "artifact.committed",
        "workspace.committed",
        "move.finished",
    }
)


class LegacyMoveTransition:
    """Project one granular event written before atomic move commits."""

    def __init__(self, state: RunState, event: LedgerEvent) -> None:
        self.state = state
        self.event = event

    def apply(self) -> None:
        handlers = {
            "observation.recorded": self._record_observation,
            "artifact.committed": self._commit_artifact,
            "workspace.committed": self._commit_workspace,
            "move.finished": self._finish_move,
        }
        handlers[self.event.event_type]()

    def _record_observation(self) -> None:
        observation = ObservationRecorded.model_validate(self.event.payload).observation
        if observation.observation_id in self.state.observations:
            raise LedgerIntegrityError(f"duplicate observation: {observation.observation_id}")
        if observation.move_id is not None:
            move = self.state.moves.get(observation.move_id)
            if move is None or move.status != MoveStatus.RUNNING:
                raise LedgerIntegrityError("move observation requires its running move")
            if observation.trajectory_id not in {None, move.trajectory_id}:
                raise LedgerIntegrityError("observation trajectory differs from its move")
        if (
            observation.trajectory_id is not None
            and observation.trajectory_id not in self.state.trajectories
        ):
            raise LedgerIntegrityError("observation trajectory is missing")
        if observation.artifact_digest is not None and not any(
            artifact.digest == observation.artifact_digest
            for artifact in self.state.artifacts.values()
        ):
            raise LedgerIntegrityError("observation is bound to an unknown artifact digest")
        self.state.observations[observation.observation_id] = observation

    def _commit_artifact(self) -> None:
        artifact = ArtifactCommitted.model_validate(self.event.payload).artifact
        if artifact.artifact_id in self.state.artifacts:
            raise LedgerIntegrityError(f"duplicate artifact: {artifact.artifact_id}")
        if artifact.digest != artifact.content_ref.digest:
            raise LedgerIntegrityError("artifact digest differs from its content reference")
        move = self.state.moves.get(artifact.created_by_move_id)
        if move is None or move.status != MoveStatus.RUNNING:
            raise LedgerIntegrityError("artifact requires its running move")
        if artifact.trajectory_id != move.trajectory_id:
            raise LedgerIntegrityError("artifact trajectory differs from its move")
        if any(parent not in self.state.artifacts for parent in artifact.parent_artifact_ids):
            raise LedgerIntegrityError("artifact parent is missing")
        self.state.artifacts[artifact.artifact_id] = artifact
        self.state.trajectories[artifact.trajectory_id].artifact_head_id = artifact.artifact_id

    def _commit_workspace(self) -> None:
        payload = WorkspaceCommitted.model_validate(self.event.payload)
        workspace = payload.workspace
        if workspace.workspace_id in self.state.workspaces:
            raise LedgerIntegrityError(f"duplicate workspace: {workspace.workspace_id}")
        if workspace.based_on_event_seq >= self.event.seq:
            raise LedgerIntegrityError("workspace cannot depend on its own or a future event")
        if (
            workspace.parent_workspace_id is not None
            and workspace.parent_workspace_id not in self.state.workspaces
        ):
            raise LedgerIntegrityError("workspace parent is missing")
        if workspace.created_by_move_id is not None:
            move = self.state.moves.get(workspace.created_by_move_id)
            if move is None or move.status != MoveStatus.RUNNING:
                raise LedgerIntegrityError("workspace requires its running move")
            if workspace.parent_workspace_id != move.based_on_workspace_id:
                raise LedgerIntegrityError("workspace lineage differs from its move base")
        if any(
            artifact_id not in self.state.artifacts for artifact_id in workspace.artifact_head_ids
        ):
            raise LedgerIntegrityError("workspace references a missing artifact")
        if any(
            trajectory_id not in self.state.trajectories
            for trajectory_id in workspace.active_trajectory_ids
        ):
            raise LedgerIntegrityError("workspace references a missing trajectory")
        if any(
            observation_id not in self.state.observations
            for observation_id in workspace.consumed_observation_ids
        ):
            raise LedgerIntegrityError("workspace consumed a missing observation")
        if payload.activate and workspace.parent_workspace_id != self.state.current_workspace_id:
            raise LedgerIntegrityError("workspace activation lost compare-and-swap")
        self.state.workspaces[workspace.workspace_id] = workspace
        if payload.activate:
            self._activate_workspace(workspace.workspace_id)

    def _activate_workspace(self, workspace_id: str) -> None:
        workspace = self.state.workspaces[workspace_id]
        self.state.current_workspace_id = workspace_id
        if (
            self.state.finish_claim is not None
            and self.state.finish_claim.workspace_id != workspace_id
        ):
            self.state.finish_claim = None
        consumed = set(workspace.consumed_observation_ids)
        self.state.pending_steering_ids = [
            item for item in self.state.pending_steering_ids if item not in consumed
        ]

    def _finish_move(self) -> None:
        payload = MoveFinished.model_validate(self.event.payload)
        move = self.state.moves.get(payload.move_id)
        if move is None or move.status != MoveStatus.RUNNING:
            raise LedgerIntegrityError("only a running move can finish")
        if payload.success == bool(payload.error):
            message = (
                "successful move cannot carry an error"
                if payload.success
                else "failed move must carry an error"
            )
            raise LedgerIntegrityError(message)
        if any(
            (observation := self.state.observations.get(observation_id)) is None
            or observation.move_id != move.move_id
            for observation_id in payload.observation_ids
        ):
            raise LedgerIntegrityError("move output observation is missing or has another owner")
        if any(
            (artifact := self.state.artifacts.get(artifact_id)) is None
            or artifact.created_by_move_id != move.move_id
            for artifact_id in payload.artifact_ids
        ):
            raise LedgerIntegrityError("move output artifact is missing or has another owner")
        if payload.workspace_id is not None:
            workspace = self.state.workspaces.get(payload.workspace_id)
            if workspace is None or workspace.created_by_move_id != move.move_id:
                raise LedgerIntegrityError("move output workspace is missing or has another owner")
        move.status = MoveStatus.SUCCEEDED if payload.success else MoveStatus.FAILED
        move.finished_at = payload.finished_at
        move.observation_ids = list(payload.observation_ids)
        move.artifact_ids = list(payload.artifact_ids)
        move.workspace_id = payload.workspace_id
        move.error = payload.error
        self.state.active_move_ids.remove(move.move_id)
        self.state.usage = self.state.usage.plus(payload.usage_delta)
