"""Lossless navigation context for Lead, Navigator, and Challenger moves."""

from __future__ import annotations

from pydantic import Field

from ..blobs import BlobStore
from ..core.types import (
    ArtifactVersion,
    ComputeEnvelope,
    ComputeUsage,
    CoreModel,
    FinishClaim,
    Move,
    MoveMode,
    Observation,
    RunState,
    Trajectory,
    WorkspaceVersion,
)


class ContextFrame(CoreModel):
    run_id: str
    mode: MoveMode
    objective_text: str
    amendments: list[str] = Field(default_factory=list)
    workspace_text: str | None = None
    workspace_summary: str | None = None
    current_workspace_id: str | None = None
    trajectories: list[Trajectory] = Field(default_factory=list)
    artifact_heads: list[ArtifactVersion] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    recent_moves: list[Move] = Field(default_factory=list)
    finish_claim: FinishClaim | None = None
    usage: ComputeUsage
    envelope: ComputeEnvelope
    capabilities: list[str] = Field(default_factory=list)


class ContextAssembler:
    """Build a compact map while leaving exact content in the blob store."""

    def __init__(self, *, blobs: BlobStore, recent_move_limit: int = 8) -> None:
        self._blobs = blobs
        self._recent_move_limit = recent_move_limit

    def build(
        self,
        state: RunState,
        *,
        mode: MoveMode,
        workspace_id: str | None = None,
        capabilities: list[str] | None = None,
    ) -> ContextFrame:
        workspace = self._workspace(state, workspace_id)
        heads = self._artifact_heads(state, workspace)
        moves = sorted(state.moves.values(), key=lambda item: item.proposed_at)
        amendments = [self._blobs.read_text(item.text_ref) for item in state.objective.amendments]
        return ContextFrame(
            run_id=state.run_id,
            mode=mode,
            objective_text=self._blobs.read_text(state.objective.original_text_ref),
            amendments=amendments,
            workspace_text=(self._blobs.read_text(workspace.document_ref) if workspace else None),
            workspace_summary=workspace.summary if workspace else None,
            current_workspace_id=workspace.workspace_id if workspace is not None else None,
            trajectories=list(state.trajectories.values()),
            artifact_heads=heads,
            observations=self._visible_observations(state, workspace),
            recent_moves=moves[-self._recent_move_limit :],
            finish_claim=state.finish_claim,
            usage=state.usage,
            envelope=state.objective.envelope,
            capabilities=capabilities or [],
        )

    @staticmethod
    def _workspace(state: RunState, workspace_id: str | None) -> WorkspaceVersion | None:
        return (
            state.workspaces.get(workspace_id)
            if workspace_id is not None
            else state.current_workspace
        )

    @staticmethod
    def _visible_observations(
        state: RunState,
        workspace: WorkspaceVersion | None,
    ) -> list[Observation]:
        consumed = set(workspace.consumed_observation_ids if workspace is not None else [])
        return [
            observation
            for observation in state.observations.values()
            if observation.observation_id not in consumed
            or observation.observation_id in state.pending_steering_ids
            or (
                state.finish_claim is not None
                and observation.kind.value == "challenge"
                and observation.metadata.get("claim_id") == state.finish_claim.claim_id
            )
        ]

    @staticmethod
    def _artifact_heads(
        state: RunState,
        workspace: WorkspaceVersion | None,
    ) -> list[ArtifactVersion]:
        head_ids = list(workspace.artifact_head_ids if workspace is not None else [])
        head_ids.extend(
            item.artifact_head_id
            for item in state.trajectories.values()
            if item.artifact_head_id is not None and item.artifact_head_id not in head_ids
        )
        return [state.artifacts[item] for item in head_ids if item in state.artifacts]
