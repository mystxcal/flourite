from __future__ import annotations

from frontier_harness.config import FrontierPolicy, ProviderConfig
from frontier_harness.engine import FrontierEngine
from frontier_harness.models import (
    ActionKind,
    ActionSpec,
    CostBand,
    EvidenceModality,
    Impact,
    IndependenceClass,
    Obligation,
    ObligationStatus,
    Role,
    ValueBand,
)
from frontier_harness.scheduler import ActionScheduler


def action(
    action_id: str,
    *,
    target: str = "claim",
    assignment: str = "test the claim",
    impact: Impact = Impact.HIGH,
    cost: CostBand = CostBand.CHEAP,
    independence: IndependenceClass = IndependenceClass.DETERMINISTIC_TOOL,
    value: ValueBand = ValueBand.MEDIUM,
    kind: ActionKind = ActionKind.DISCRIMINATE,
) -> ActionSpec:
    return ActionSpec(
        action_id=action_id,
        round_index=1,
        kind=kind,
        target=target,
        assignment=assignment,
        impact=impact,
        cost=cost,
        independence_class=independence,
        could_change_decision=True,
        expected_decision_effect="accept or revise",
        reusable_value=value,
    )


def test_scheduler_removes_local_pareto_dominance() -> None:
    scheduler = ActionScheduler(FrontierPolicy(max_actions_per_batch=3))
    best = action("best")
    dominated = action(
        "dominated",
        impact=Impact.MEDIUM,
        cost=CostBand.MODERATE,
        independence=IndependenceClass.SAME_MODEL,
        value=ValueBand.LOW,
    )
    result = scheduler.select([dominated, best], max_parallel=3, available_calls=3)
    assert [item.action_id for item in result.selected] == ["best"]
    assert "dominated" in result.dominated


def test_scheduler_discounts_correlated_and_bounds_frontier_width() -> None:
    scheduler = ActionScheduler(FrontierPolicy(max_actions_per_batch=2, max_actions_per_target=1))
    first = action("a", assignment="counterexample search A")
    correlated = action("b", assignment="counterexample search B")
    distinct = action(
        "c",
        target="other claim",
        assignment="source check",
        independence=IndependenceClass.EXTERNAL_EVIDENCE,
        kind=ActionKind.ACQUIRE,
    )
    result = scheduler.select([first, correlated, distinct], max_parallel=2, available_calls=2)
    assert len(result.selected) == 2
    assert {item.action_id for item in result.selected} == {"a", "c"}
    assert "b" in result.dominated or "b" in result.deferred


def test_scheduler_preserves_synthesis_reserve_signal() -> None:
    scheduler = ActionScheduler(FrontierPolicy(max_actions_per_batch=2))
    result = scheduler.select([action("a")], max_parallel=2, available_calls=0)
    assert not result.selected
    assert result.deferred["a"] == "reserved for final synthesis/release"


def test_scheduler_defers_unavailable_human_evidence_and_cross_round_churn() -> None:
    scheduler = ActionScheduler(
        FrontierPolicy(max_actions_per_batch=2, max_stalled_actions_per_target=2)
    )
    human = action(
        "human",
        independence=IndependenceClass.HUMAN,
        assignment="ask an unavailable panel",
    )
    repeated = action("repeated", target="stalled target")
    result = scheduler.select(
        [human, repeated],
        max_parallel=2,
        available_calls=2,
        target_stalls={"stalled target": 2},
        human_evidence_available=False,
    )
    assert not result.selected
    assert "not available" in result.deferred["human"]
    assert "diminishing-return" in result.deferred["repeated"]


def test_stalled_target_remains_reachable_only_through_a_new_causal_frame() -> None:
    scheduler = ActionScheduler(
        FrontierPolicy(max_actions_per_batch=2, max_stalled_actions_per_target=2)
    )
    local = action("local", target="stalled target", kind=ActionKind.EXPLORE)
    reframe = action("reframe", target="stalled target", kind=ActionKind.REFRAME)
    result = scheduler.select(
        [local, reframe],
        max_parallel=2,
        available_calls=2,
        target_stalls={"stalled target": 2},
    )
    assert [item.action_id for item in result.selected] == ["reframe"]
    assert "diminishing-return" in result.deferred["local"]


def test_expensive_experiment_requires_a_real_causal_contract() -> None:
    scheduler = ActionScheduler(FrontierPolicy(max_actions_per_batch=1))
    vague = action("vague", cost=CostBand.EXPENSIVE, kind=ActionKind.INSTRUMENT)
    causal = vague.model_copy(
        update={
            "action_id": "causal",
            "causal_hypothesis": "The codec is causing the observed freeze.",
            "intervention": "Change only the codec.",
            "potency_check": "Confirm the encoded codec changed.",
            "decision_rule": "Reject the hypothesis if freeze duration is unchanged.",
        }
    )
    result = scheduler.select([vague, causal], max_parallel=1, available_calls=1)
    assert [item.action_id for item in result.selected] == ["causal"]
    assert "lacks a causal hypothesis" in result.dominated["vague"]


def test_scheduler_respects_stage_dependencies_and_prefers_missing_modality() -> None:
    scheduler = ActionScheduler(FrontierPolicy(max_actions_per_batch=1))
    direction = Obligation(
        obligation_id="direction",
        title="Approve direction",
        requirement="Direction must be established first",
        kind="decision",
        acceptance="Representative slice passes",
        impact=Impact.FATAL,
        status=ObligationStatus.OPEN,
    )
    film = Obligation(
        obligation_id="film",
        title="Build film",
        requirement="Build the full film",
        kind="construction",
        acceptance="Rendered film passes",
        impact=Impact.FATAL,
        depends_on=["direction"],
        required_evidence_modalities=[EvidenceModality.TEMPORAL_VISUAL],
    )
    premature = action("premature").model_copy(update={"obligation_ids": ["film"]})
    slice_review = action("slice").model_copy(
        update={
            "obligation_ids": ["direction"],
            "observation_modalities": [EvidenceModality.TEMPORAL_VISUAL],
            "optimization_value": ValueBand.HIGH,
        }
    )
    result = scheduler.select(
        [premature, slice_review],
        max_parallel=1,
        available_calls=1,
        obligations={"direction": direction, "film": film},
    )
    assert [item.action_id for item in result.selected] == ["slice"]
    assert "unsatisfied obligation dependencies" in result.deferred["premature"]


def test_default_model_ladder_is_sol_at_every_substantive_effort() -> None:
    provider = ProviderConfig()
    assert provider.default_model == "gpt-5.6-sol"
    assert (provider.strong.model, provider.strong.reasoning_effort) == (
        "gpt-5.6-sol",
        "xhigh",
    )
    assert (provider.worker.model, provider.worker.reasoning_effort) == (
        "gpt-5.6-sol",
        "high",
    )
    assert (provider.cheap.model, provider.cheap.reasoning_effort) == (
        "gpt-5.6-sol",
        "medium",
    )


def test_action_impact_selects_the_sol_effort_ladder() -> None:
    engine = object.__new__(FrontierEngine)
    assert engine._role_for_action(action("fatal", impact=Impact.FATAL)) == Role.STRONG
    assert engine._role_for_action(action("high", impact=Impact.HIGH)) == Role.WORKER
    assert engine._role_for_action(action("medium", impact=Impact.MEDIUM)) == Role.CHEAP
    assert (
        engine._role_for_action(
            action("expensive-high", impact=Impact.HIGH, cost=CostBand.EXPENSIVE)
        )
        == Role.STRONG
    )
