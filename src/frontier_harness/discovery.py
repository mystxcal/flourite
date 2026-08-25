"""A bounded experimental frontier for exact-task upper-tail discovery.

The controller is deliberately not a second agent society.  It converts the
small Summit archive into one or two causal experiments at a time, using
runtime-owned progress evidence to decide whether to develop, falsify, mutate,
cross, or revive a lineage.  Semantic work remains with the provider; search
discipline, diversity pressure, and accounting remain deterministic.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from .models import (
    ActionKind,
    ActionOutcome,
    ActionProposal,
    ActionReceipt,
    ActionRecord,
    ActionSpec,
    CognitiveTopology,
    CostBand,
    DiscoveryOperator,
    DiscoveryRecord,
    Impact,
    IndependenceClass,
    ObjectiveMeasurement,
    SandboxPolicy,
    SummitLineage,
    SummitLineageStatus,
    Uncertainty,
    ValueBand,
)
from .util import normalize_key, unique_preserving_order

_VALUE = {ValueBand.NONE: 0, ValueBand.LOW: 1, ValueBand.MEDIUM: 2, ValueBand.HIGH: 3}
_UNCERTAINTY = {Uncertainty.LOW: 0, Uncertainty.MEDIUM: 1, Uncertainty.HIGH: 2}
_EVIDENCE_STRENGTH = {"none": 0, "weak": 1, "moderate": 2, "strong": 3, "decisive": 4}


def _niche(lineage: SummitLineage) -> str:
    if lineage.behavioral_descriptors:
        return normalize_key(lineage.behavioral_descriptors[0]) or "general"
    return normalize_key(lineage.mechanism.split(".", 1)[0]) or "general"


def _viable(lineage: SummitLineage) -> bool:
    return lineage.status not in {
        SummitLineageStatus.FALSIFIED,
        SummitLineageStatus.MERGED,
        SummitLineageStatus.DORMANT,
    }


def _has_support(lineage: SummitLineage, record: DiscoveryRecord) -> bool:
    return bool(lineage.evidence_for or record.informative_results or record.productive_results)


def _productivity_band(record: DiscoveryRecord) -> int:
    if record.attempts == 0:
        return 1
    if record.productive_results * 2 >= record.attempts:
        return 3
    if record.informative_results * 2 >= record.attempts:
        return 2
    return 0


@dataclass(frozen=True, slots=True)
class DiscoveryPlan:
    operator: DiscoveryOperator
    parent_ids: tuple[str, ...]
    target: str
    assignment: str
    expected_decision_effect: str
    stop_condition: str
    potential: int
    information: int
    diversity: int
    productivity: int
    cost: int

    @property
    def key(self) -> tuple[str, tuple[str, ...]]:
        return self.operator.value, self.parent_ids


def _dominates(left: DiscoveryPlan, right: DiscoveryPlan) -> bool:
    weakly = (
        left.potential >= right.potential
        and left.information >= right.information
        and left.diversity >= right.diversity
        and left.productivity >= right.productivity
        and left.cost <= right.cost
    )
    strictly = (
        left.potential > right.potential
        or left.information > right.information
        or left.diversity > right.diversity
        or left.productivity > right.productivity
        or left.cost < right.cost
    )
    return weakly and strictly


class ExperimentalFrontier:
    """Choose sparse, information-bearing transformations over Summit lineages."""

    def __init__(
        self,
        *,
        stagnation_before_mutation: int = 2,
        enable_mutation: bool = True,
        enable_crossover: bool = True,
    ) -> None:
        self.stagnation_before_mutation = stagnation_before_mutation
        self.enable_mutation = enable_mutation
        self.enable_crossover = enable_crossover

    @staticmethod
    def seed_records(
        lineages: Mapping[str, SummitLineage],
        existing: Mapping[str, DiscoveryRecord] | None = None,
    ) -> dict[str, DiscoveryRecord]:
        records = {key: value.model_copy(deep=True) for key, value in (existing or {}).items()}
        for lineage in lineages.values():
            records.setdefault(
                lineage.lineage_id,
                DiscoveryRecord(
                    lineage_id=lineage.lineage_id,
                    parent_lineage_ids=lineage.parent_lineage_ids,
                ),
            )
        return records

    def candidates(
        self,
        archive: Mapping[str, SummitLineage],
        records: Mapping[str, DiscoveryRecord],
    ) -> list[DiscoveryPlan]:
        viable = [item for item in archive.values() if _viable(item)]
        plans: list[DiscoveryPlan] = []
        for lineage in viable:
            record = records.get(lineage.lineage_id, DiscoveryRecord(lineage_id=lineage.lineage_id))
            potential = max(
                _VALUE[lineage.quality],
                _VALUE[lineage.potential],
                _VALUE[lineage.leverage],
            )
            productivity = _productivity_band(record)
            next_dependency = (
                lineage.unresolved_questions[0]
                if lineage.unresolved_questions
                else lineage.enabling_dependencies[0]
                if lineage.enabling_dependencies
                else lineage.mechanism
            )
            plans.append(
                DiscoveryPlan(
                    operator=DiscoveryOperator.DEVELOP,
                    parent_ids=(lineage.lineage_id,),
                    target=lineage.name,
                    assignment=(
                        "Advance this lineage through exactly one load-bearing dependency: "
                        f"{next_dependency}. Make a concrete state change or return a scoped "
                        "negative result; do not merely elaborate the thesis."
                    ),
                    expected_decision_effect=(
                        "Resolve one dependency and either strengthen, narrow, or retire the lineage."
                    ),
                    stop_condition=(
                        "Stop after one dependency is resolved or shown not to control the lineage."
                    ),
                    potential=potential,
                    information=max(1, _UNCERTAINTY[lineage.uncertainty]),
                    diversity=_VALUE[lineage.novelty],
                    productivity=productivity,
                    cost=1,
                )
            )

            needs_falsifier = (
                lineage.uncertainty != Uncertainty.LOW
                or not lineage.evidence_against
                or lineage.status == SummitLineageStatus.ELITE
            )
            if needs_falsifier:
                prediction = (
                    lineage.unlock_contract.kill_condition
                    if lineage.unlock_contract
                    else f"a result that cannot be explained by {lineage.mechanism}"
                )
                plans.append(
                    DiscoveryPlan(
                        operator=DiscoveryOperator.FALSIFY,
                        parent_ids=(lineage.lineage_id,),
                        target=lineage.name,
                        assignment=(
                            "Run the cheapest credible discriminator between this lineage and its "
                            "closest rival. Pre-register the rival predictions, seek an observation "
                            f"with a genuinely different evidence channel, and treat {prediction} "
                            "as a kill result. Preserve an informative negative result."
                        ),
                        expected_decision_effect=(
                            "Either independently support the lineage or cheaply prevent a false champion."
                        ),
                        stop_condition="Stop as soon as the competing predictions are discriminated.",
                        potential=potential,
                        information=3 if lineage.uncertainty == Uncertainty.HIGH else 2,
                        diversity=1,
                        productivity=max(1, productivity),
                        cost=1,
                    )
                )

            mutation_signal = (
                record.consecutive_stalls >= self.stagnation_before_mutation
                or bool(lineage.evidence_against)
                or bool(lineage.falsification_residue)
            )
            if self.enable_mutation and mutation_signal:
                residue = (
                    lineage.falsification_residue[-1]
                    if lineage.falsification_residue
                    else lineage.evidence_against[-1]
                    if lineage.evidence_against
                    else "the observed stagnation"
                )
                plans.append(
                    DiscoveryPlan(
                        operator=DiscoveryOperator.MUTATE,
                        parent_ids=(lineage.lineage_id,),
                        target=f"Mutation of {lineage.name}",
                        assignment=(
                            "Create exactly one child lineage that explains the parent's failure "
                            f"signal ({residue}) by changing a causal assumption or mechanism—not its "
                            "wording. Return a new lineage ID, this parent ID, generation + 1, a "
                            "consequential behavioral difference, and a bounded kill condition."
                        ),
                        expected_decision_effect=(
                            "Escape a demonstrated local basin while retaining only supported parent structure."
                        ),
                        stop_condition=(
                            "Stop after one causally distinct child and its decisive test are specified."
                        ),
                        potential=max(2, _VALUE[lineage.potential]),
                        information=2,
                        diversity=3,
                        productivity=max(1, productivity),
                        cost=1,
                    )
                )

        if self.enable_crossover:
            for left, right in combinations(viable, 2):
                left_record = records.get(
                    left.lineage_id, DiscoveryRecord(lineage_id=left.lineage_id)
                )
                right_record = records.get(
                    right.lineage_id, DiscoveryRecord(lineage_id=right.lineage_id)
                )
                if _niche(left) == _niche(right):
                    continue
                if not (_has_support(left, left_record) and _has_support(right, right_record)):
                    continue
                plans.append(
                    DiscoveryPlan(
                        operator=DiscoveryOperator.CROSSOVER,
                        parent_ids=(left.lineage_id, right.lineage_id),
                        target=f"Crossover: {left.name} x {right.name}",
                        assignment=(
                            "Create exactly one child from the complementary causal components of "
                            f"'{left.name}' and '{right.name}'. This must be a coherent mechanism, not "
                            "a union of features. State the interface, discarded incompatibilities, "
                            "new behavior, both parent IDs, generation + 1, and one test that could "
                            "show the composition is worse than its best parent."
                        ),
                        expected_decision_effect=(
                            "Test whether independently supported mechanisms compose into a stronger solution."
                        ),
                        stop_condition=(
                            "Stop after one coherent child and a parent-relative discriminator exist."
                        ),
                        potential=max(_VALUE[left.potential], _VALUE[right.potential], 2),
                        information=2,
                        diversity=3,
                        productivity=min(
                            _productivity_band(left_record), _productivity_band(right_record)
                        ),
                        cost=2,
                    )
                )

        for lineage in archive.values():
            if lineage.status not in {
                SummitLineageStatus.FALSIFIED,
                SummitLineageStatus.DORMANT,
            }:
                continue
            if not lineage.falsification_residue or lineage.potential != ValueBand.HIGH:
                continue
            if any(
                lineage.lineage_id in child.parent_lineage_ids
                and child.status not in {SummitLineageStatus.FALSIFIED, SummitLineageStatus.DORMANT}
                for child in archive.values()
            ):
                continue
            plans.append(
                DiscoveryPlan(
                    operator=DiscoveryOperator.REVIVE,
                    parent_ids=(lineage.lineage_id,),
                    target=f"Residual successor to {lineage.name}",
                    assignment=(
                        "Do not revive the falsified thesis. Extract exactly one still-valid residual "
                        "mechanism into a new child lineage with a distinct prediction and explicit "
                        "parentage; otherwise confirm that the residue has no remaining unlock value."
                    ),
                    expected_decision_effect=(
                        "Recover transferable information without repeatedly retrying a dead lineage."
                    ),
                    stop_condition="Stop after one residual successor is justified or the residue is closed.",
                    potential=2,
                    information=2,
                    diversity=3,
                    productivity=1,
                    cost=1,
                )
            )
        return plans

    def select(
        self,
        archive: Mapping[str, SummitLineage],
        records: Mapping[str, DiscoveryRecord],
        *,
        limit: int,
    ) -> list[DiscoveryPlan]:
        if limit <= 0:
            return []
        candidates = self.candidates(archive, records)
        non_dominated = [
            item
            for item in candidates
            if not any(_dominates(other, item) for other in candidates if other is not item)
        ]
        operator_usage = Counter(
            operator for record in records.values() for operator in record.operator_history
        )
        ordered = sorted(
            non_dominated,
            key=lambda item: (
                operator_usage[item.operator],
                -item.information,
                -item.potential,
                -item.diversity,
                -item.productivity,
                item.cost,
                item.operator.value,
                item.parent_ids,
            ),
        )
        selected: list[DiscoveryPlan] = []
        used_parents: set[str] = set()
        for item in ordered:
            if selected and used_parents.intersection(item.parent_ids):
                continue
            selected.append(item)
            used_parents.update(item.parent_ids)
            if len(selected) >= limit:
                break
        if len(selected) < limit:
            for item in ordered:
                if item in selected:
                    continue
                selected.append(item)
                if len(selected) >= limit:
                    break
        return selected

    @staticmethod
    def to_action(
        plan: DiscoveryPlan,
        *,
        crux_ids: Sequence[str] = (),
        obligation_ids: Sequence[str] = (),
        summit_reason: str,
    ) -> ActionProposal:
        primary = plan.parent_ids[0] if plan.parent_ids else None
        outcomes = [
            ActionOutcome(
                outcome="The experiment changes a load-bearing belief or produces a viable child.",
                decision_effect="Update the lineage frontier and the artifact only where evidence warrants.",
                obligation_effect="Record any obligation directly unlocked or invalidated.",
            ),
            ActionOutcome(
                outcome="The experiment is negative, falsifying, or inconclusive.",
                decision_effect="Retire, mutate, or narrow the lineage while preserving reusable residue.",
                obligation_effect="Do not change satisfied obligations without scoped contrary evidence.",
            ),
        ]
        return ActionProposal(
            kind=(
                ActionKind.DISCRIMINATE
                if plan.operator == DiscoveryOperator.FALSIFY
                else ActionKind.EXPLORE
            ),
            target=plan.target,
            assignment=plan.assignment,
            obligation_ids=list(obligation_ids),
            crux_ids=list(crux_ids),
            impact=Impact.HIGH,
            cost=CostBand.MODERATE,
            independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
            topology=CognitiveTopology.SUMMIT,
            expected_decision_effect=plan.expected_decision_effect,
            reusable_value=ValueBand.HIGH,
            distinctive_angle=f"{plan.operator.value}: {' + '.join(plan.parent_ids)}",
            stop_condition=plan.stop_condition,
            failure_handling=(
                "Preserve the failed attempt and its discriminating residue; never silently retry it."
            ),
            outcome_branches=outcomes,
            lineage_id=primary,
            parent_lineage_ids=list(plan.parent_ids),
            discovery_operator=plan.operator,
            summit_reason=summit_reason,
            sandbox=SandboxPolicy.WORKSPACE_WRITE,
        )

    @staticmethod
    def observe(
        existing: Mapping[str, DiscoveryRecord],
        archive_before: Mapping[str, SummitLineage],
        *,
        action: ActionSpec,
        returned: SummitLineage | None,
        receipt: ActionReceipt,
        baseline_objective: ObjectiveMeasurement | None,
        objective: ObjectiveMeasurement | None,
        negative_result: bool,
        event_seq: int,
    ) -> dict[str, DiscoveryRecord]:
        records = ExperimentalFrontier.seed_records(archive_before, existing)
        parent_ids = unique_preserving_order(
            [
                *action.parent_lineage_ids,
                *([action.lineage_id] if action.lineage_id else []),
            ]
        )
        operator = action.discovery_operator or DiscoveryOperator.DEVELOP
        old = archive_before.get(action.lineage_id or "")
        baseline_value = (
            baseline_objective.metrics.get(baseline_objective.primary_metric)
            if baseline_objective is not None and baseline_objective.valid
            else None
        )
        if baseline_value is not None and baseline_objective is not None:
            for parent_id in parent_ids:
                record = records.setdefault(parent_id, DiscoveryRecord(lineage_id=parent_id))
                if record.best_objective is None:
                    record.objective_measurements += 1
                    record.best_objective = baseline_value
                    record.last_objective = baseline_value
                    record.objective_direction = baseline_objective.direction
        evidence_delta = False
        state_delta = False
        if returned is not None:
            prior = archive_before.get(returned.lineage_id)
            if prior is None:
                state_delta = normalize_key(returned.mechanism) not in {
                    normalize_key(item.mechanism) for item in archive_before.values()
                }
                evidence_delta = bool(returned.evidence_for or returned.evidence_against)
            else:
                evidence_delta = bool(
                    set(returned.evidence_for) - set(prior.evidence_for)
                    or set(returned.evidence_against) - set(prior.evidence_against)
                    or set(returned.falsification_residue) - set(prior.falsification_residue)
                )
                state_delta = bool(
                    returned.status != prior.status
                    or returned.quality != prior.quality
                    or returned.robustness != prior.robustness
                    or returned.uncertainty != prior.uncertainty
                    or len(returned.unresolved_questions) < len(prior.unresolved_questions)
                )
        objective_value = (
            objective.metrics.get(objective.primary_metric)
            if objective is not None and objective.valid
            else None
        )
        prior_objectives: list[float] = []
        for parent_id in parent_ids:
            prior_value = records.get(parent_id)
            if prior_value is not None and prior_value.best_objective is not None:
                prior_objectives.append(prior_value.best_objective)
        objective_improved = False
        if objective_value is not None and objective is not None:
            objective_improved = (
                not prior_objectives
                or (objective.direction == "maximize" and objective_value > max(prior_objectives))
                or (objective.direction == "minimize" and objective_value < min(prior_objectives))
            )
        strong_observation = _EVIDENCE_STRENGTH[receipt.evidence_strength] >= 3
        informative = bool(
            negative_result
            or evidence_delta
            or state_delta
            or strong_observation
            or receipt.outcome_match == "matched"
            or objective_value is not None
            or bool(objective and objective.constraint_violations)
        )
        # Model-authored state changes are informative proposals, not accepted
        # productivity.  A result becomes productive only through an external
        # objective improvement here or Lead integration at checkpoint.
        productive = objective_improved
        independent = bool(
            receipt.evidence_channel_confirmed
            and any(
                channel
                not in {
                    IndependenceClass.SAME_MODEL,
                    IndependenceClass.DIFFERENT_CONDITIONING,
                }
                for channel in receipt.observed_evidence_channels
            )
        )
        for parent_id in parent_ids:
            record = records.setdefault(parent_id, DiscoveryRecord(lineage_id=parent_id))
            record.attempts += 1
            record.informative_results += int(informative)
            record.productive_results += int(productive)
            if productive and action.action_id not in record.productive_action_ids:
                record.productive_action_ids.append(action.action_id)
            record.independent_results += int(independent)
            record.consecutive_stalls = 0 if informative else record.consecutive_stalls + 1
            record.covered_crux_ids = unique_preserving_order(
                [*record.covered_crux_ids, *action.crux_ids]
            )
            record.covered_obligation_ids = unique_preserving_order(
                [*record.covered_obligation_ids, *action.obligation_ids]
            )
            record.operator_history.append(operator)
            record.last_action_id = action.action_id
            record.last_observation_seq = event_seq
            if objective_value is not None and objective is not None:
                record.objective_measurements += 1
                record.last_objective = objective_value
                record.objective_direction = objective.direction
                if record.best_objective is None or objective_improved:
                    record.best_objective = objective_value
                record.objective_improvements += int(objective_improved)
        if returned is not None:
            child = records.setdefault(
                returned.lineage_id,
                DiscoveryRecord(
                    lineage_id=returned.lineage_id,
                    parent_lineage_ids=unique_preserving_order(
                        [*returned.parent_lineage_ids, *parent_ids]
                    ),
                ),
            )
            child.parent_lineage_ids = unique_preserving_order(
                [*child.parent_lineage_ids, *returned.parent_lineage_ids, *parent_ids]
            )
            child.covered_crux_ids = unique_preserving_order(
                [*child.covered_crux_ids, *action.crux_ids]
            )
            child.covered_obligation_ids = unique_preserving_order(
                [*child.covered_obligation_ids, *action.obligation_ids]
            )
            if objective_value is not None and objective is not None:
                child.objective_measurements += 1
                child.last_objective = objective_value
                child.objective_direction = objective.direction
                if (
                    child.best_objective is None
                    or (
                        objective.direction == "maximize" and objective_value > child.best_objective
                    )
                    or (
                        objective.direction == "minimize" and objective_value < child.best_objective
                    )
                ):
                    child.best_objective = objective_value
        # A direct update to an incumbent with no declared parent still owns
        # its observation record.
        if not parent_ids and old is not None:
            records.setdefault(old.lineage_id, DiscoveryRecord(lineage_id=old.lineage_id))
        return records

    @staticmethod
    def integrate(
        existing: Mapping[str, DiscoveryRecord],
        actions: Mapping[str, ActionRecord],
        accepted_action_ids: Sequence[str],
    ) -> dict[str, DiscoveryRecord]:
        """Backpropagate Lead-accepted discoveries exactly once."""

        records = {key: value.model_copy(deep=True) for key, value in existing.items()}
        for action_id in accepted_action_ids:
            raw = actions.get(action_id)
            if raw is None or raw.spec.topology != CognitiveTopology.SUMMIT:
                continue
            spec = raw.spec
            parent_ids = unique_preserving_order(
                [
                    *spec.parent_lineage_ids,
                    *([spec.lineage_id] if spec.lineage_id else []),
                ]
            )
            for parent_id in parent_ids:
                record = records.setdefault(parent_id, DiscoveryRecord(lineage_id=parent_id))
                if action_id in record.accepted_action_ids:
                    continue
                record.accepted_action_ids.append(action_id)
                record.accepted_results += 1
                if action_id not in record.productive_action_ids:
                    record.productive_action_ids.append(action_id)
                    record.productive_results += 1
        return records
