from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from frontier_harness.config import HarnessConfig, ResourcePolicy
from frontier_harness.engine import FrontierEngine
from frontier_harness.models import (
    ActionKind,
    ActionRecord,
    ActionSpec,
    ActionStatus,
    ArtifactRef,
    BlobRef,
    BudgetContract,
    CostBand,
    EvidenceModality,
    EvidenceRecord,
    FrontierKernel,
    Impact,
    IndependenceClass,
    Obligation,
    ObligationStatus,
    ResourceDecisionKind,
    RunState,
    WorkerEnvelope,
)
from frontier_harness.resources import ResourceGovernor


def state() -> RunState:
    return RunState(run_id="run-resource", created_at="2026-08-25T00:00:00Z")


def governor(
    *,
    hard: int = 24,
    max_stagnant_grants: int = 2,
    release_expected: bool = False,
) -> ResourceGovernor:
    return ResourceGovernor(
        policy=ResourcePolicy(max_stagnant_grants=max_stagnant_grants),
        budget=BudgetContract(max_calls=hard, max_parallel=3),
        release_expected=release_expected,
        max_material_repairs=3,
    )


def add_evidence(run: RunState, key: str) -> None:
    run.evidence[key] = EvidenceRecord(
        evidence_id=key,
        kind="test",
        summary="A discriminative observation",
        scope="current candidate",
        independence_class=IndependenceClass.DETERMINISTIC_TOOL,
        modalities=[EvidenceModality.DETERMINISTIC_TEST],
        establishes=["the tested branch is viable"],
    )


def artifact(digest: str, version: int) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"artifact-{version}",
        version=version,
        blob=BlobRef(
            digest=digest,
            size=1,
            relative_path=f"blobs/{digest}",
        ),
        created_at="2026-08-25T00:00:00Z",
    )


def test_initial_grant_is_derived_but_hard_envelope_remains_operator_owned() -> None:
    run = state()
    allocator = governor(hard=24)

    resource = allocator.initial_state(run)

    assert resource.active_call_limit == 6  # orient + batch + checkpoint + synthesis
    assert resource.hard_call_limit == 24


def test_artifact_activity_alone_cannot_earn_compute() -> None:
    run = state()
    allocator = governor(max_stagnant_grants=0)
    resource = allocator.initial_state(run)
    run.current_artifact = None

    # Artifact mutation is recorded but is not itself a material signal.
    before = resource.last_snapshot.model_copy(update={"artifact_digest": "a" * 64})
    resource = resource.model_copy(update={"last_snapshot": before})
    after = allocator.snapshot(run).model_copy(update={"artifact_digest": "b" * 64})
    vector, reasons = allocator._progress(before, after)

    assert vector.productive is False
    assert reasons == ["authoritative artifact changed"]


def test_artifact_churn_does_not_reset_material_debt_grace() -> None:
    run = state()
    run.obligations["obl-1"] = Obligation(
        obligation_id="obl-1",
        title="Required result",
        requirement="Resolve the load-bearing requirement",
        kind="constraint",
        acceptance="The requirement is evidenced",
        impact=Impact.HIGH,
    )
    run.current_artifact = artifact("a" * 64, 1)
    allocator = governor(max_stagnant_grants=1)
    resource = allocator.initial_state(run)

    run.current_artifact = artifact("b" * 64, 2)
    first, resource = allocator.decide(run, resource, actionable_actions=1)
    run.current_artifact = artifact("c" * 64, 3)
    second, _ = allocator.decide(run, resource, actionable_actions=1)

    assert first.kind == ResourceDecisionKind.GRANT
    assert first.gradient_score == 0
    assert second.kind == ResourceDecisionKind.FINALIZE


def test_discriminative_evidence_earns_another_horizon() -> None:
    run = state()
    allocator = governor()
    resource = allocator.initial_state(run)
    add_evidence(run, "ev-1")

    decision, updated = allocator.decide(run, resource, actionable_actions=2)

    assert decision.kind == ResourceDecisionKind.GRANT
    assert decision.gradient_score > 0
    assert updated.active_call_limit > resource.active_call_limit


def test_model_claimed_materiality_does_not_mint_compute() -> None:
    run = state()
    run.actions["act-claim"] = ActionRecord(
        spec=ActionSpec(
            action_id="act-claim",
            round_index=1,
            kind=ActionKind.EXPLORE,
            target="unverified idea",
            assignment="declare this important",
            impact=Impact.HIGH,
            cost=CostBand.CHEAP,
            expected_decision_effect="change the answer",
        ),
        status=ActionStatus.COMPLETE,
        result=WorkerEnvelope(
            target="unverified idea",
            result_or_artifact_reference="output/claim.md",
            findings=["The model says this matters."],
            materiality="fatal",
            negative_result=True,
        ),
    )

    assert ResourceGovernor.snapshot(run).informative_actions == 0


def test_keeper_owned_frontier_advance_can_earn_the_next_thought_horizon() -> None:
    run = state()
    allocator = governor()
    before = allocator.snapshot(run)
    run.frontier_kernel = FrontierKernel(
        bottleneck="A new causal incompatibility controls the result.",
        revision=1,
        last_advance_round=1,
    )
    after = allocator.snapshot(run)

    vector, reasons = allocator._progress(before, after)

    assert vector.epistemic == 1
    assert reasons == ["frontier understanding advanced by 1 revision(s)"]


def test_valid_delayed_commitment_can_earn_a_horizon_without_fake_progress() -> None:
    run = state()
    allocator = governor(max_stagnant_grants=0)
    resource = allocator.initial_state(run)

    decision, _ = allocator.decide(
        run,
        resource,
        actionable_actions=1,
        active_commitments=1,
    )

    assert decision.kind == ResourceDecisionKind.GRANT
    assert decision.gradient_score == 0
    assert decision.active_commitments == 1
    assert "bounded continuation" in decision.reasons[0]


def test_unresolved_material_debt_gets_bounded_grace_not_infinite_churn() -> None:
    run = state()
    run.obligations["obl-1"] = Obligation(
        obligation_id="obl-1",
        title="Required result",
        requirement="Resolve the load-bearing requirement",
        kind="constraint",
        acceptance="The requirement is evidenced",
        impact=Impact.HIGH,
    )
    allocator = governor(max_stagnant_grants=1)
    resource = allocator.initial_state(run)

    first, resource = allocator.decide(run, resource, actionable_actions=1)
    second, _ = allocator.decide(run, resource, actionable_actions=1)

    assert first.kind == ResourceDecisionKind.GRANT
    assert second.kind == ResourceDecisionKind.FINALIZE


def test_bookkeeping_only_debt_closure_does_not_mint_compute() -> None:
    run = state()
    run.obligations["obl-1"] = Obligation(
        obligation_id="obl-1",
        title="Required result",
        requirement="Resolve the load-bearing requirement",
        kind="constraint",
        acceptance="The requirement is evidenced",
        impact=Impact.HIGH,
    )
    allocator = governor(max_stagnant_grants=0)
    resource = allocator.initial_state(run)
    run.obligations["obl-1"].status = ObligationStatus.SATISFIED

    decision, _ = allocator.decide(run, resource, actionable_actions=1)

    assert decision.kind == ResourceDecisionKind.FINALIZE
    assert decision.progress_vector.feasibility == 0
    assert "did not mint compute" in decision.progress_reasons[-1]


def test_live_gradient_at_hard_envelope_recommends_operator_extension() -> None:
    run = state()
    allocator = governor(hard=8)
    resource = allocator.initial_state(run).model_copy(update={"active_call_limit": 8})
    add_evidence(run, "ev-1")

    decision, _ = allocator.decide(run, resource, actionable_actions=1)

    assert decision.kind == ResourceDecisionKind.EXTENSION_REQUIRED
    assert decision.extension_recommended is True


def test_completion_reserve_tracks_real_release_risk() -> None:
    run = state()
    allocator = governor(release_expected=True)

    assert allocator.completion_reserve(run) == 4
    run.metadata["repair_count"] = 3
    assert allocator.completion_reserve(run) == 2


def test_legacy_reserve_does_not_constrain_an_adaptive_hard_envelope() -> None:
    adaptive = HarnessConfig.model_validate(
        {
            "run": {"budget": {"max_calls": 3, "synthesis_reserve_calls": 4}},
            "resource": {"mode": "adaptive"},
        }
    )
    assert adaptive.run.budget.max_calls == 3

    with pytest.raises(ValueError, match="static call budget"):
        HarnessConfig.model_validate(
            {
                "run": {"budget": {"max_calls": 3, "synthesis_reserve_calls": 4}},
                "resource": {"mode": "static"},
            }
        )


def test_resource_horizon_is_replayed_from_the_authoritative_ledger(
    tmp_path: Path, fake_config
) -> None:
    config = fake_config(
        resource={"mode": "adaptive"},
        run={
            "budget": {
                "max_rounds": None,
                "max_calls": 24,
                "max_parallel": 3,
                "synthesis_reserve_calls": 4,
            }
        },
    )
    engine = FrontierEngine.create("Persist the resource decision.", config=config)
    run_dir = engine.run_dir
    try:
        engine._ensure_resource_state()
        expected = engine.state.resource_state
        assert expected is not None
    finally:
        engine.close()

    resumed = FrontierEngine.load(run_dir)
    try:
        assert resumed.state.resource_state == expected
        assert resumed.state.resource_state.mode == "adaptive"
    finally:
        resumed.close()


def test_adaptive_run_finishes_far_below_its_hard_envelope(
    fake_config,
) -> None:
    engine = FrontierEngine.create(
        "Finish when the decision frontier is actually resolved.",
        config=fake_config(
            resource={"mode": "adaptive"},
            run={
                "budget": {
                    "max_rounds": None,
                    "max_calls": 24,
                    "max_parallel": 3,
                    "synthesis_reserve_calls": 4,
                }
            },
        ),
    )
    try:
        asyncio.run(engine.execute())

        assert engine.state.phase.value == "complete"
        assert engine.state.usage.calls == 5
        assert engine.state.resource_state is not None
        assert engine.state.resource_state.active_call_limit < 24
    finally:
        engine.close()
