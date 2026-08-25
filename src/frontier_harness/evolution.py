"""Fail-closed promotion logic for slow, trace-grounded harness evolution.

Harness changes are ordinary software candidates, but they do not promote on a
persuasive rationale or a training-task win.  A candidate must predict a named
failure-mode change, preserve protected behavior, survive matched-budget shadow
cases, and then generalize to sealed held-out cases.  This module deliberately
contains no model call and no weighted universal score.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .models import StrictModel
from .util import unique_preserving_order


class EvaluationSplit(StrEnum):
    SHADOW = "shadow"
    HELD_OUT = "held_out"


class TrialOutcome(StrEnum):
    CANDIDATE = "candidate"
    BASELINE = "baseline"
    TIE = "tie"


class HarnessCandidate(StrictModel):
    candidate_id: str
    baseline_fingerprint: str
    candidate_fingerprint: str
    failure_mode: str
    causal_change: str
    predicted_effect: str
    prediction_scope: str
    protected_properties: list[str] = Field(default_factory=list)
    changed_components: list[str] = Field(default_factory=list)
    shadow_case_ids: list[str] = Field(default_factory=list)
    held_out_case_digests: list[str] = Field(default_factory=list)
    source_trace_references: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct_candidate(self) -> HarnessCandidate:
        if self.baseline_fingerprint == self.candidate_fingerprint:
            raise ValueError("candidate fingerprint must differ from the baseline")
        if not self.failure_mode.strip() or not self.predicted_effect.strip():
            raise ValueError("harness evolution requires a falsifiable failure mode and prediction")
        return self


class HarnessTrial(StrictModel):
    candidate_id: str
    case_id: str
    case_digest: str
    split: EvaluationSplit
    seed: str
    baseline_budget_fingerprint: str
    candidate_budget_fingerprint: str
    outcome: TrialOutcome
    valid: bool = True
    predicted_effect_observed: bool = False
    protected_properties_preserved: list[str] = Field(default_factory=list)
    hard_regressions: list[str] = Field(default_factory=list)
    baseline_cost: dict[str, float] = Field(default_factory=dict)
    candidate_cost: dict[str, float] = Field(default_factory=dict)
    trace_references: list[str] = Field(default_factory=list)
    notes: str = ""

    @property
    def matched_budget(self) -> bool:
        return self.baseline_budget_fingerprint == self.candidate_budget_fingerprint


class EvolutionPolicy(StrictModel):
    minimum_shadow_cases: int = Field(default=2, ge=1)
    minimum_held_out_cases: int = Field(default=2, ge=1)
    require_held_out_win: bool = True
    require_trace_references: bool = True


class PromotionDecision(StrictModel):
    promotable: bool
    reasons: list[str]
    shadow_record: dict[str, int]
    held_out_record: dict[str, int]
    evidence_case_ids: list[str]


def _record(trials: list[HarnessTrial]) -> dict[str, int]:
    counts = {"candidate": 0, "baseline": 0, "tie": 0}
    for trial in trials:
        counts[trial.outcome.value] += 1
    return counts


class HarnessPromotionGate:
    def __init__(self, policy: EvolutionPolicy | None = None) -> None:
        self.policy = policy or EvolutionPolicy()

    def evaluate(
        self,
        candidate: HarnessCandidate,
        trials: list[HarnessTrial],
    ) -> PromotionDecision:
        relevant = [item for item in trials if item.candidate_id == candidate.candidate_id]
        shadow = [item for item in relevant if item.split == EvaluationSplit.SHADOW]
        held_out = [item for item in relevant if item.split == EvaluationSplit.HELD_OUT]
        reasons: list[str] = []

        if len({item.case_id for item in shadow}) < self.policy.minimum_shadow_cases:
            reasons.append("insufficient distinct shadow cases")
        if len({item.case_id for item in held_out}) < self.policy.minimum_held_out_cases:
            reasons.append("insufficient distinct held-out cases")
        if any(not item.valid for item in relevant):
            reasons.append("one or more trials are invalid")
        if any(not item.matched_budget for item in relevant):
            reasons.append("solver budgets are not matched")
        if any(item.hard_regressions for item in relevant):
            reasons.append("a protected behavior has a hard regression")
        if self.policy.require_trace_references and any(
            not item.trace_references for item in relevant
        ):
            reasons.append("one or more trials lack raw trace references")

        shadow_digests = {item.case_digest for item in shadow}
        held_out_digests = {item.case_digest for item in held_out}
        if shadow_digests.intersection(held_out_digests):
            reasons.append("shadow and held-out cases overlap")
        if candidate.held_out_case_digests and not held_out_digests.issubset(
            set(candidate.held_out_case_digests)
        ):
            reasons.append("held-out trial is absent from the sealed candidate manifest")

        required_properties = set(candidate.protected_properties)
        for item in relevant:
            missing = required_properties - set(item.protected_properties_preserved)
            if missing:
                reasons.append(
                    f"{item.case_id} lacks preservation evidence for: {', '.join(sorted(missing))}"
                )
        if shadow and not any(item.predicted_effect_observed for item in shadow):
            reasons.append("the pre-registered causal prediction was not observed in shadow")

        held_record = _record(held_out)
        if held_record["baseline"] > held_record["candidate"]:
            reasons.append("candidate loses the held-out head-to-head record")
        if self.policy.require_held_out_win and held_record["candidate"] == 0:
            reasons.append("candidate has no held-out win")

        return PromotionDecision(
            promotable=not reasons,
            reasons=unique_preserving_order(reasons),
            shadow_record=_record(shadow),
            held_out_record=held_record,
            evidence_case_ids=unique_preserving_order(item.case_id for item in relevant),
        )
