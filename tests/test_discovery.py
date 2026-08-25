from __future__ import annotations

from frontier_harness.discovery import ExperimentalFrontier
from frontier_harness.models import (
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
    SummitLineage,
    SummitLineageStatus,
    Uncertainty,
    ValueBand,
)


def _lineage(
    lineage_id: str,
    *,
    niche: str,
    support: bool = False,
    residue: bool = False,
    status: SummitLineageStatus = SummitLineageStatus.ACTIVE,
) -> SummitLineage:
    return SummitLineage(
        lineage_id=lineage_id,
        name=lineage_id,
        thesis=f"Thesis {lineage_id}",
        mechanism=f"Mechanism {lineage_id}",
        unresolved_questions=[f"Question {lineage_id}"],
        evidence_for=["supported observation"] if support else [],
        falsification_residue=["failed boundary"] if residue else [],
        behavioral_descriptors=[niche],
        status=status,
        quality=ValueBand.MEDIUM,
        potential=ValueBand.HIGH,
        novelty=ValueBand.HIGH,
        leverage=ValueBand.HIGH,
        robustness=ValueBand.MEDIUM,
        uncertainty=Uncertainty.HIGH,
    )


def _action(
    *,
    operator: DiscoveryOperator,
    lineage_id: str,
    parents: list[str],
) -> ActionSpec:
    return ActionSpec(
        action_id="act_1",
        round_index=1,
        kind="explore",
        target="target",
        assignment="assignment",
        impact=Impact.HIGH,
        cost=CostBand.MODERATE,
        independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
        topology=CognitiveTopology.SUMMIT,
        expected_decision_effect="change the decision",
        lineage_id=lineage_id,
        parent_lineage_ids=parents,
        discovery_operator=operator,
    )


def _receipt(*, independent: bool = False) -> ActionReceipt:
    return ActionReceipt(
        action_id="act_1",
        observed_result="A decisive boundary observation",
        decisions_changed=["retire the old mechanism"],
        evidence_strength="strong",
        observed_evidence_channels=(
            [IndependenceClass.DETERMINISTIC_TOOL]
            if independent
            else [IndependenceClass.DIFFERENT_CONDITIONING]
        ),
        evidence_channel_confirmed=independent,
        integration_status="accepted",
        forecast_was_useful=True,
    )


def test_mutation_requires_stagnation_or_falsification_signal() -> None:
    parent = _lineage("lin_parent", niche="route")
    controller = ExperimentalFrontier(stagnation_before_mutation=2)
    fresh = DiscoveryRecord(lineage_id=parent.lineage_id)
    fresh_ops = {
        plan.operator
        for plan in controller.candidates({parent.lineage_id: parent}, {parent.lineage_id: fresh})
    }
    assert DiscoveryOperator.MUTATE not in fresh_ops

    stalled = fresh.model_copy(update={"consecutive_stalls": 2})
    stalled_ops = {
        plan.operator
        for plan in controller.candidates({parent.lineage_id: parent}, {parent.lineage_id: stalled})
    }
    assert DiscoveryOperator.MUTATE in stalled_ops


def test_crossover_requires_supported_distinct_mechanisms() -> None:
    left = _lineage("lin_left", niche="geometry", support=True)
    right = _lineage("lin_right", niche="market", support=True)
    controller = ExperimentalFrontier()
    records = controller.seed_records({left.lineage_id: left, right.lineage_id: right})
    plans = controller.candidates({left.lineage_id: left, right.lineage_id: right}, records)
    crosses = [item for item in plans if item.operator == DiscoveryOperator.CROSSOVER]
    assert len(crosses) == 1
    assert crosses[0].parent_ids == (left.lineage_id, right.lineage_id)

    unsupported = right.model_copy(update={"evidence_for": []})
    plans = controller.candidates(
        {left.lineage_id: left, unsupported.lineage_id: unsupported}, records
    )
    assert not any(item.operator == DiscoveryOperator.CROSSOVER for item in plans)


def test_observation_backpropagates_progress_and_child_parentage() -> None:
    parent = _lineage("lin_parent", niche="route", support=True)
    child = _lineage("lin_child", niche="route-v2", support=True).model_copy(
        update={
            "parent_lineage_ids": [parent.lineage_id],
            "generation": 1,
            "quality": ValueBand.HIGH,
        }
    )
    controller = ExperimentalFrontier()
    action = _action(
        operator=DiscoveryOperator.MUTATE,
        lineage_id=parent.lineage_id,
        parents=[parent.lineage_id],
    )
    records = controller.observe(
        {},
        {parent.lineage_id: parent},
        action=action,
        returned=child,
        receipt=_receipt(independent=True),
        baseline_objective=None,
        objective=None,
        negative_result=False,
        event_seq=42,
    )
    parent_record = records[parent.lineage_id]
    assert parent_record.attempts == 1
    assert parent_record.informative_results == 1
    assert parent_record.productive_results == 0
    assert parent_record.independent_results == 1
    assert parent_record.operator_history == [DiscoveryOperator.MUTATE]
    assert records[child.lineage_id].parent_lineage_ids == [parent.lineage_id]

    integrated = controller.integrate(
        records,
        {action.action_id: ActionRecord(spec=action)},
        [action.action_id],
    )
    assert integrated[parent.lineage_id].accepted_results == 1
    assert integrated[parent.lineage_id].productive_results == 1
    repeated = controller.integrate(
        integrated,
        {action.action_id: ActionRecord(spec=action)},
        [action.action_id],
    )
    assert repeated[parent.lineage_id].productive_results == 1


def test_stalled_observation_is_counted_without_being_rewarded() -> None:
    parent = _lineage("lin_parent", niche="route")
    action = _action(
        operator=DiscoveryOperator.DEVELOP,
        lineage_id=parent.lineage_id,
        parents=[parent.lineage_id],
    )
    receipt = _receipt().model_copy(
        update={
            "decisions_changed": [],
            "evidence_strength": "weak",
            "outcome_match": "unmapped",
        }
    )
    records = ExperimentalFrontier.observe(
        {},
        {parent.lineage_id: parent},
        action=action,
        returned=None,
        receipt=receipt,
        baseline_objective=None,
        objective=None,
        negative_result=False,
        event_seq=7,
    )
    assert records[parent.lineage_id].attempts == 1
    assert records[parent.lineage_id].informative_results == 0
    assert records[parent.lineage_id].productive_results == 0
    assert records[parent.lineage_id].consecutive_stalls == 1


def test_objective_improvement_is_backpropagated_without_model_scoring() -> None:
    parent = _lineage("lin_parent", niche="route", support=True)
    existing = {
        parent.lineage_id: DiscoveryRecord(
            lineage_id=parent.lineage_id,
            objective_measurements=1,
            best_objective=10.0,
            last_objective=10.0,
            objective_direction="maximize",
        )
    }
    measured = ObjectiveMeasurement(
        primary_metric="score",
        direction="maximize",
        metrics={"score": 12.0},
        valid=True,
        command="python evaluate.py",
    )
    records = ExperimentalFrontier.observe(
        existing,
        {parent.lineage_id: parent},
        action=_action(
            operator=DiscoveryOperator.DEVELOP,
            lineage_id=parent.lineage_id,
            parents=[parent.lineage_id],
        ),
        returned=None,
        receipt=_receipt().model_copy(update={"decisions_changed": []}),
        baseline_objective=None,
        objective=measured,
        negative_result=False,
        event_seq=9,
    )
    record = records[parent.lineage_id]
    assert record.objective_measurements == 2
    assert record.last_objective == 12.0
    assert record.best_objective == 12.0
    assert record.productive_results == 1


def test_first_candidate_is_compared_to_an_isolated_baseline() -> None:
    parent = _lineage("lin_parent", niche="route", support=True)
    baseline = ObjectiveMeasurement(
        primary_metric="score",
        direction="maximize",
        metrics={"score": 15.0},
        valid=True,
        command="python evaluate.py",
    )
    candidate = baseline.model_copy(update={"metrics": {"score": 12.0}})
    records = ExperimentalFrontier.observe(
        {},
        {parent.lineage_id: parent},
        action=_action(
            operator=DiscoveryOperator.DEVELOP,
            lineage_id=parent.lineage_id,
            parents=[parent.lineage_id],
        ),
        returned=None,
        receipt=_receipt().model_copy(
            update={"decisions_changed": [], "evidence_strength": "weak"}
        ),
        baseline_objective=baseline,
        objective=candidate,
        negative_result=False,
        event_seq=10,
    )
    record = records[parent.lineage_id]
    assert record.objective_measurements == 2
    assert record.last_objective == 12.0
    assert record.best_objective == 15.0
    assert record.productive_results == 0
