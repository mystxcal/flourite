"""Typed domain objects for Flourite.

The models are intentionally small at worker boundaries and richer inside the
runtime. The ledger stores model dumps, so every state transition remains
replayable without depending on Python object identity.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator

ArtifactScope = Literal["targeted", "sequence", "whole_artifact", "release"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Impact(StrEnum):
    FATAL = "fatal"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Uncertainty(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CostBand(StrEnum):
    CHEAP = "cheap"
    MODERATE = "moderate"
    EXPENSIVE = "expensive"


class ValueBand(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class IndependenceClass(StrEnum):
    SAME_MODEL = "same_model"
    DIFFERENT_CONDITIONING = "different_conditioning"
    DETERMINISTIC_TOOL = "deterministic_tool"
    EXTERNAL_EVIDENCE = "external_evidence"
    HUMAN = "human"
    REAL_WORLD = "real_world"


class EvidenceModality(StrEnum):
    """What an observation actually measures, independent of who produced it."""

    SOURCE = "source"
    STRUCTURED_DATA = "structured_data"
    DETERMINISTIC_TEST = "deterministic_test"
    STATIC_VISUAL = "static_visual"
    TEMPORAL_VISUAL = "temporal_visual"
    AUDIO = "audio"
    INTERACTIVE = "interactive"
    EXTERNAL_OBSERVATION = "external_observation"
    HUMAN_OBSERVATION = "human_observation"


class FailureScope(StrEnum):
    """Earliest semantic boundary falsified by a release observation."""

    LOCAL = "local"
    SEQUENCE = "sequence"
    WHOLE_ARTIFACT = "whole_artifact"
    ARCHITECTURE = "architecture"
    TASK_FRAME = "task_frame"
    OBSERVATION = "observation"


class RecoveryRoute(StrEnum):
    """The smallest recovery move capable of addressing a failure's cause."""

    REPAIR = "repair"
    RECONSTRUCT = "reconstruct"
    REFRAME = "reframe"
    REOBSERVE = "reobserve"
    EXTERNAL_BLOCKER = "external_blocker"


class ActionKind(StrEnum):
    EXPLOIT = "exploit"
    EXPLORE = "explore"
    DISCRIMINATE = "discriminate"
    ACQUIRE = "acquire"
    REPAIR = "repair"
    TOOL = "tool"
    INSTRUMENT = "instrument"
    INTEGRATE = "integrate"
    REFRAME = "reframe"
    RECONSTRUCT = "reconstruct"
    CEILING_AUDIT = "ceiling_audit"
    MECHANISM_GRAFT = "mechanism_graft"
    STOP = "stop"


class SandboxPolicy(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class IssueStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DEFERRED = "deferred"
    INVALIDATED = "invalidated"


class CandidateStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ProbeStatus(StrEnum):
    PROPOSED = "proposed"
    RUNNING = "running"
    COMPLETE = "complete"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    SELECTED = "selected"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DOMINATED = "dominated"
    DEFERRED = "deferred"


class RunPhase(StrEnum):
    CREATED = "created"
    BOOTSTRAPPING = "bootstrapping"
    ACTIVE = "active"
    FINALIZING = "finalizing"
    RELEASE = "release"
    COMPLETE = "complete"
    FAILED = "failed"


class Role(StrEnum):
    STRONG = "strong"
    WORKER = "worker"
    CHEAP = "cheap"


class CharterProvenance(StrEnum):
    EXPLICIT = "explicit"
    STRONGLY_IMPLIED = "strongly_implied"
    TENTATIVE = "tentative"
    UNRESOLVED = "unresolved"


class ObligationStatus(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    INVALIDATED = "invalidated"


class CruxStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    DORMANT = "dormant"
    INVALIDATED = "invalidated"


class OverlayStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    MERGED = "merged"
    REJECTED = "rejected"
    DORMANT = "dormant"


class InstrumentStatus(StrEnum):
    PROPOSED = "proposed"
    BUILT = "built"
    VALIDATED = "validated"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"


class LeadContinuityStatus(StrEnum):
    UNINITIALIZED = "uninitialized"
    CONTINUOUS = "continuous"
    RECONSTRUCTED_VERIFIED = "reconstructed_verified"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class CognitiveTopology(StrEnum):
    LEAD = "lead"
    WORKER = "worker"
    DETERMINISTIC_TOOL = "deterministic_tool"
    EVIDENCE_BATCH = "evidence_batch"
    SPECIALIST = "specialist"
    OVERLAY = "overlay"
    USER_QUERY = "user_query"
    SUMMIT = "summit"


class EpistemicMode(StrEnum):
    """The action's dominant intent, used for attention and observability.

    ``AUTO`` keeps old ledgers and domain adapters compatible.  New adaptive
    runs may declare a mode so the context and live view can emphasize the
    right evidence.  The label never removes a trusted model's ordinary tools.
    """

    AUTO = "auto"
    THINK = "think"
    RETRIEVE = "retrieve"
    EXECUTE = "execute"
    BUILD = "build"
    VERIFY = "verify"


class SummitLineageStatus(StrEnum):
    SEED = "seed"
    ACTIVE = "active"
    PROTECTED = "protected"
    FALSIFIED = "falsified"
    DORMANT = "dormant"
    ELITE = "elite"
    MERGED = "merged"


class DiscoveryOperator(StrEnum):
    """Bounded transformations available to the experimental frontier."""

    DEVELOP = "develop"
    FALSIFY = "falsify"
    MUTATE = "mutate"
    CROSSOVER = "crossover"
    REVIVE = "revive"


class BlobRef(StrictModel):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    media_type: str = "application/octet-stream"
    relative_path: str
    original_name: str | None = None


class ArtifactRef(StrictModel):
    artifact_id: str
    version: int = Field(ge=1)
    blob: BlobRef
    kind: str = "markdown"
    summary: str = ""
    parent_artifact_id: str | None = None
    source_action_ids: list[str] = Field(default_factory=list)
    deliverables: list[BlobRef] = Field(default_factory=list)
    created_at: str


class BudgetContract(StrictModel):
    # These are operator-owned hard envelopes, not phase allocations.  The
    # adaptive resource governor may stop far below them, but never exceed
    # them.  ``None`` removes the round ceiling while preserving the call,
    # token, and wall envelopes.
    max_rounds: int | None = Field(default=None, ge=0)
    max_calls: int = Field(default=48, ge=1)
    max_parallel: int = Field(default=3, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_wall_seconds: int | None = Field(default=None, ge=1)
    synthesis_reserve_calls: int = Field(default=4, ge=1)


class ResourceSnapshot(StrictModel):
    calls: int = Field(default=0, ge=0)
    model_requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    wall_seconds: float = Field(default=0.0, ge=0.0)
    artifact_digest: str | None = None
    accepted_actions: int = Field(default=0, ge=0)
    informative_actions: int = Field(default=0, ge=0)
    failed_actions: int = Field(default=0, ge=0)
    release_blockers: int = Field(default=0, ge=0)
    high_impact_issues: int = Field(default=0, ge=0)
    active_cruxes: int = Field(default=0, ge=0)
    scoped_evidence: int = Field(default=0, ge=0)
    frontier_revision: int = Field(default=0, ge=0)
    objective_improvements: int = Field(default=0, ge=0)
    productive_discoveries: int = Field(default=0, ge=0)


class ProgressVector(StrictModel):
    """Runtime-observed movement kept separate by causal meaning.

    The controller deliberately does not collapse these dimensions into a
    universal quality score.  Their positive support controls continuation;
    their individual values remain visible for audit and task-specific policy.
    """

    quality: int = Field(default=0, ge=0)
    epistemic: int = Field(default=0, ge=0)
    feasibility: int = Field(default=0, ge=0)
    exploration: int = Field(default=0, ge=0)
    reliability: int = Field(default=0, ge=0)
    calls_spent: int = Field(default=0, ge=0)
    input_tokens_spent: int = Field(default=0, ge=0)
    output_tokens_spent: int = Field(default=0, ge=0)
    wall_seconds_spent: float = Field(default=0.0, ge=0.0)

    @property
    def productive(self) -> bool:
        return any(
            value > 0
            for value in (
                self.quality,
                self.epistemic,
                self.feasibility,
                self.exploration,
                self.reliability,
            )
        )


class ResourceDecisionKind(StrEnum):
    GRANT = "grant"
    FINALIZE = "finalize"
    EXTENSION_REQUIRED = "extension_required"


class ResourceDecision(StrictModel):
    kind: ResourceDecisionKind
    active_call_limit_before: int = Field(ge=1)
    active_call_limit_after: int = Field(ge=1)
    hard_call_limit: int = Field(ge=1)
    completion_reserve_calls: int = Field(ge=1)
    actionable_actions: int = Field(default=0, ge=0)
    active_commitments: int = Field(default=0, ge=0)
    gradient_score: int = Field(default=0, ge=0)
    progress_vector: ProgressVector = Field(default_factory=ProgressVector)
    stagnation_patience: int = Field(default=0, ge=0)
    progress_reasons: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    extension_recommended: bool = False
    snapshot: ResourceSnapshot


class ResourceState(StrictModel):
    mode: Literal["static", "adaptive"] = "adaptive"
    active_call_limit: int = Field(ge=1)
    hard_call_limit: int = Field(ge=1)
    grant_count: int = Field(default=0, ge=0)
    stagnant_grants: int = Field(default=0, ge=0)
    last_snapshot: ResourceSnapshot
    last_decision: ResourceDecision | None = None


class GoalContract(StrictModel):
    original_request: str
    deliverable: str
    hard_constraints: list[str] = Field(default_factory=list)
    soft_objectives: list[str] = Field(default_factory=list)
    stakes: Literal["low", "medium", "high", "critical"] = "medium"
    quality_floor: Literal["adequate", "high", "very_high", "frontier"] = "high"
    known_preferences: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    semantic_profiles: list[
        Literal["generic", "research", "formal", "decision", "creative", "media"]
    ] = Field(default_factory=list)
    artifact_modalities: list[EvidenceModality] = Field(default_factory=list)
    budget: BudgetContract = Field(default_factory=BudgetContract)


class TaskAmendment(StrictModel):
    amendment_id: str
    text: str
    created_at: str
    source: Literal["user", "operator", "system"] = "user"
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaskSource(StrictModel):
    original_text: str
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    amendments: list[TaskAmendment] = Field(default_factory=list)


class CharterAssertion(StrictModel):
    key: str
    statement: str
    provenance: CharterProvenance
    source_quote: str | None = None
    rationale: str = ""


class RequirementTrace(StrictModel):
    """Lossless link from an interpreted requirement back to the Task Source."""

    requirement_id: str
    source_text: str
    category: Literal["requirement", "prohibition", "preference", "hypothesis", "process"]
    release_blocking: bool = True
    evidence_modalities: list[EvidenceModality] = Field(default_factory=list)


class TaskCharter(StrictModel):
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(default=1, ge=1)
    deliverable: str
    real_world_purpose: str = ""
    audience: str = ""
    assertions: list[CharterAssertion] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    soft_objectives: list[str] = Field(default_factory=list)
    unacceptable_failures: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)
    requirement_traces: list[RequirementTrace] = Field(default_factory=list)
    unresolved_authority_questions: list[str] = Field(default_factory=list)


class ReframeWitness(StrictModel):
    original_success_condition: str
    new_representation: str
    mapping_back: str
    preserved_constraints: list[str] = Field(default_factory=list)
    leverage: str
    drift_risks: list[str] = Field(default_factory=list)
    invalidation_evidence: list[str] = Field(default_factory=list)


class ArtifactSpine(StrictModel):
    central_thesis: str
    architecture: list[str] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    hard_invariants: list[str] = Field(default_factory=list)
    invariant_revisions: list[InvariantRevision] = Field(default_factory=list)
    must_preserve: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    residual_uncertainty: list[str] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)


class ObligationDraft(StrictModel):
    local_key: str
    title: str
    requirement: str
    kind: Literal[
        "deliverable",
        "constraint",
        "claim",
        "decision",
        "construction",
        "verification",
        "coherence",
        "user_authority",
    ] = "claim"
    acceptance: str
    impact: Impact = Impact.HIGH
    depends_on_keys: list[str] = Field(default_factory=list)
    assumption_keys: list[str] = Field(default_factory=list)
    release_blocking: bool = True
    artifact_location_hint: str = ""
    source_requirement_ids: list[str] = Field(default_factory=list)
    required_evidence_modalities: list[EvidenceModality] = Field(default_factory=list)
    required_artifact_scope: ArtifactScope = "targeted"
    tags: list[str] = Field(default_factory=list)


class Obligation(StrictModel):
    obligation_id: str
    title: str
    requirement: str
    kind: str
    acceptance: str
    impact: Impact
    status: ObligationStatus = ObligationStatus.OPEN
    depends_on: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    artifact_location: str = ""
    release_blocking: bool = True
    residual_uncertainty: str = ""
    reopen_condition: str = ""
    resolution: str | None = None
    source_requirement_ids: list[str] = Field(default_factory=list)
    required_evidence_modalities: list[EvidenceModality] = Field(default_factory=list)
    required_artifact_scope: ArtifactScope = "targeted"
    tags: list[str] = Field(default_factory=list)
    created_seq: int = 0
    updated_seq: int = 0


class ObligationUpdate(StrictModel):
    obligation_id: str
    status: ObligationStatus | None = None
    acceptance: str | None = None
    evidence_references: list[str] = Field(default_factory=list)
    artifact_location: str | None = None
    residual_uncertainty: str | None = None
    reopen_condition: str | None = None
    resolution: str | None = None
    required_artifact_scope: ArtifactScope | None = None
    invalidate_dependents: bool = False


class CruxDraft(StrictModel):
    local_key: str
    title: str
    uncertainty: str
    decision_controlled: str
    competing_possibilities: list[str] = Field(default_factory=list)
    why_it_matters: str
    obligation_keys: list[str] = Field(default_factory=list)
    discriminating_evidence: list[str] = Field(default_factory=list)
    unlock_value: Impact = Impact.HIGH
    tags: list[str] = Field(default_factory=list)


class Crux(StrictModel):
    crux_id: str
    title: str
    uncertainty: str
    decision_controlled: str
    competing_possibilities: list[str] = Field(default_factory=list)
    why_it_matters: str
    obligation_ids: list[str] = Field(default_factory=list)
    discriminating_evidence: list[str] = Field(default_factory=list)
    unlock_value: Impact = Impact.HIGH
    status: CruxStatus = CruxStatus.ACTIVE
    resolution: str | None = None
    evidence_references: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_seq: int = 0
    updated_seq: int = 0


class CruxUpdate(StrictModel):
    crux_id: str
    status: CruxStatus | None = None
    uncertainty: str | None = None
    resolution: str | None = None
    evidence_references: list[str] = Field(default_factory=list)


class EliminatedDirection(StrictModel):
    """One semantic search family that should not be rediscovered blindly."""

    family: str
    failure_mechanism: str
    reopen_if: str = ""


class InvariantRevision(StrictModel):
    """Explicit causal retirement of a formerly useful working invariant."""

    statement: str
    failure_mechanism: str
    replacement: str = ""
    evidence_references: list[str] = Field(default_factory=list)


class FrontierKernel(StrictModel):
    """Small, loss-aware working memory for the current problem frontier.

    This is deliberately not the long-term memory or knowledge base.  It is
    the dense handoff between a solver and its frontier keeper: what must stay
    true, what remains live, what already failed for a reusable reason, and
    where attention belongs next.  Runtime-owned revision fields prevent a
    model from self-awarding movement by merely rewriting the summary.
    """

    bottleneck: str = ""
    invariants: list[str] = Field(default_factory=list)
    invariant_revisions: list[InvariantRevision] = Field(default_factory=list)
    live_hypotheses: list[str] = Field(default_factory=list)
    eliminated_directions: list[EliminatedDirection] = Field(default_factory=list)
    next_move: str = ""
    source_action_ids: list[str] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)
    last_advance_round: int = Field(default=0, ge=0)
    stagnant_rounds: int = Field(default=0, ge=0)


class ActionOutcome(StrictModel):
    outcome: str
    decision_effect: str
    obligation_effect: str = ""


class ContinuationContract(StrictModel):
    """A tiny option contract for work whose payoff is genuinely delayed.

    It is not a plan.  Later steps are admissible only after the prior step was
    integrated and produced a source-backed frontier change or confirmed
    observation.  This protects deep constructions without giving vague
    promises an unbounded claim on compute.
    """

    key: str = Field(min_length=1, max_length=120)
    thesis: str = Field(min_length=1)
    terminal_observation: str = Field(min_length=1)
    continuation_evidence: str = Field(min_length=1)
    kill_condition: str = Field(min_length=1)
    step: int = Field(default=1, ge=1, le=6)
    max_steps: int = Field(default=2, ge=2, le=6)

    @model_validator(mode="after")
    def step_within_bound(self) -> ContinuationContract:
        if self.step > self.max_steps:
            raise ValueError("continuation step cannot exceed max_steps")
        return self


class ActionContract(StrictModel):
    action_id: str | None = None
    target_crux_ids: list[str] = Field(default_factory=list)
    question: str
    possible_outcomes: list[ActionOutcome] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    evidence_channel: IndependenceClass
    expected_cost: CostBand
    stop_condition: str
    failure_handling: str = "Preserve raw output, record scope, and do not infer success."
    expected_unlock: str = ""
    artifact_scope: ArtifactScope = "targeted"
    causal_hypothesis: str = ""
    intervention: str = ""
    potency_check: str = ""
    decision_rule: str = ""
    observation_modalities: list[EvidenceModality] = Field(default_factory=list)
    continuation: ContinuationContract | None = None
    substantive: bool = True


class ContextLens(StrictModel):
    """Auditable, loss-aware projection supplied to one model call."""

    purpose: Literal["bootstrap", "action", "checkpoint", "synthesis", "release", "repair"]
    action_id: str | None = None
    task_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_scope: ArtifactScope = "targeted"
    artifact_view: Literal["none", "full", "preview_with_full"] = "none"
    obligation_ids: list[str] = Field(default_factory=list)
    crux_ids: list[str] = Field(default_factory=list)
    evidence_action_ids: list[str] = Field(default_factory=list)
    required_modalities: list[EvidenceModality] = Field(default_factory=list)
    included: list[str] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list)
    zoom_paths: list[str] = Field(default_factory=list)
    state_event_seq: int = Field(default=0, ge=0)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ObservedActionCost(StrictModel):
    """Provider-observed cost, never a worker estimate."""

    model_turns: int = Field(default=0, ge=0)
    provider_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tool_errors: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    wall_seconds: float = Field(default=0.0, ge=0.0)


class ActionObservation(StrictModel):
    """Worker-owned semantic observation against a pre-registered forecast."""

    action_id: str
    observed_result: str
    state_changes: list[str] = Field(default_factory=list)
    decisions_changed: list[str] = Field(default_factory=list)
    obligations_unlocked: list[str] = Field(default_factory=list)
    obligations_invalidated: list[str] = Field(default_factory=list)
    evidence_strength: Literal["none", "weak", "moderate", "strong", "decisive"] = "moderate"
    evidence_scope: str = ""
    reusable_assets: list[str] = Field(default_factory=list)
    # The worker may map its observation to a pre-registered outcome. The
    # runtime validates the index; an unanticipated result stays unmapped.
    matched_outcome_index: int | None = Field(default=None, ge=0)
    outcome_match: Literal["matched", "unmapped", "ambiguous", "invalid"] = "unmapped"
    outcome_rationale: str = ""
    recommended_next_action: str = ""


class ActionReceipt(ActionObservation):
    """Observation enriched with runtime-owned provenance and disposition."""

    observed_evidence_channels: list[IndependenceClass] = Field(default_factory=list)
    evidence_channel_confirmed: bool = False
    observed_cost: ObservedActionCost = Field(default_factory=ObservedActionCost)
    integration_status: Literal["pending", "accepted", "rejected", "failed"] = "pending"
    forecast_was_useful: bool = False
    context_lens_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    parent_artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SubstrateEntry(StrictModel):
    entry_id: str
    kind: Literal[
        "fact",
        "source",
        "calculation",
        "tool",
        "test",
        "claim",
        "counterexample",
        "constraint",
        "subproblem_result",
        "method",
        "residue",
    ]
    statement: str
    scope: str
    evidence_references: list[str] = Field(default_factory=list)
    source_action_id: str | None = None
    global_admission: bool = False
    confidence: Literal["low", "medium", "high", "verified"] = "medium"
    supersedes: list[str] = Field(default_factory=list)


class UnlockContract(StrictModel):
    potential_unlock: str
    blocking_dependency: str
    next_probe: str
    continuation_evidence: str
    kill_condition: str
    probe_allowance: int = Field(default=1, ge=0, le=5)
    development_allowance: int = Field(default=1, ge=0, le=5)
    probes_used: int = Field(default=0, ge=0)
    development_steps_used: int = Field(default=0, ge=0)


class SpeculativeOverlay(StrictModel):
    overlay_id: str
    name: str
    mechanism: str
    assumptions: list[str] = Field(default_factory=list)
    candidate_change: str
    distinctive_predictions: list[str] = Field(default_factory=list)
    behavioral_difference: str
    unresolved_dependencies: list[str] = Field(default_factory=list)
    counterevidence: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    status: OverlayStatus = OverlayStatus.PROPOSED
    unlock_contract: UnlockContract | None = None
    source_lineage_id: str | None = None
    artifact_blob: BlobRef | None = None


class InstrumentSpec(StrictModel):
    instrument_id: str
    name: str
    purpose: str
    target_crux_ids: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    build_plan: str
    validation_plan: str
    execution_plan: str
    expected_observations: list[str] = Field(default_factory=list)
    expected_decision_value: str
    reuse_scope: str = ""
    execution_mode: Literal["inside_provider", "operator"] = "inside_provider"
    status: InstrumentStatus = InstrumentStatus.PROPOSED
    artifact_references: list[str] = Field(default_factory=list)
    validation_evidence: list[str] = Field(default_factory=list)
    observation_evidence: list[str] = Field(default_factory=list)
    interpretation_scope: str = ""


class CeilingSensitivityScan(StrictModel):
    hidden_assumptions: list[str] = Field(default_factory=list)
    alternative_mechanisms: list[str] = Field(default_factory=list)
    weak_observation_channels: list[str] = Field(default_factory=list)
    holistic_tradeoffs: list[str] = Field(default_factory=list)
    representation_failures: list[str] = Field(default_factory=list)
    concrete_trigger: bool = False
    rationale: str = ""
    recommended_capabilities: list[
        Literal[
            "lineage", "reconstruction", "ceiling_audit", "mechanism_graft", "instrument", "reframe"
        ]
    ] = Field(default_factory=list)


class LeadContinuityAck(StrictModel):
    task_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    active_obligation_ids: list[str] = Field(default_factory=list)
    active_crux_ids: list[str] = Field(default_factory=list)
    artifact_spine_revision: int | None = Field(default=None, ge=1)


class LeadSessionState(StrictModel):
    thread_id: str | None = None
    status: LeadContinuityStatus = LeadContinuityStatus.UNINITIALIZED
    turns: int = 0
    last_call_id: str | None = None
    last_ack: LeadContinuityAck | None = None
    reconstruction_failures: int = 0
    degraded_reason: str | None = None


class SummitLineage(StrictModel):
    lineage_id: str
    name: str
    thesis: str
    mechanism: str
    assumptions: list[str] = Field(default_factory=list)
    enabling_dependencies: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    behavioral_descriptors: list[str] = Field(default_factory=list)
    parent_lineage_ids: list[str] = Field(default_factory=list)
    generation: int = Field(default=0, ge=0)
    development_history: list[str] = Field(default_factory=list)
    falsification_residue: list[str] = Field(default_factory=list)
    status: SummitLineageStatus = SummitLineageStatus.SEED
    quality: ValueBand = ValueBand.MEDIUM
    potential: ValueBand = ValueBand.MEDIUM
    novelty: ValueBand = ValueBand.MEDIUM
    leverage: ValueBand = ValueBand.MEDIUM
    robustness: ValueBand = ValueBand.LOW
    uncertainty: Uncertainty = Uncertainty.HIGH
    unlock_contract: UnlockContract | None = None
    overlay_id: str | None = None
    candidate_artifact: ArtifactRef | None = None


class DiscoveryRecord(StrictModel):
    """Runtime-owned evidence about one lineage's research productivity.

    Models may inspect these records but cannot write them through a worker
    envelope.  They are derived from receipts and lineage state transitions.
    """

    lineage_id: str
    attempts: int = Field(default=0, ge=0)
    informative_results: int = Field(default=0, ge=0)
    productive_results: int = Field(default=0, ge=0)
    accepted_results: int = Field(default=0, ge=0)
    independent_results: int = Field(default=0, ge=0)
    consecutive_stalls: int = Field(default=0, ge=0)
    covered_crux_ids: list[str] = Field(default_factory=list)
    covered_obligation_ids: list[str] = Field(default_factory=list)
    operator_history: list[DiscoveryOperator] = Field(default_factory=list)
    parent_lineage_ids: list[str] = Field(default_factory=list)
    last_action_id: str | None = None
    last_observation_seq: int = Field(default=0, ge=0)
    accepted_action_ids: list[str] = Field(default_factory=list)
    productive_action_ids: list[str] = Field(default_factory=list)
    objective_measurements: int = Field(default=0, ge=0)
    objective_improvements: int = Field(default=0, ge=0)
    best_objective: float | None = None
    last_objective: float | None = None
    objective_direction: Literal["maximize", "minimize"] | None = None


class ObjectiveMeasurement(StrictModel):
    """A runtime-observed domain objective, never a model-authored score."""

    primary_metric: str
    direction: Literal["maximize", "minimize"]
    metrics: dict[str, float]
    valid: bool
    constraint_violations: list[str] = Field(default_factory=list)
    command: str
    exit_code: int | None = None
    wall_seconds: float = Field(default=0.0, ge=0.0)
    evidence_blob: BlobRef | None = None
    detail: str = ""


class SemanticRegressionFinding(StrictModel):
    severity: Literal["fatal", "high", "medium", "low"]
    property: str
    prior_value: str
    final_value: str
    disposition: Literal["restore", "explicit_tradeoff", "irrelevant", "preserved"] = Field(
        description=(
            "Use 'restore' only when the current final artifact is still deficient and "
            "requires another edit; use 'preserved' when a prior deficiency has already "
            "been repaired in the current final artifact."
        )
    )
    rationale: str
    evidence_references: list[str] = Field(default_factory=list)


class CompletionClaim(StrictModel):
    obligation_id: str
    artifact_location: str
    evidence_or_test: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    status: Literal["satisfied", "partially_satisfied", "unsatisfied", "not_applicable"]
    remaining_uncertainty: str = ""
    reopen_condition: str = ""


class CompletionCase(StrictModel):
    task_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    claims: list[CompletionClaim] = Field(default_factory=list)
    strongest_rejected_alternative: str = ""
    why_rejected: str = ""
    preserved_insights: list[str] = Field(default_factory=list)
    unresolved_high_impact_risks: list[str] = Field(default_factory=list)


class ArenaJudgeOutput(StrictModel):
    winner: Literal["A", "B", "tie"]
    confidence: Literal["low", "medium", "high"] = "medium"
    rationale: str
    decisive_factors: list[str] = Field(default_factory=list)
    fatal_issues_a: list[str] = Field(default_factory=list)
    fatal_issues_b: list[str] = Field(default_factory=list)


class IssueDraft(StrictModel):
    local_key: str
    title: str
    description: str
    impact: Impact
    uncertainty: Uncertainty = Uncertainty.MEDIUM
    decision_sensitivity: str
    depends_on_keys: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Issue(StrictModel):
    issue_id: str
    title: str
    description: str
    impact: Impact
    uncertainty: Uncertainty
    status: IssueStatus = IssueStatus.OPEN
    decision_sensitivity: str
    depends_on: list[str] = Field(default_factory=list)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    resolution: str | None = None
    created_seq: int = 0
    updated_seq: int = 0


class IssueUpdate(StrictModel):
    issue_id: str
    status: IssueStatus | None = None
    title: str | None = None
    description: str | None = None
    impact: Impact | None = None
    uncertainty: Uncertainty | None = None
    decision_sensitivity: str | None = None
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    resolution: str | None = None
    tags_to_add: list[str] = Field(default_factory=list)


class CandidateDelta(StrictModel):
    delta_id: str
    target: str
    proposed_change: str
    expected_benefit: str
    dependencies: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    source_action_id: str
    status: CandidateStatus = CandidateStatus.PROPOSED
    artifact_blob: BlobRef | None = None


class Probe(StrictModel):
    probe_id: str
    target_issue_ids: list[str]
    method: str
    predicted_outcomes: list[str]
    scope: str
    blind_spots: list[str]
    independence_class: IndependenceClass
    cost: CostBand
    source_action_id: str
    status: ProbeStatus = ProbeStatus.PROPOSED
    finding: str | None = None
    evidence_references: list[str] = Field(default_factory=list)


class ActionProposal(StrictModel):
    kind: ActionKind
    target: str
    assignment: str
    issue_ids: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    obligation_keys: list[str] = Field(default_factory=list)
    crux_ids: list[str] = Field(default_factory=list)
    crux_keys: list[str] = Field(default_factory=list)
    impact: Impact
    cost: CostBand
    independence_class: IndependenceClass = IndependenceClass.SAME_MODEL
    topology: CognitiveTopology = CognitiveTopology.WORKER
    epistemic_mode: EpistemicMode = EpistemicMode.AUTO
    hypothesis_family: str = ""
    novelty_basis: str = ""
    execution_trigger: str = ""
    could_change_decision: bool = True
    expected_decision_effect: str
    reusable_value: ValueBand = ValueBand.LOW
    optimization_value: ValueBand = ValueBand.MEDIUM
    information_value: ValueBand = ValueBand.MEDIUM
    feasibility: ValueBand = ValueBand.HIGH
    artifact_scope: ArtifactScope = "targeted"
    observation_modalities: list[EvidenceModality] = Field(default_factory=list)
    causal_hypothesis: str = ""
    intervention: str = ""
    potency_check: str = ""
    decision_rule: str = ""
    distinctive_angle: str = ""
    stop_condition: str = (
        "Stop when the targeted uncertainty is resolved or no longer decision-relevant."
    )
    failure_handling: str = "Preserve raw output and do not infer success."
    outcome_branches: list[ActionOutcome] = Field(default_factory=list)
    continuation: ContinuationContract | None = None
    instrument: InstrumentSpec | None = None
    overlay_id: str | None = None
    lineage_id: str | None = None
    parent_lineage_ids: list[str] = Field(default_factory=list)
    discovery_operator: DiscoveryOperator | None = None
    summit_reason: str | None = None
    substantive: bool = True
    sandbox: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE
    network: bool = False


class ActionSpec(ActionProposal):
    action_id: str
    round_index: int = Field(ge=0)


class WorkerEnvelope(StrictModel):
    """The deliberately tiny worker boundary from the reference design."""

    @model_validator(mode="before")
    @classmethod
    def migrate_runtime_enriched_receipt(cls, value: Any) -> Any:
        """Replay v3.5 ledgers whose worker envelope stored a full receipt.

        Runtime-owned provenance moved out of the model boundary. Historical
        envelopes remain byte-for-byte replayable by materializing their old
        enriched payload as the ``ActionReceipt`` subtype. The generated worker
        schema still exposes only ``ActionObservation``.
        """

        if not isinstance(value, dict):
            return value
        raw_receipt = value.get("action_receipt")
        if not isinstance(raw_receipt, dict):
            return value
        runtime_fields = {
            "observed_evidence_channels",
            "evidence_channel_confirmed",
            "observed_cost",
            "integration_status",
            "forecast_was_useful",
        }
        if not runtime_fields.intersection(raw_receipt):
            return value
        migrated = dict(value)
        migrated["action_receipt"] = ActionReceipt.model_validate(raw_receipt)
        return migrated

    target: str
    result_or_artifact_reference: str
    findings: list[str]
    evidence_references: list[str] = Field(default_factory=list)
    evidence_artifact_paths: list[str] = Field(default_factory=list)
    unresolved_risks: list[str] = Field(default_factory=list)
    frame_break: str | None = None
    materiality: Literal["none", "low", "medium", "high", "fatal"] = "medium"
    decision_effect: str = ""
    scope: str = ""
    negative_result: bool = False
    action_receipt: SerializeAsAny[ActionObservation] | None = None
    substrate_entries: list[SubstrateEntry] = Field(default_factory=list)
    instrument: InstrumentSpec | None = None
    overlay: SpeculativeOverlay | None = None
    lineage: SummitLineage | None = None
    continuity_ack: LeadContinuityAck | None = None


class ActionRecord(StrictModel):
    spec: ActionSpec
    status: ActionStatus = ActionStatus.PROPOSED
    selected_reason: str | None = None
    rejection_reason: str | None = None
    result: WorkerEnvelope | None = None
    result_blob: BlobRef | None = None
    raw_events_blob: BlobRef | None = None
    patch_blob: BlobRef | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    contract: ActionContract | None = None
    receipt: ActionReceipt | None = None
    objective_measurement: ObjectiveMeasurement | None = None
    baseline_objective_measurement: ObjectiveMeasurement | None = None


class Usage(StrictModel):
    # `calls` is the sparse controller's provider-process budget currency.
    # `model_requests` counts every parent/tool-loop/subagent model request.
    calls: int = 0
    model_requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    wall_seconds: float = 0.0

    def plus(self, other: Usage) -> Usage:
        return Usage(
            calls=self.calls + other.calls,
            model_requests=self.model_requests + other.model_requests,
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens + other.reasoning_output_tokens,
            wall_seconds=self.wall_seconds + other.wall_seconds,
        )


class BootstrapOutput(StrictModel):
    goal_contract: GoalContract
    artifact_path: str
    artifact_summary: str
    artifact_scope: ArtifactScope = "targeted"
    issues: list[IssueDraft] = Field(default_factory=list)
    actions: list[ActionProposal] = Field(default_factory=list)
    task_charter: TaskCharter | None = None
    artifact_spine: ArtifactSpine | None = None
    frontier_kernel: FrontierKernel | None = None
    obligations: list[ObligationDraft] = Field(default_factory=list)
    cruxes: list[CruxDraft] = Field(default_factory=list)
    ceiling_scan: CeilingSensitivityScan | None = None
    overlays: list[SpeculativeOverlay] = Field(default_factory=list)
    lineages: list[SummitLineage] = Field(default_factory=list)
    continuity_ack: LeadContinuityAck | None = None
    quality_floor_reached: bool = False
    stop_reason: str | None = None
    frame_break: str | None = None


class CheckpointOutput(StrictModel):
    artifact_path: str
    artifact_summary: str
    task_charter: TaskCharter | None = None
    issue_updates: list[IssueUpdate] = Field(default_factory=list)
    new_issues: list[IssueDraft] = Field(default_factory=list)
    obligation_updates: list[ObligationUpdate] = Field(default_factory=list)
    new_obligations: list[ObligationDraft] = Field(default_factory=list)
    crux_updates: list[CruxUpdate] = Field(default_factory=list)
    new_cruxes: list[CruxDraft] = Field(default_factory=list)
    substrate_entries: list[SubstrateEntry] = Field(default_factory=list)
    overlays: list[SpeculativeOverlay] = Field(default_factory=list)
    lineages: list[SummitLineage] = Field(default_factory=list)
    artifact_spine: ArtifactSpine | None = None
    frontier_kernel: FrontierKernel | None = None
    reframe_witness: ReframeWitness | None = None
    ceiling_scan: CeilingSensitivityScan | None = None
    accepted_action_ids: list[str] = Field(default_factory=list)
    rejected_action_ids: list[str] = Field(default_factory=list)
    actions: list[ActionProposal] = Field(default_factory=list)
    continuity_ack: LeadContinuityAck | None = None
    stop: bool = False
    stop_reason: str | None = None
    frame_break: str | None = None
    clean_synthesis_needed: bool = False


class FinalOutput(StrictModel):
    artifact_path: str
    summary: str
    remaining_uncertainty: list[str] = Field(default_factory=list)
    artifact_spine: ArtifactSpine | None = None
    semantic_regression: list[SemanticRegressionFinding] = Field(default_factory=list)
    completion_case: CompletionCase | None = None
    preservation_decisions: list[str] = Field(default_factory=list)
    continuity_ack: LeadContinuityAck | None = None
    release_gate_recommended: bool = True


class ReleaseFinding(StrictModel):
    severity: Literal["fatal", "high", "medium"]
    title: str
    explanation: str
    evidence_reference: str | None = None
    repair_instruction: str | None = None
    scope: FailureScope = FailureScope.LOCAL
    causal_layer: str = ""
    falsified_assumptions: list[str] = Field(default_factory=list)
    invalidated_invariants: list[str] = Field(default_factory=list)
    recovery_route: RecoveryRoute = RecoveryRoute.REPAIR
    next_discriminator: str = ""


class ReleaseRecovery(StrictModel):
    """Causal handoff from the release membrane back into active search."""

    route: RecoveryRoute
    scope: FailureScope
    reason: str
    finding_titles: list[str] = Field(default_factory=list)
    causal_layers: list[str] = Field(default_factory=list)
    falsified_assumptions: list[str] = Field(default_factory=list)
    invalidated_invariants: list[str] = Field(default_factory=list)
    next_discriminators: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)


class ReleaseOutput(StrictModel):
    findings: list[ReleaseFinding] = Field(default_factory=list)
    requires_repair: bool = False
    releaseable: bool = True
    rationale: str = ""
    task_fidelity_passed: bool = True
    completion_case_valid: bool = True
    strongest_alternative_addressed: bool = True
    artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_modalities: list[EvidenceModality] = Field(default_factory=list)
    establishes: list[str] = Field(default_factory=list)
    cannot_establish: list[str] = Field(default_factory=list)


class RepairOutput(StrictModel):
    artifact_path: str
    repaired_findings: list[str] = Field(default_factory=list)
    remaining_uncertainty: list[str] = Field(default_factory=list)
    artifact_spine: ArtifactSpine | None = None
    completion_case: CompletionCase | None = None
    continuity_ack: LeadContinuityAck | None = None


class EvidenceRecord(StrictModel):
    evidence_id: str
    source_action_id: str | None = None
    kind: str
    summary: str
    scope: str
    artifact_scope: ArtifactScope = "targeted"
    independence_class: IndependenceClass
    references: list[str] = Field(default_factory=list)
    blob: BlobRef | None = None
    negative_result: bool = False
    modalities: list[EvidenceModality] = Field(default_factory=list)
    establishes: list[str] = Field(default_factory=list)
    cannot_establish: list[str] = Field(default_factory=list)
    artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RunState(StrictModel):
    run_id: str
    phase: RunPhase = RunPhase.CREATED
    created_at: str
    source_prompt: str = ""
    adapter: str = "generic"
    workspace: str | None = None
    contract: GoalContract | None = None
    current_artifact: ArtifactRef | None = None
    artifact_history: list[ArtifactRef] = Field(default_factory=list)
    issues: dict[str, Issue] = Field(default_factory=dict)
    actions: dict[str, ActionRecord] = Field(default_factory=dict)
    pending_action_ids: list[str] = Field(default_factory=list)
    candidate_deltas: dict[str, CandidateDelta] = Field(default_factory=dict)
    probes: dict[str, Probe] = Field(default_factory=dict)
    evidence: dict[str, EvidenceRecord] = Field(default_factory=dict)
    task_source: TaskSource | None = None
    task_charter: TaskCharter | None = None
    charter_history: list[TaskCharter] = Field(default_factory=list)
    artifact_spine: ArtifactSpine | None = None
    frontier_kernel: FrontierKernel | None = None
    frontier_advancing_action_ids: list[str] = Field(default_factory=list)
    obligations: dict[str, Obligation] = Field(default_factory=dict)
    cruxes: dict[str, Crux] = Field(default_factory=dict)
    substrate: dict[str, SubstrateEntry] = Field(default_factory=dict)
    overlays: dict[str, SpeculativeOverlay] = Field(default_factory=dict)
    instruments: dict[str, InstrumentSpec] = Field(default_factory=dict)
    summit_lineages: dict[str, SummitLineage] = Field(default_factory=dict)
    discovery_records: dict[str, DiscoveryRecord] = Field(default_factory=dict)
    summit_active: bool = False
    summit_reasons: list[str] = Field(default_factory=list)
    lead_session: LeadSessionState = Field(default_factory=LeadSessionState)
    semantic_regression_findings: list[SemanticRegressionFinding] = Field(default_factory=list)
    completion_case: CompletionCase | None = None
    action_receipts: dict[str, ActionReceipt] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    resource_state: ResourceState | None = None
    round_index: int = 0
    stop_requested: bool = False
    stop_reason: str | None = None
    final_artifact: ArtifactRef | None = None
    release: ReleaseOutput | None = None
    last_event_seq: int = 0
    last_event_hash: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def open_issues(self) -> list[Issue]:
        return [issue for issue in self.issues.values() if issue.status == IssueStatus.OPEN]

    @property
    def high_impact_open_issues(self) -> list[Issue]:
        return [issue for issue in self.open_issues if issue.impact in {Impact.FATAL, Impact.HIGH}]

    @property
    def active_cruxes(self) -> list[Crux]:
        return [item for item in self.cruxes.values() if item.status == CruxStatus.ACTIVE]

    @property
    def open_obligations(self) -> list[Obligation]:
        return [
            item
            for item in self.obligations.values()
            if item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
        ]

    @property
    def release_blocking_obligations(self) -> list[Obligation]:
        return [item for item in self.open_obligations if item.release_blocking]
