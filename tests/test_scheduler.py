from __future__ import annotations

from pathlib import Path

from frontier_harness.adapters.base import CallWorkspace
from frontier_harness.config import FrontierPolicy, HarnessConfig, ProviderConfig
from frontier_harness.engine import FrontierEngine
from frontier_harness.models import (
    ActionKind,
    ActionReceipt,
    ActionRecord,
    ActionSpec,
    ActionStatus,
    CognitiveTopology,
    ContinuationContract,
    CostBand,
    EliminatedDirection,
    EpistemicMode,
    EvidenceModality,
    FrontierKernel,
    Impact,
    IndependenceClass,
    Obligation,
    ObligationStatus,
    Role,
    RunState,
    ValueBand,
)
from frontier_harness.prompts import worker_prompt
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
    epistemic_mode: EpistemicMode = EpistemicMode.AUTO,
    hypothesis_family: str = "",
    novelty_basis: str = "",
    execution_trigger: str = "",
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
        epistemic_mode=epistemic_mode,
        hypothesis_family=hypothesis_family,
        novelty_basis=novelty_basis,
        execution_trigger=execution_trigger,
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


def test_epistemic_mode_never_gates_tools_unless_strict_mode_is_explicit() -> None:
    scheduler = ActionScheduler(FrontierPolicy(max_actions_per_batch=2))
    premature = action(
        "premature",
        epistemic_mode=EpistemicMode.EXECUTE,
        assignment="run a broad experiment",
    )
    permissive = scheduler.select([premature], max_parallel=1, available_calls=1)
    strict = scheduler.select(
        [premature],
        max_parallel=1,
        available_calls=1,
        require_execution_trigger=True,
    )

    assert [item.action_id for item in permissive.selected] == ["premature"]
    assert "residual uncertainty" in strict.dominated["premature"]


def test_scheduler_chooses_question_value_before_convenient_cost() -> None:
    scheduler = ActionScheduler(FrontierPolicy(max_actions_per_batch=1))
    convenient = action(
        "cheap",
        target="minor cleanup",
        impact=Impact.MEDIUM,
        cost=CostBand.CHEAP,
    )
    decisive = action(
        "decisive",
        target="controlling uncertainty",
        impact=Impact.FATAL,
        cost=CostBand.EXPENSIVE,
    ).model_copy(
        update={
            "information_value": ValueBand.HIGH,
            "outcome_branches": [
                {
                    "outcome": "mechanism A",
                    "decision_effect": "adopt A",
                },
                {
                    "outcome": "mechanism B",
                    "decision_effect": "adopt B",
                },
            ],
            "causal_hypothesis": "One of two mechanisms controls the fatal boundary.",
            "intervention": "Expose their conflicting prediction.",
            "potency_check": "Confirm the conflicting condition was reached.",
            "decision_rule": "Adopt the mechanism matching the observation.",
        }
    )

    result = scheduler.select([convenient, decisive], max_parallel=1, available_calls=1)

    assert [item.action_id for item in result.selected] == ["decisive"]


def test_continuation_requires_integrated_causal_movement() -> None:
    scheduler = ActionScheduler(FrontierPolicy(max_actions_per_batch=1))
    thesis = "A two-step reconstruction unlocks the controlling representation."
    terminal = "The reconstructed representation passes the task-native discriminator."
    first_contract = ContinuationContract(
        key="representation-rebuild",
        thesis=thesis,
        terminal_observation=terminal,
        continuation_evidence="The first invariant survives and changes the frontier.",
        kill_condition="The first invariant contradicts the Task Source.",
        step=1,
        max_steps=2,
    )
    first = action("first").model_copy(update={"continuation": first_contract})
    prior = ActionRecord(
        spec=first,
        status=ActionStatus.COMPLETE,
        receipt=ActionReceipt(
            action_id="first",
            observed_result="The representation exposes a new invariant.",
            integration_status="accepted",
        ),
    )
    second = action("second").model_copy(
        update={
            "continuation": first_contract.model_copy(update={"step": 2}),
        }
    )
    records = {
        "first": prior,
        "second": ActionRecord(spec=second),
    }

    blocked = scheduler.select(
        [second],
        max_parallel=1,
        available_calls=1,
        action_records=records,
    )
    admitted = scheduler.select(
        [second],
        max_parallel=1,
        available_calls=1,
        action_records=records,
        frontier_advancing_action_ids={"first"},
    )

    assert "no confirmed observation" in blocked.deferred["second"]
    assert [item.action_id for item in admitted.selected] == ["second"]


def test_new_auto_actions_are_compiled_into_an_explicit_epistemic_medium() -> None:
    engine = object.__new__(FrontierEngine)
    engine.config = HarnessConfig()
    conceptual = action(
        "conceptual",
        kind=ActionKind.EXPLORE,
        independence=IndependenceClass.SAME_MODEL,
    )
    observed = action(
        "observed",
        kind=ActionKind.DISCRIMINATE,
        independence=IndependenceClass.DETERMINISTIC_TOOL,
    )
    summit = conceptual.model_copy(
        update={"action_id": "summit", "topology": CognitiveTopology.SUMMIT}
    )

    thought = engine._compile_epistemic_action(conceptual)
    execution = engine._compile_epistemic_action(observed)
    widened_thought = engine._compile_epistemic_action(summit)

    assert (thought.epistemic_mode, thought.topology) == (
        EpistemicMode.THINK,
        CognitiveTopology.LEAD,
    )
    assert execution.epistemic_mode == EpistemicMode.EXECUTE
    assert (widened_thought.epistemic_mode, widened_thought.topology) == (
        EpistemicMode.THINK,
        CognitiveTopology.SUMMIT,
    )


def test_think_label_keeps_the_full_tool_plane(tmp_path: Path) -> None:
    workspace = CallWorkspace(
        call_id="call-tools",
        call_kind="worker",
        root=tmp_path,
        cwd=tmp_path,
        context_dir=tmp_path / "context",
        output_dir=tmp_path / "output",
        expected_artifact_path=tmp_path / "artifact.md",
    )
    prompt = worker_prompt(
        workspace,
        action=action(
            "thought",
            epistemic_mode=EpistemicMode.THINK,
            independence=IndependenceClass.SAME_MODEL,
        ),
        profile=None,
        software=False,
    )

    assert "A `think` action keeps all tools" not in prompt  # bootstrap-only wording
    assert "within any epistemic mode" in prompt
    assert "Do not run code" not in prompt


def test_eliminated_family_needs_reopening_evidence_before_more_spend() -> None:
    scheduler = ActionScheduler(FrontierPolicy(max_actions_per_batch=2))
    kernel = FrontierKernel(
        eliminated_directions=[
            EliminatedDirection(
                family="brute force parameter sweep",
                failure_mechanism="The proxy rewards a shortcut that fails held-out cases.",
                reopen_if="A task-equivalent evaluator removes the proxy mismatch.",
            )
        ]
    )
    repeated = action(
        "repeat",
        hypothesis_family="parameter brute force sweep",
    )
    reopened = action(
        "reopened",
        target="other claim",
        hypothesis_family="brute force parameter sweep",
        novelty_basis="A new held-out evaluator removes the prior proxy mismatch.",
    )

    result = scheduler.select(
        [repeated, reopened],
        max_parallel=2,
        available_calls=2,
        frontier_kernel=kernel,
    )

    assert [item.action_id for item in result.selected] == ["reopened"]
    assert "eliminated hypothesis family" in result.deferred["repeat"]


def test_frame_pressure_appears_only_after_runtime_observed_samsara() -> None:
    engine = object.__new__(FrontierEngine)
    engine.config = HarnessConfig()
    engine.state = RunState(
        run_id="run-frame-pressure",
        created_at="2026-08-25T00:00:00Z",
        frontier_kernel=FrontierKernel(stagnant_rounds=2),
    )
    obligation = Obligation(
        obligation_id="blocking",
        title="Controlling result",
        requirement="The controlling result must work.",
        kind="construction",
        acceptance="Task-native evidence passes.",
        impact=Impact.HIGH,
    )
    proposals = []

    engine._ensure_frame_pressure(
        proposals,
        obligations={"blocking": obligation},
        cruxes={},
    )

    assert len(proposals) == 1
    assert proposals[0].kind == ActionKind.CEILING_AUDIT
    assert proposals[0].topology == CognitiveTopology.WORKER
    assert "process report" in proposals[0].assignment


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
