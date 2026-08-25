"""Deterministic sparse-frontier action selection.

The scheduler deliberately uses ordinal comparisons and local Pareto filtering
instead of a tuned scalar reward. Semantic judgment proposes the slate; this
module removes obviously wasteful, dominated, duplicate, and over-correlated
work before any subscription budget is spent.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from .config import FrontierPolicy
from .models import (
    ActionKind,
    ActionSpec,
    CostBand,
    Impact,
    IndependenceClass,
    Obligation,
    ObligationStatus,
    ValueBand,
)
from .util import normalize_key

_IMPACT_RANK = {Impact.LOW: 1, Impact.MEDIUM: 2, Impact.HIGH: 3, Impact.FATAL: 4}
_COST_RANK = {CostBand.CHEAP: 1, CostBand.MODERATE: 2, CostBand.EXPENSIVE: 3}
_VALUE_RANK = {ValueBand.NONE: 0, ValueBand.LOW: 1, ValueBand.MEDIUM: 2, ValueBand.HIGH: 3}
_INDEPENDENCE_RANK = {
    IndependenceClass.SAME_MODEL: 0,
    IndependenceClass.DIFFERENT_CONDITIONING: 1,
    IndependenceClass.DETERMINISTIC_TOOL: 2,
    IndependenceClass.EXTERNAL_EVIDENCE: 2,
    IndependenceClass.HUMAN: 3,
    IndependenceClass.REAL_WORLD: 3,
}
_KIND_PRIORITY = {
    ActionKind.TOOL: 0,
    ActionKind.INSTRUMENT: 0,
    ActionKind.DISCRIMINATE: 1,
    ActionKind.ACQUIRE: 2,
    ActionKind.INTEGRATE: 3,
    ActionKind.REPAIR: 3,
    ActionKind.REFRAME: 4,
    ActionKind.RECONSTRUCT: 4,
    ActionKind.CEILING_AUDIT: 4,
    ActionKind.MECHANISM_GRAFT: 4,
    ActionKind.EXPLOIT: 5,
    ActionKind.EXPLORE: 6,
    ActionKind.STOP: 7,
}


@dataclass(slots=True)
class SelectionResult:
    selected: list[ActionSpec] = field(default_factory=list)
    selected_reasons: dict[str, str] = field(default_factory=dict)
    dominated: dict[str, str] = field(default_factory=dict)
    deferred: dict[str, str] = field(default_factory=dict)


class ActionScheduler:
    def __init__(self, policy: FrontierPolicy) -> None:
        self.policy = policy

    @staticmethod
    def _target_key(action: ActionSpec) -> str:
        semantic_ids = [*action.crux_ids, *action.obligation_ids, *action.issue_ids]
        issue_key = ",".join(sorted(semantic_ids))
        return issue_key or normalize_key(action.target)

    @staticmethod
    def _duplicate_key(action: ActionSpec) -> tuple[str, str, str]:
        return (
            action.kind.value,
            ActionScheduler._target_key(action),
            normalize_key(action.assignment)[:280],
        )

    @staticmethod
    def _correlation_key(action: ActionSpec) -> tuple[str, str, str]:
        return (
            ActionScheduler._target_key(action),
            action.kind.value,
            action.independence_class.value,
        )

    @staticmethod
    def _dominates(left: ActionSpec, right: ActionSpec) -> bool:
        """Return whether *left* locally dominates *right* on the same target."""

        if ActionScheduler._target_key(left) != ActionScheduler._target_key(right):
            return False
        weakly_better = (
            _IMPACT_RANK[left.impact] >= _IMPACT_RANK[right.impact]
            and _COST_RANK[left.cost] <= _COST_RANK[right.cost]
            and _VALUE_RANK[left.optimization_value] >= _VALUE_RANK[right.optimization_value]
            and _VALUE_RANK[left.information_value] >= _VALUE_RANK[right.information_value]
            and _VALUE_RANK[left.feasibility] >= _VALUE_RANK[right.feasibility]
            and _VALUE_RANK[left.reusable_value] >= _VALUE_RANK[right.reusable_value]
        )
        strictly_better = (
            _IMPACT_RANK[left.impact] > _IMPACT_RANK[right.impact]
            or _COST_RANK[left.cost] < _COST_RANK[right.cost]
            or _VALUE_RANK[left.optimization_value] > _VALUE_RANK[right.optimization_value]
            or _VALUE_RANK[left.information_value] > _VALUE_RANK[right.information_value]
            or _VALUE_RANK[left.feasibility] > _VALUE_RANK[right.feasibility]
            or _VALUE_RANK[left.reusable_value] > _VALUE_RANK[right.reusable_value]
        )
        return weakly_better and strictly_better

    @staticmethod
    def _release_blocker(action: ActionSpec, obligations: Mapping[str, Obligation]) -> bool:
        return any(
            obligation_id in obligations and obligations[obligation_id].release_blocking
            for obligation_id in action.obligation_ids
        )

    @staticmethod
    def _coverage_score(action: ActionSpec, obligations: Mapping[str, Obligation]) -> int:
        offered = set(action.observation_modalities)
        required = {
            modality
            for obligation_id in action.obligation_ids
            if obligation_id in obligations
            for modality in obligations[obligation_id].required_evidence_modalities
        }
        return len(offered.intersection(required))

    @staticmethod
    def _sort_key(
        action: ActionSpec, obligations: Mapping[str, Obligation] | None = None
    ) -> tuple[int, int, int, int, int, int, int, int, int, str]:
        obligation_map = obligations or {}
        return (
            -int(ActionScheduler._release_blocker(action, obligation_map)),
            -ActionScheduler._coverage_score(action, obligation_map),
            -_IMPACT_RANK[action.impact],
            -_VALUE_RANK[action.optimization_value],
            -_VALUE_RANK[action.information_value],
            -_VALUE_RANK[action.feasibility],
            -_VALUE_RANK[action.reusable_value],
            _COST_RANK[action.cost],
            _KIND_PRIORITY[action.kind],
            action.action_id,
        )

    def select(
        self,
        actions: list[ActionSpec],
        *,
        max_parallel: int,
        available_calls: int,
        obligations: Mapping[str, Obligation] | None = None,
        target_stalls: Mapping[str, int] | None = None,
        human_evidence_available: bool = False,
    ) -> SelectionResult:
        result = SelectionResult()
        obligations = obligations or {}
        target_stalls = target_stalls or {}
        if available_calls <= 0:
            result.deferred.update(
                {action.action_id: "reserved for final synthesis/release" for action in actions}
            )
            return result

        minimum_impact = _IMPACT_RANK[Impact(self.policy.minimum_action_impact)]
        expensive_minimum = _IMPACT_RANK[Impact(self.policy.expensive_probe_minimum_impact)]

        duplicate_best: dict[tuple[str, str, str], ActionSpec] = {}
        for action in actions:
            if action.kind == ActionKind.STOP:
                result.deferred[action.action_id] = "stop is handled by the semantic checkpoint"
                continue
            if self.policy.require_decision_relevance and not action.could_change_decision:
                result.dominated[action.action_id] = "cannot change a load-bearing decision"
                continue
            if (
                action.independence_class in {IndependenceClass.HUMAN, IndependenceClass.REAL_WORLD}
                and not human_evidence_available
            ):
                result.deferred[action.action_id] = (
                    "requested human/real-world evidence is not available in this run"
                )
                continue
            missing_dependencies = {
                dependency
                for obligation_id in action.obligation_ids
                if obligation_id in obligations
                for dependency in obligations[obligation_id].depends_on
                if dependency not in action.obligation_ids
                and dependency in obligations
                and obligations[dependency].status != ObligationStatus.SATISFIED
            }
            if missing_dependencies:
                result.deferred[action.action_id] = (
                    "blocked by unsatisfied obligation dependencies: "
                    + ", ".join(sorted(missing_dependencies))
                )
                continue
            target_key = self._target_key(action)
            if (
                target_stalls.get(target_key, 0) >= self.policy.max_stalled_actions_per_target
                and action.kind
                not in {
                    ActionKind.REFRAME,
                    ActionKind.RECONSTRUCT,
                    ActionKind.CEILING_AUDIT,
                    ActionKind.MECHANISM_GRAFT,
                }
            ):
                result.deferred[action.action_id] = (
                    "target reached the cross-round diminishing-return limit; require a "
                    "causally different reframe, reconstruction, ceiling audit, or mechanism graft"
                )
                continue
            if _IMPACT_RANK[action.impact] < minimum_impact:
                result.deferred[action.action_id] = "below configured minimum impact"
                continue
            if (
                action.cost == CostBand.EXPENSIVE
                and _IMPACT_RANK[action.impact] < expensive_minimum
            ):
                result.dominated[action.action_id] = (
                    "expensive action is not justified by its impact"
                )
                continue
            if (
                action.cost == CostBand.EXPENSIVE
                and action.kind
                in {
                    ActionKind.EXPLORE,
                    ActionKind.DISCRIMINATE,
                    ActionKind.INSTRUMENT,
                    ActionKind.CEILING_AUDIT,
                }
                and not all(
                    value.strip()
                    for value in (
                        action.causal_hypothesis,
                        action.intervention,
                        action.potency_check,
                        action.decision_rule,
                    )
                )
            ):
                result.dominated[action.action_id] = (
                    "expensive experiment lacks a causal hypothesis, intervention, potency "
                    "check, or decision rule"
                )
                continue
            duplicate_key = self._duplicate_key(action)
            incumbent = duplicate_best.get(duplicate_key)
            if incumbent is None:
                duplicate_best[duplicate_key] = action
                continue
            winner = min((incumbent, action), key=lambda item: self._sort_key(item, obligations))
            loser = action if winner is incumbent else incumbent
            result.dominated[loser.action_id] = (
                f"semantic duplicate dominated by {winner.action_id}"
            )
            duplicate_best[duplicate_key] = winner

        viable = list(duplicate_best.values())

        dominated_ids: set[str] = set()
        for right in viable:
            for left in viable:
                if left.action_id == right.action_id:
                    continue
                if self._dominates(left, right):
                    dominated_ids.add(right.action_id)
                    result.dominated[right.action_id] = (
                        f"locally Pareto-dominated by {left.action_id}"
                    )
                    break
        viable = [action for action in viable if action.action_id not in dominated_ids]

        if self.policy.correlation_discount:
            best_by_class: dict[tuple[str, str, str], ActionSpec] = {}
            for action in sorted(viable, key=lambda item: self._sort_key(item, obligations)):
                key = self._correlation_key(action)
                if key in best_by_class:
                    result.dominated[action.action_id] = (
                        "correlation-discounted: same target, method, and evidence channel"
                    )
                else:
                    best_by_class[key] = action
            viable = list(best_by_class.values())

        limit = min(
            self.policy.max_actions_per_batch,
            max_parallel,
            available_calls,
            len(viable),
        )
        target_counts: Counter[str] = Counter()
        for action in sorted(viable, key=lambda item: self._sort_key(item, obligations)):
            if len(result.selected) >= limit:
                result.deferred[action.action_id] = "outside the current sparse batch"
                continue
            target_key = self._target_key(action)
            if target_counts[target_key] >= self.policy.max_actions_per_target:
                result.deferred[action.action_id] = (
                    "target already has enough independent attention"
                )
                continue
            result.selected.append(action)
            target_counts[target_key] += 1
            result.selected_reasons[action.action_id] = (
                "highest non-dominated action under release coverage, impact, optimization/information value, feasibility, cost, and sparse frontier-width constraints"
            )

        selected_ids = {action.action_id for action in result.selected}
        for action in viable:
            if (
                action.action_id not in selected_ids
                and action.action_id not in result.dominated
                and action.action_id not in result.deferred
            ):
                result.deferred[action.action_id] = "not selected in this epoch"
        return result
