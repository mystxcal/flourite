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


class FailureDomain(StrEnum):
    """The layer that must recover an execution failure."""

    COMPONENT = "component"
    PROVIDER = "provider"
    ASSAY = "assay"
    EXTERNAL = "external"


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


class AssayStatus(StrEnum):
    """Whether an evaluator could actually inspect the claimed target."""

    VALID = "valid"
    INVALID = "invalid"


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
    based_on_event_seq: int = Field(default=0, ge=0)
    trajectory_id: str
    mode: MoveMode
    intent: str
    instructions: str = ""
    causal_checkpoint: bool = False
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
    claim_id: str | None = None
    assay_status: AssayStatus | None = None
    assay_coverage: str | None = None
    covered_claims: list[str] = Field(default_factory=list)
    material_to_claim: bool = True
    direct_inspection: bool = False
    quality_delta: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_assay_semantics(self) -> Observation:
        if self.assay_status == AssayStatus.INVALID:
            raise ValueError("invalid assay cannot enter semantic evidence")
        if self.challenge_verdict is not None and self.assay_status != AssayStatus.VALID:
            raise ValueError("challenge verdict requires a valid assay")
        if self.challenge_verdict is not None and not self.direct_inspection:
            raise ValueError("challenge verdict requires direct inspection")
        if self.challenge_verdict is not None and not (self.assay_coverage or "").strip():
            raise ValueError("challenge verdict requires concrete assay coverage")
        if self.covered_claims and self.challenge_verdict is None:
            raise ValueError("claim coverage requires a challenge verdict")
        if self.quality_delta and (
            self.assay_status != AssayStatus.VALID or not self.direct_inspection
        ):
            raise ValueError("quality-lens change requires valid direct inspection")
        return self


class WorkspaceVersion(CoreModel):
    workspace_id: str
    document_ref: ContentRef
    quality_ref: ContentRef | None = None
    summary: str
    decision_boundary: str | None = None
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
    quality_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_head_ids: list[str] = Field(min_length=1)
    satisfaction_claims: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    residual_uncertainty: list[str] = Field(default_factory=list)
    created_at: str

    @model_validator(mode="after")
    def validate_claims(self) -> FinishClaim:
        normalized = [item.strip() for item in self.satisfaction_claims]
        if any(not item for item in normalized):
            raise ValueError("finish claim contains an empty satisfaction claim")
        if len(normalized) != len(set(normalized)):
            raise ValueError("finish claim repeats a satisfaction claim")
        return self


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
    finish_claim: FinishClaim | None = None
    usage: ComputeUsage = Field(default_factory=ComputeUsage)
    terminal_reason: str | None = None
    terminal_evidence_refs: list[str] = Field(default_factory=list)
    pause_kind: PauseKind | None = None
    failure_domain: FailureDomain | None = None
    last_event_seq: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def current_workspace(self) -> WorkspaceVersion | None:
        if self.current_workspace_id is None:
            return None
        return self.workspaces[self.current_workspace_id]

    @model_validator(mode="after")
    def validate_references(self) -> RunState:
        self._validate_objective()
        self._validate_trajectories()
        self._validate_artifacts()
        self._validate_observations()
        self._validate_workspaces()
        self._validate_moves()
        self._validate_live_refs()
        self._validate_finish_refs()
        return self

    def _validate_objective(self) -> None:
        if self.root_trajectory_id not in self.trajectories:
            raise ValueError("root trajectory is missing")
        if self.objective.original_text_digest != self.objective.original_text_ref.digest:
            raise ValueError("objective digest differs from its content reference")
        amendment_ids = [item.amendment_id for item in self.objective.amendments]
        if len(amendment_ids) != len(set(amendment_ids)):
            raise ValueError("objective repeats an amendment")

    def _validate_trajectories(self) -> None:
        for trajectory_id, trajectory in self.trajectories.items():
            if trajectory_id != trajectory.trajectory_id:
                raise ValueError("trajectory map key differs from its identity")
            if (
                trajectory.parent_trajectory_id is not None
                and trajectory.parent_trajectory_id not in self.trajectories
            ):
                raise ValueError("trajectory parent is missing")
            if (
                trajectory.base_workspace_id is not None
                and trajectory.base_workspace_id not in self.workspaces
            ):
                raise ValueError("trajectory base workspace is missing")
            if (
                trajectory.artifact_head_id is not None
                and trajectory.artifact_head_id not in self.artifacts
            ):
                raise ValueError("trajectory artifact head is missing")

    def _validate_artifacts(self) -> None:
        for artifact_id, artifact in self.artifacts.items():
            if artifact_id != artifact.artifact_id:
                raise ValueError("artifact map key differs from its identity")
            if artifact.digest != artifact.content_ref.digest:
                raise ValueError("artifact digest differs from its content reference")
            if artifact.trajectory_id not in self.trajectories:
                raise ValueError("artifact trajectory is missing")
            if artifact.created_by_move_id not in self.moves:
                raise ValueError("artifact creator move is missing")
            if any(parent not in self.artifacts for parent in artifact.parent_artifact_ids):
                raise ValueError("artifact parent is missing")

    def _validate_observations(self) -> None:
        artifact_digests = {item.digest for item in self.artifacts.values()}
        for observation_id, observation in self.observations.items():
            if observation_id != observation.observation_id:
                raise ValueError("observation map key differs from its identity")
            if observation.move_id is not None and observation.move_id not in self.moves:
                raise ValueError("observation move is missing")
            if (
                observation.trajectory_id is not None
                and observation.trajectory_id not in self.trajectories
            ):
                raise ValueError("observation trajectory is missing")
            if (
                observation.artifact_digest is not None
                and observation.artifact_digest not in artifact_digests
            ):
                raise ValueError("observation artifact digest is missing")

    def _validate_workspaces(self) -> None:
        for workspace_id, workspace in self.workspaces.items():
            if workspace_id != workspace.workspace_id:
                raise ValueError("workspace map key differs from its identity")
            if (
                workspace.parent_workspace_id is not None
                and workspace.parent_workspace_id not in self.workspaces
            ):
                raise ValueError("workspace parent is missing")
            if any(item not in self.artifacts for item in workspace.artifact_head_ids):
                raise ValueError("workspace artifact is missing")
            if any(item not in self.trajectories for item in workspace.active_trajectory_ids):
                raise ValueError("workspace trajectory is missing")
            if len(workspace.active_trajectory_ids) != len(set(workspace.active_trajectory_ids)):
                raise ValueError("workspace repeats a trajectory")
            if any(item not in self.observations for item in workspace.consumed_observation_ids):
                raise ValueError("workspace consumed observation is missing")
            if (
                workspace.created_by_move_id is not None
                and workspace.created_by_move_id not in self.moves
            ):
                raise ValueError("workspace creator move is missing")

    def _validate_moves(self) -> None:
        for move_id, move in self.moves.items():
            if move_id != move.move_id:
                raise ValueError("move map key differs from its identity")
            if move.trajectory_id not in self.trajectories:
                raise ValueError("move trajectory is missing")
            if move.based_on_event_seq > self.last_event_seq:
                raise ValueError("move depends on a future event")
            if (
                move.based_on_workspace_id is not None
                and move.based_on_workspace_id not in self.workspaces
            ):
                raise ValueError("move base workspace is missing")
            if move.retry_of_move_id is not None and move.retry_of_move_id not in self.moves:
                raise ValueError("retried move is missing")
            if any(item not in self.observations for item in move.observation_ids):
                raise ValueError("move observation is missing")
            if any(item not in self.artifacts for item in move.artifact_ids):
                raise ValueError("move artifact is missing")
            if move.workspace_id is not None and move.workspace_id not in self.workspaces:
                raise ValueError("move workspace is missing")

    def _validate_live_refs(self) -> None:
        if (
            self.current_workspace_id is not None
            and self.current_workspace_id not in self.workspaces
        ):
            raise ValueError("current workspace is missing")
        if len(self.active_move_ids) != len(set(self.active_move_ids)):
            raise ValueError("run repeats an active move")
        if any(move_id not in self.moves for move_id in self.active_move_ids):
            raise ValueError("active move is missing")
        if any(
            self.moves[move_id].status != MoveStatus.RUNNING for move_id in self.active_move_ids
        ):
            raise ValueError("active move is not running")
        if len(self.pending_steering_ids) != len(set(self.pending_steering_ids)):
            raise ValueError("run repeats pending steering")
        if any(obs_id not in self.observations for obs_id in self.pending_steering_ids):
            raise ValueError("pending steering observation is missing")
        if any(
            self.observations[obs_id].kind != ObservationKind.STEERING
            for obs_id in self.pending_steering_ids
        ):
            raise ValueError("pending steering is not a steering observation")

    def _validate_finish_refs(self) -> None:
        if self.finish_claim is not None:
            claim = self.finish_claim
            if claim.workspace_id not in self.workspaces:
                raise ValueError("finish workspace is missing")
            if any(item not in self.artifacts for item in claim.artifact_head_ids):
                raise ValueError("finish artifact is missing")
            if any(item not in self.observations for item in claim.evidence_refs):
                raise ValueError("finish evidence is missing")
        if any(item not in self.observations for item in self.terminal_evidence_refs):
            raise ValueError("terminal evidence is missing")


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
        if not self.success and (
            self.artifacts
            or self.new_trajectories
            or self.workspace is not None
            or self.finish_claim is not None
            or self.blocked_reason is not None
        ):
            raise ValueError("failed move cannot commit semantic progress")
        if not self.success and (
            len(self.next_moves) > 1
            or (self.next_moves and self.next_moves[0].retry_of_move_id != self.move_id)
        ):
            raise ValueError("failed move may preserve only one exact retry of itself")
        if self.blocked_reason is not None and (self.finish_claim is not None or self.next_moves):
            raise ValueError("a blocked move cannot also continue or claim completion")
        if self.blocked_reason is not None and not self.blocker_evidence_refs:
            raise ValueError("a blocked move requires durable evidence")
        return self


class FinishClaimed(CoreModel):
    claim: FinishClaim


class RunPaused(CoreModel):
    reason: str = "operator pause"
    kind: PauseKind = PauseKind.OPERATOR
    failure_domain: FailureDomain | None = None

    @model_validator(mode="after")
    def validate_failure_domain(self) -> RunPaused:
        if self.kind == PauseKind.OPERATOR and self.failure_domain is not None:
            raise ValueError("operator pause cannot carry an execution failure domain")
        if self.kind == PauseKind.EXECUTION and self.failure_domain is None:
            raise ValueError("execution pause requires a causal failure domain")
        return self


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
