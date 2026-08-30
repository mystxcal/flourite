"""The small typed shell around Flourite's expressive model workspace.

These contracts protect identity, lineage, provenance, resources, and legal
state transitions. They deliberately do not encode the task's semantic world.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class CoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RunStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    SATISFIED = "satisfied"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    STOPPED = "stopped"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SATISFIED,
            self.EXHAUSTED,
            self.BLOCKED,
            self.STOPPED,
            self.FAILED,
        }


class PauseKind(StrEnum):
    OPERATOR = "operator"
    EXECUTION = "execution"


class MoveMode(StrEnum):
    LEAD = "lead"
    NAVIGATE = "navigate"
    CHALLENGE = "challenge"
    ENVIRONMENT = "environment"


class MoveStatus(StrEnum):
    PROPOSED = "proposed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class TrajectoryStatus(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"
    ARCHIVED = "archived"
    FAILED = "failed"


class ObservationKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    ARTIFACT = "artifact"
    TEST = "test"
    SOURCE = "source"
    CHALLENGE = "challenge"
    STEERING = "steering"
    RESOURCE = "resource"
    ERROR = "error"


class ChallengeVerdict(StrEnum):
    SUPPORTS = "supports"
    CHALLENGES = "challenges"
    UNCERTAIN = "uncertain"


class PromotionGate(CoreModel):
    """Bind a challenge or revision move to one exact artifact head."""

    role: Literal["challenge", "revision"]
    target_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class PromotionDecision(CoreModel):
    """Controller-owned disposition for one exact artifact challenge."""

    decision_id: str
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    challenge_move_id: str
    disposition: Literal["granted", "denied"]
    evidence_observation_ids: list[str]
    direct_evidence_observation_ids: list[str]
    created_at: str


class PromotionLease(CoreModel):
    """Capability to build from, finish, or export one immutable artifact head."""

    lease_id: str
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_id: str
    issued_at: str


class ComputeEnvelope(CoreModel):
    """Operator-owned hard boundaries, never phase allocations."""

    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_model_turns: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_parallel: int = Field(default=1, ge=1)


class ComputeUsage(CoreModel):
    wall_seconds: float = Field(default=0.0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_turns: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)

    def plus(self, other: ComputeUsage) -> ComputeUsage:
        return ComputeUsage(
            wall_seconds=self.wall_seconds + other.wall_seconds,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            model_turns=self.model_turns + other.model_turns,
            tool_calls=self.tool_calls + other.tool_calls,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    def exhausted(self, envelope: ComputeEnvelope) -> list[str]:
        reasons: list[str] = []
        limits: tuple[tuple[str, float | int, float | int | None], ...] = (
            ("wall time", self.wall_seconds, envelope.max_wall_seconds),
            ("input tokens", self.input_tokens, envelope.max_input_tokens),
            ("output tokens", self.output_tokens, envelope.max_output_tokens),
            ("model turns", self.model_turns, envelope.max_model_turns),
            ("cost", self.cost_usd, envelope.max_cost_usd),
        )
        for label, used, limit in limits:
            if limit is not None and used >= limit:
                reasons.append(f"{label} envelope exhausted ({used} >= {limit})")
        return reasons


class ContentRef(CoreModel):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    media_type: str = "application/octet-stream"
    relative_path: str
    original_name: str | None = None


class ObjectiveAmendment(CoreModel):
    amendment_id: str
    text_ref: ContentRef
    created_at: str
    source: Literal["user", "operator"] = "user"


class Objective(CoreModel):
    objective_id: str
    original_text_ref: ContentRef
    original_text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    amendments: list[ObjectiveAmendment] = Field(default_factory=list)
    envelope: ComputeEnvelope = Field(default_factory=ComputeEnvelope)
    created_at: str


class ArtifactVersion(CoreModel):
    artifact_id: str
    content_ref: ContentRef
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_artifact_ids: list[str] = Field(default_factory=list)
    trajectory_id: str
    created_by_move_id: str
    deliverables: list[ContentRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class Trajectory(CoreModel):
    trajectory_id: str
    purpose: str
    base_workspace_id: str | None = None
    parent_trajectory_id: str | None = None
    artifact_head_id: str | None = None
    status: TrajectoryStatus = TrajectoryStatus.ACTIVE
    created_at: str


class Move(CoreModel):
    move_id: str
    retry_of_move_id: str | None = None
    based_on_workspace_id: str | None = None
    trajectory_id: str
    mode: MoveMode
    intent: str
    instructions: str = ""
    promotion_gate: PromotionGate | None = None
    input_refs: list[ContentRef] = Field(default_factory=list)
    declared_ceiling: ComputeEnvelope = Field(default_factory=ComputeEnvelope)
    idempotency_key: str
    status: MoveStatus = MoveStatus.PROPOSED
    proposed_at: str
    started_at: str | None = None
    finished_at: str | None = None
    observation_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    workspace_id: str | None = None
    error: str | None = None


class Observation(CoreModel):
    observation_id: str
    kind: ObservationKind
    summary: str
    source: str
    created_at: str
    move_id: str | None = None
    trajectory_id: str | None = None
    artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_ref: ContentRef | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    challenge_verdict: ChallengeVerdict | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceVersion(CoreModel):
    workspace_id: str
    document_ref: ContentRef
    summary: str
    based_on_event_seq: int = Field(ge=0)
    created_at: str
    parent_workspace_id: str | None = None
    artifact_head_ids: list[str] = Field(default_factory=list)
    active_trajectory_ids: list[str] = Field(default_factory=list)
    consumed_observation_ids: list[str] = Field(default_factory=list)
    created_by_move_id: str | None = None


class FinishClaim(CoreModel):
    claim_id: str
    workspace_id: str
    artifact_head_ids: list[str] = Field(default_factory=list)
    satisfaction_claims: list[str]
    evidence_refs: list[str] = Field(default_factory=list)
    residual_uncertainty: list[str] = Field(default_factory=list)
    created_at: str


class RunState(CoreModel):
    run_id: str
    objective: Objective
    status: RunStatus = RunStatus.ACTIVE
    started_at: str
    updated_at: str
    root_trajectory_id: str
    trajectories: dict[str, Trajectory]
    workspaces: dict[str, WorkspaceVersion] = Field(default_factory=dict)
    current_workspace_id: str | None = None
    artifacts: dict[str, ArtifactVersion] = Field(default_factory=dict)
    observations: dict[str, Observation] = Field(default_factory=dict)
    moves: dict[str, Move] = Field(default_factory=dict)
    active_move_ids: list[str] = Field(default_factory=list)
    pending_steering_ids: list[str] = Field(default_factory=list)
    promotion_decisions: list[PromotionDecision] = Field(default_factory=list)
    promotion_lease: PromotionLease | None = None
    pending_promotion_finish_claim: FinishClaim | None = None
    finish_claim: FinishClaim | None = None
    usage: ComputeUsage = Field(default_factory=ComputeUsage)
    terminal_reason: str | None = None
    pause_kind: PauseKind | None = None
    last_event_seq: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def current_workspace(self) -> WorkspaceVersion | None:
        if self.current_workspace_id is None:
            return None
        return self.workspaces[self.current_workspace_id]

    @model_validator(mode="after")
    def validate_references(self) -> RunState:
        if self.root_trajectory_id not in self.trajectories:
            raise ValueError("root trajectory is missing")
        if (
            self.current_workspace_id is not None
            and self.current_workspace_id not in self.workspaces
        ):
            raise ValueError("current workspace is missing")
        if any(move_id not in self.moves for move_id in self.active_move_ids):
            raise ValueError("active move is missing")
        if any(obs_id not in self.observations for obs_id in self.pending_steering_ids):
            raise ValueError("pending steering observation is missing")
        decision_ids = {item.decision_id for item in self.promotion_decisions}
        if len(decision_ids) != len(self.promotion_decisions):
            raise ValueError("promotion decision id is duplicated")
        for decision in self.promotion_decisions:
            if decision.challenge_move_id not in self.moves:
                raise ValueError("promotion decision challenge move is missing")
            if any(item not in self.observations for item in decision.evidence_observation_ids):
                raise ValueError("promotion decision evidence is missing")
            if not set(decision.direct_evidence_observation_ids).issubset(
                decision.evidence_observation_ids
            ):
                raise ValueError("promotion decision direct evidence is not in its evidence set")
        if self.promotion_lease is not None:
            if self.promotion_lease.decision_id not in decision_ids:
                raise ValueError("promotion lease decision is missing")
            decision = next(
                item
                for item in self.promotion_decisions
                if item.decision_id == self.promotion_lease.decision_id
            )
            if (
                decision.disposition != "granted"
                or decision.artifact_digest != self.promotion_lease.artifact_digest
            ):
                raise ValueError("promotion lease differs from its decision")
            root = self.trajectories[self.root_trajectory_id]
            if root.artifact_head_id is None:
                raise ValueError("promotion lease exists without a root artifact")
            if self.artifacts[root.artifact_head_id].digest != self.promotion_lease.artifact_digest:
                raise ValueError("promotion lease is stale for the root artifact")
        pending = self.pending_promotion_finish_claim
        if pending is not None:
            if pending.workspace_id not in self.workspaces:
                raise ValueError("deferred finish workspace is missing")
            if any(item not in self.artifacts for item in pending.artifact_head_ids):
                raise ValueError("deferred finish artifact is missing")
        return self


class RunStarted(CoreModel):
    objective: Objective
    root_trajectory: Trajectory


class SteeringReceived(CoreModel):
    observation: Observation


class MoveProposed(CoreModel):
    move: Move


class MoveStarted(CoreModel):
    move_id: str
    started_at: str


class MoveApplied(CoreModel):
    """One complete semantic result of an externally executed move.

    Model/tool execution may have many side effects inside its isolated capsule,
    but the durable run state observes it exactly once through this event.
    """

    move_id: str
    success: bool
    finished_at: str
    usage_delta: ComputeUsage = Field(default_factory=ComputeUsage)
    observations: list[Observation] = Field(default_factory=list)
    artifacts: list[ArtifactVersion] = Field(default_factory=list)
    new_trajectories: list[Trajectory] = Field(default_factory=list)
    workspace: WorkspaceVersion | None = None
    activate_workspace: bool = True
    promotion_decision: PromotionDecision | None = None
    promotion_lease: PromotionLease | None = None
    deferred_finish_claim: FinishClaim | None = None
    clear_deferred_finish_claim: bool = False
    finish_claim: FinishClaim | None = None
    next_moves: list[Move] = Field(default_factory=list)
    blocked_reason: str | None = None
    blocker_evidence_refs: list[str] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> MoveApplied:
        if self.workspace is None and not self.activate_workspace:
            raise ValueError("workspace activation flag requires a workspace")
        if self.success and self.error:
            raise ValueError("successful move application cannot carry an error")
        if not self.success and not self.error:
            raise ValueError("failed move application requires an error")
        if self.promotion_lease is not None and self.promotion_decision is None:
            raise ValueError("promotion lease requires its decision")
        if self.clear_deferred_finish_claim and self.promotion_decision is None:
            raise ValueError("only a promotion decision can clear a deferred finish claim")
        if self.deferred_finish_claim is not None and self.finish_claim is not None:
            raise ValueError("finish claim cannot be active and deferred together")
        continuations = (
            int(self.finish_claim is not None)
            + int(bool(self.next_moves))
            + int(self.blocked_reason is not None)
        )
        if continuations > 1:
            raise ValueError("move application may choose only one continuation")
        return self


class FinishClaimed(CoreModel):
    claim: FinishClaim


class RunPaused(CoreModel):
    reason: str = "operator pause"
    kind: PauseKind = PauseKind.OPERATOR


class RunResumed(CoreModel):
    reason: str = "operator resume"


class RunTerminated(CoreModel):
    status: Literal["satisfied", "exhausted", "blocked", "stopped", "failed"]
    reason: str
    claim_id: str | None = None
    supporting_observation_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_satisfaction(self) -> RunTerminated:
        if self.status == "satisfied" and self.claim_id is None:
            raise ValueError("satisfied termination requires a finish claim")
        return self
