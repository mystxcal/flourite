"""Thin contracts between the deterministic kernel and model/tool execution."""

from __future__ import annotations

from typing import Protocol

from pydantic import Field, model_validator

from ..core.types import (
    AssayStatus,
    ChallengeVerdict,
    ComputeEnvelope,
    ComputeUsage,
    ContentRef,
    CoreModel,
    Move,
    MoveMode,
    ObservationKind,
    RunState,
)
from .context import ContextFrame


class MoveDirective(CoreModel):
    mode: MoveMode
    intent: str
    instructions: str = ""
    trajectory_id: str | None = None
    retry_of_move_id: str | None = None
    fork_purpose: str | None = None
    declared_ceiling: ComputeEnvelope = Field(default_factory=ComputeEnvelope)


class ObservationDraft(CoreModel):
    kind: ObservationKind
    summary: str
    source: str
    raw_ref: ContentRef | None = None
    artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    bind_to_new_artifact: bool = False
    challenge_verdict: ChallengeVerdict | None = None
    claim_id: str | None = None
    assay_status: AssayStatus | None = None
    assay_coverage: str | None = None
    material_to_claim: bool = True
    direct_inspection: bool = False
    quality_delta: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class ArtifactDraft(CoreModel):
    content_ref: ContentRef
    parent_artifact_ids: list[str] = Field(default_factory=list)
    deliverables: list[ContentRef] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class WorkspaceDraft(CoreModel):
    document: str
    quality_document: str | None = None
    summary: str
    consumed_observation_ids: list[str] = Field(default_factory=list)
    artifact_head_ids: list[str] = Field(default_factory=list)
    active_trajectory_ids: list[str] = Field(default_factory=list)
    activate: bool = True


class FinishDraft(CoreModel):
    satisfaction_claims: list[str]
    evidence_refs: list[str] = Field(default_factory=list)
    residual_uncertainty: list[str] = Field(default_factory=list)
    artifact_head_ids: list[str] = Field(default_factory=list)


class BlockerDraft(CoreModel):
    reason: str
    evidence_refs: list[str]


class MoveExecutionResult(CoreModel):
    success: bool = True
    observations: list[ObservationDraft] = Field(default_factory=list)
    artifact: ArtifactDraft | None = None
    workspace: WorkspaceDraft | None = None
    next_move: MoveDirective | None = None
    next_moves: list[MoveDirective] = Field(default_factory=list)
    finish: FinishDraft | None = None
    blocker: BlockerDraft | None = None
    usage: ComputeUsage = Field(default_factory=ComputeUsage)
    error: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> MoveExecutionResult:
        if self.next_move is not None and self.next_moves:
            raise ValueError("use next_move or next_moves, not both")
        terminal_intents = sum(
            (
                self.next_move is not None or bool(self.next_moves),
                self.finish is not None,
                self.blocker is not None,
            )
        )
        if terminal_intents > 1:
            raise ValueError("move result may choose only one continuation")
        if self.success and self.error:
            raise ValueError("successful execution cannot carry an error")
        if not self.success and not self.error:
            raise ValueError("failed execution must carry an error")
        return self


class MoveRunner(Protocol):
    async def run(
        self,
        *,
        move: Move,
        state: RunState,
        context: ContextFrame,
        recovering: bool,
    ) -> MoveExecutionResult: ...
