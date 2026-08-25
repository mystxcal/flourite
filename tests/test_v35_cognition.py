from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from frontier_harness import events as et
from frontier_harness.cognition import (
    admit_overlays,
    admit_substrate_entries,
    apply_obligation_updates,
    capture_task_source,
    charter_change_requires_witness,
    compile_guard_obligations,
    fallback_charter,
    finalize_action_receipt,
    observed_modalities_from_trace,
    reactivate_cruxes_for_open_obligations,
    reconcile_frontier_kernel,
)
from frontier_harness.engine import FrontierEngine
from frontier_harness.errors import ProviderCallError
from frontier_harness.models import (
    ActionContract,
    ActionKind,
    ActionOutcome,
    ActionProposal,
    ActionReceipt,
    ActionRecord,
    ActionSpec,
    ArtifactRef,
    ArtifactSpine,
    BlobRef,
    CharterAssertion,
    CharterProvenance,
    CheckpointOutput,
    CompletionCase,
    CompletionClaim,
    CostBand,
    Crux,
    CruxStatus,
    EliminatedDirection,
    EvidenceModality,
    EvidenceRecord,
    FrontierKernel,
    GoalContract,
    Impact,
    IndependenceClass,
    InvariantRevision,
    Obligation,
    ObligationDraft,
    ObligationStatus,
    ObligationUpdate,
    OverlayStatus,
    SemanticRegressionFinding,
    SpeculativeOverlay,
    SubstrateEntry,
    TaskCharter,
    UnlockContract,
    Usage,
    WorkerEnvelope,
)
from frontier_harness.providers.base import ProviderTraceSummary, ToolCallSummary
from frontier_harness.providers.fake import FakeProvider
from frontier_harness.util import utc_now


class ResumeFailsOnce(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.failed_resume = False

    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.resume_thread_id and not self.failed_resume:
            self.failed_resume = True
            raise ProviderCallError(
                "synthetic Lead resume failure",
                usage=Usage(calls=1, input_tokens=3, output_tokens=1),
            )
        return await super().run(request)


class ResumeAndReconstructionFail(FakeProvider):
    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.response_model is CheckpointOutput:
            stage = "resume" if request.resume_thread_id else "reconstruction"
            raise ProviderCallError(
                f"synthetic {stage} failure",
                usage=Usage(calls=1, model_requests=2, input_tokens=7),
                boundary_attempts=1,
            )
        return await super().run(request)


def _charter(*, deliverable: str = "Deliver the report", audience: str = "Operator") -> TaskCharter:
    source = capture_task_source("Deliver the report without changing its objective.")
    return TaskCharter(
        source_digest=source.digest,
        deliverable=deliverable,
        real_world_purpose="Support the original decision.",
        audience=audience,
        assertions=[
            CharterAssertion(
                key="deliverable",
                statement=deliverable,
                provenance=CharterProvenance.EXPLICIT,
            )
        ],
        hard_constraints=["Do not replace the user's objective"],
        unacceptable_failures=["Answering a different task"],
    )


def test_frontier_kernel_does_not_reward_paraphrase_churn() -> None:
    current = FrontierKernel(
        bottleneck="The evaluator rewards a proxy rather than the actual objective.",
        invariants=["The immutable task remains the optimization target."],
        live_hypotheses=["The proxy mismatch causes the observed failure."],
        next_move="Derive a task-equivalent discriminator.",
        revision=4,
        last_advance_round=2,
    )
    paraphrase = FrontierKernel(
        bottleneck="The evaluator rewards a proxy, not the real objective.",
        invariants=["The optimization target stays the immutable task."],
        live_hypotheses=["Observed failure is caused by mismatch in the proxy."],
        next_move="Construct an equivalent task discriminator.",
    )

    updated, notes = reconcile_frontier_kernel(
        current,
        paraphrase,
        cruxes=[],
        spine=None,
        next_actions=[],
        round_index=3,
    )

    assert not notes.advanced
    assert updated.revision == 4
    assert updated.stagnant_rounds == 1


def test_frontier_kernel_retains_causal_negative_results_without_fake_revisions() -> None:
    current = FrontierKernel(
        bottleneck="Choose the mechanism that generalizes.",
        invariants=["The task objective is immutable."],
        live_hypotheses=["A causal planner generalizes."],
        revision=2,
        last_advance_round=1,
    )
    proposed = current.model_copy(
        update={
            "eliminated_directions": [
                EliminatedDirection(
                    family="proxy-only hill climbing",
                    failure_mechanism="It exploits evaluator leakage and fails held-out cases.",
                    reopen_if="A task-equivalent evaluator removes the leakage.",
                ),
                EliminatedDirection(
                    family="malformed family",
                    failure_mechanism="",
                ),
            ],
            "source_action_ids": ["act-negative", "act-invented"],
        }
    )

    learned, notes = reconcile_frontier_kernel(
        current,
        proposed,
        cruxes=[],
        spine=None,
        next_actions=[],
        round_index=2,
        eligible_action_ids=["act-negative"],
    )
    repeated, repeat_notes = reconcile_frontier_kernel(
        learned,
        proposed,
        cruxes=[],
        spine=None,
        next_actions=[],
        round_index=3,
        eligible_action_ids=["act-negative"],
    )

    assert notes.advanced
    assert learned.revision == 3
    assert len(learned.eliminated_directions) == 1
    assert learned.source_action_ids == ["act-negative"]
    assert notes.rejected == [
        "eliminated direction lacked a family or failure mechanism",
        "frontier sources were not completed in this checkpoint: act-invented",
    ]
    assert not repeat_notes.advanced
    assert repeated.revision == 3


def test_frontier_kernel_cannot_claim_batch_progress_without_attribution() -> None:
    current = FrontierKernel(
        bottleneck="The current mechanism does not explain the residual.",
        live_hypotheses=["The representation hides the controlling variable."],
        revision=3,
    )
    unattributed = FrontierKernel(
        bottleneck="A different mechanism now controls the residual.",
        live_hypotheses=["The scheduler causes the failure."],
    )

    updated, notes = reconcile_frontier_kernel(
        current,
        unattributed,
        cruxes=[],
        spine=None,
        next_actions=[],
        round_index=4,
        eligible_action_ids=["act-completed"],
    )

    assert not notes.advanced
    assert updated.bottleneck == current.bottleneck
    assert updated.revision == 3
    assert updated.stagnant_rounds == 1
    assert notes.rejected[-1].startswith("semantic frontier update lacked")


def test_frontier_kernel_revises_false_working_invariants_but_not_hard_ones() -> None:
    from frontier_harness.models import ArtifactSpine

    current = FrontierKernel(
        bottleneck="Explain the held-out failure.",
        invariants=[
            "The user's objective remains fixed.",
            "The proxy score tracks real performance.",
        ],
        revision=2,
    )
    proposed = current.model_copy(
        update={
            "invariant_revisions": [
                InvariantRevision(
                    statement="The proxy score tracks actual performance.",
                    failure_mechanism="Held-out outcomes diverge from the proxy.",
                    replacement="The proxy is valid only on the calibration slice.",
                ),
                InvariantRevision(
                    statement="The user objective stays fixed.",
                    failure_mechanism="A simpler objective is easier to optimize.",
                ),
            ],
            "source_action_ids": ["act-held-out"],
        }
    )

    updated, notes = reconcile_frontier_kernel(
        current,
        proposed,
        cruxes=[],
        spine=ArtifactSpine(
            central_thesis="Solve the original task.",
            hard_invariants=["The user's objective remains fixed."],
        ),
        next_actions=[],
        round_index=3,
        eligible_action_ids=["act-held-out"],
    )

    assert notes.advanced
    assert updated.revision == 3
    assert updated.invariants == [
        "The user's objective remains fixed.",
        "The proxy is valid only on the calibration slice.",
    ]
    assert len(updated.invariant_revisions) == 1
    assert any("cannot retire" in item for item in notes.rejected)


def test_eliminated_hypothesis_leaves_live_frontier_until_evidence_reopens_it() -> None:
    current = FrontierKernel(
        bottleneck="Choose a search mechanism.",
        live_hypotheses=["proxy-only hill climbing"],
        revision=1,
    )
    eliminated = FrontierKernel(
        bottleneck=current.bottleneck,
        live_hypotheses=[],
        eliminated_directions=[
            EliminatedDirection(
                family="proxy-only hill climbing",
                failure_mechanism="It fails held-out objectives.",
                reopen_if="A task-equivalent evaluator becomes available.",
            )
        ],
        source_action_ids=["act-falsify"],
    )
    closed, _ = reconcile_frontier_kernel(
        current,
        eliminated,
        cruxes=[],
        spine=None,
        next_actions=[],
        round_index=2,
        eligible_action_ids=["act-falsify"],
    )
    reopen = ActionProposal(
        kind=ActionKind.EXPLORE,
        target="search mechanism",
        assignment="Reconsider the family under the new evaluator.",
        impact=Impact.HIGH,
        cost=CostBand.CHEAP,
        expected_decision_effect="Retain or kill the family under the real objective.",
        hypothesis_family="proxy-only hill climbing",
        novelty_basis="A task-equivalent held-out evaluator is now available.",
    )
    reopened, _ = reconcile_frontier_kernel(
        closed,
        closed.model_copy(
            update={
                "live_hypotheses": ["proxy-only hill climbing"],
                "source_action_ids": ["act-reopen"],
            }
        ),
        cruxes=[],
        spine=None,
        next_actions=[reopen],
        round_index=3,
        eligible_action_ids=["act-reopen"],
    )

    assert closed.live_hypotheses == []
    assert reopened.live_hypotheses == ["proxy-only hill climbing"]


def test_only_destination_changes_require_reframe_witness() -> None:
    current = _charter()
    clarification = current.model_copy(update={"audience": "Operator and reviewer", "revision": 2})
    changed_destination = current.model_copy(
        update={"deliverable": "Deliver a benchmark instead", "revision": 2}
    )
    assert not charter_change_requires_witness(current, clarification)
    assert charter_change_requires_witness(current, changed_destination)


def test_action_receipt_uses_observed_tools_cost_and_validated_forecast() -> None:
    contract = ActionContract(
        action_id="act_1",
        question="Does the executable return 42?",
        possible_outcomes=[
            ActionOutcome(outcome="It returns 42", decision_effect="accept"),
            ActionOutcome(outcome="It does not", decision_effect="repair"),
        ],
        evidence_channel=IndependenceClass.DETERMINISTIC_TOOL,
        expected_cost=CostBand.CHEAP,
        stop_condition="Stop after execution.",
    )
    claimed = ActionReceipt(
        action_id="act_1",
        observed_result="It returned 42.",
        evidence_strength="decisive",
        matched_outcome_index=0,
        outcome_match="matched",
    )
    trace = ProviderTraceSummary(
        model_turns=3,
        tool_calls=[
            ToolCallSummary(
                call_id="tool_1",
                name="bash",
                success=True,
                duration_ms=12,
            )
        ],
    )
    finalized = finalize_action_receipt(
        claimed,
        contract=contract,
        trace=trace,
        usage=Usage(
            calls=1,
            model_requests=3,
            input_tokens=90,
            output_tokens=12,
            wall_seconds=1.25,
        ),
    )
    assert finalized.outcome_match == "matched"
    assert finalized.forecast_was_useful
    assert finalized.evidence_channel_confirmed
    assert finalized.observed_evidence_channels == [IndependenceClass.DETERMINISTIC_TOOL]
    assert finalized.evidence_strength == "strong"
    assert finalized.observed_cost.model_turns == 3
    assert finalized.observed_cost.provider_calls == 1
    assert finalized.observed_cost.tool_calls == 1
    assert finalized.observed_cost.input_tokens == 90


def test_action_receipt_rejects_impossible_branch_and_self_certified_channel() -> None:
    contract = ActionContract(
        action_id="act_2",
        question="Check the source.",
        possible_outcomes=[ActionOutcome(outcome="supported", decision_effect="accept")],
        evidence_channel=IndependenceClass.EXTERNAL_EVIDENCE,
        expected_cost=CostBand.CHEAP,
        stop_condition="Stop after one source.",
    )
    finalized = finalize_action_receipt(
        ActionReceipt(
            action_id="act_2",
            observed_result="Trust me.",
            evidence_strength="decisive",
            matched_outcome_index=9,
            observed_evidence_channels=[IndependenceClass.EXTERNAL_EVIDENCE],
            evidence_channel_confirmed=True,
        ),
        contract=contract,
        trace=ProviderTraceSummary(model_turns=1),
        usage=Usage(calls=1),
    )
    assert finalized.matched_outcome_index is None
    assert finalized.outcome_match == "invalid"
    assert not finalized.forecast_was_useful
    assert finalized.observed_evidence_channels == [IndependenceClass.SAME_MODEL]
    assert not finalized.evidence_channel_confirmed
    assert finalized.evidence_strength == "moderate"


def test_context_reads_and_unfinished_tools_do_not_self_certify_evidence() -> None:
    finalized = finalize_action_receipt(
        ActionReceipt(
            action_id="act_context",
            observed_result="I read the capsule.",
            evidence_strength="decisive",
        ),
        contract=None,
        trace=ProviderTraceSummary(
            model_turns=2,
            tool_calls=[
                ToolCallSummary(call_id="read-1", name="read", success=True),
                ToolCallSummary(call_id="bash-pending", name="bash", success=None),
            ],
        ),
        usage=Usage(calls=1, model_requests=2),
    )

    assert finalized.observed_evidence_channels == [IndependenceClass.SAME_MODEL]
    assert finalized.evidence_strength == "moderate"
    assert not finalized.evidence_channel_confirmed


def test_declared_observation_modality_requires_a_matching_successful_tool() -> None:
    requested = [
        EvidenceModality.DETERMINISTIC_TEST,
        EvidenceModality.STATIC_VISUAL,
        EvidenceModality.HUMAN_OBSERVATION,
    ]
    observed = observed_modalities_from_trace(
        requested,
        ProviderTraceSummary(
            tool_calls=[
                ToolCallSummary(call_id="bash-ok", name="bash", success=True),
                ToolCallSummary(call_id="image-failed", name="inspect_image", success=False),
            ]
        ),
    )

    assert observed == [EvidenceModality.DETERMINISTIC_TEST]


def test_worker_envelope_replays_legacy_runtime_enriched_receipt() -> None:
    schema_fields = WorkerEnvelope.model_json_schema()["$defs"]["ActionObservation"]["properties"]
    assert "observed_cost" not in schema_fields
    assert "integration_status" not in schema_fields
    assert "evidence_channel_confirmed" not in schema_fields

    envelope = WorkerEnvelope.model_validate(
        {
            "target": "legacy action",
            "result_or_artifact_reference": "artifact.txt",
            "findings": ["done"],
            "action_receipt": {
                "action_id": "act_legacy",
                "observed_result": "done",
                "evidence_strength": "strong",
                "observed_evidence_channels": ["deterministic_tool"],
                "evidence_channel_confirmed": True,
                "observed_cost": {"tool_calls": 1},
                "integration_status": "accepted",
                "forecast_was_useful": True,
            },
        }
    )

    assert envelope.action_receipt is not None
    assert envelope.action_receipt.action_id == "act_legacy"
    assert envelope.action_receipt.evidence_strength == "strong"
    assert isinstance(envelope.action_receipt, ActionReceipt)
    assert envelope.action_receipt.integration_status == "accepted"


def test_invalidating_dependency_reopens_obligation_and_controlling_crux() -> None:
    root = Obligation(
        obligation_id="obl_root",
        title="Root premise",
        requirement="Premise must hold",
        kind="claim",
        acceptance="Direct evidence",
        impact=Impact.HIGH,
        status=ObligationStatus.SATISFIED,
    )
    dependent = Obligation(
        obligation_id="obl_dependent",
        title="Dependent result",
        requirement="Result follows",
        kind="claim",
        acceptance="Valid derivation",
        impact=Impact.HIGH,
        status=ObligationStatus.SATISFIED,
        depends_on=[root.obligation_id],
    )
    updated, notes = apply_obligation_updates(
        {root.obligation_id: root, dependent.obligation_id: dependent},
        [
            ObligationUpdate(
                obligation_id=root.obligation_id,
                status=ObligationStatus.INVALIDATED,
                invalidate_dependents=True,
            )
        ],
        updated_seq=8,
    )
    assert updated[dependent.obligation_id].status == ObligationStatus.OPEN
    assert any("reopened dependent obligation" in item for item in notes)

    crux = Crux(
        crux_id="crx_result",
        title="Can the dependent result survive?",
        uncertainty="Previously resolved under the invalidated premise.",
        decision_controlled="Whether to retain the result",
        why_it_matters="It controls the final conclusion.",
        obligation_ids=[dependent.obligation_id],
        unlock_value=Impact.HIGH,
        status=CruxStatus.RESOLVED,
        resolution="Previously accepted.",
    )
    recompiled, crux_notes = reactivate_cruxes_for_open_obligations(
        {crux.crux_id: crux},
        updated,
        updated_seq=9,
        active_limit=1,
    )
    assert recompiled[crux.crux_id].status == CruxStatus.ACTIVE
    assert recompiled[crux.crux_id].resolution is None
    assert crux_notes


def test_obligation_update_can_raise_required_artifact_scope() -> None:
    obligation = Obligation(
        obligation_id="obl_quality",
        title="Release quality",
        requirement="The complete artifact must meet the quality bar.",
        kind="coherence",
        acceptance="Cold review of the complete artifact",
        impact=Impact.HIGH,
        required_artifact_scope="targeted",
    )

    updated, notes = apply_obligation_updates(
        {obligation.obligation_id: obligation},
        [
            ObligationUpdate(
                obligation_id=obligation.obligation_id,
                required_artifact_scope="whole_artifact",
            )
        ],
        updated_seq=3,
    )

    assert notes == []
    assert updated[obligation.obligation_id].required_artifact_scope == "whole_artifact"
    assert updated[obligation.obligation_id].updated_seq == 3


def test_shared_substrate_requires_provenance_for_global_admission() -> None:
    entry = SubstrateEntry(
        entry_id="sub_ungrounded",
        kind="fact",
        statement="A branch-local assertion",
        scope="Only the speculative branch",
        global_admission=True,
        confidence="high",
    )
    projected, notes = admit_substrate_entries([entry], existing={})
    assert projected[entry.entry_id].global_admission is False
    assert notes.warnings


def test_overlay_limits_preserve_bounded_stepping_stone_without_population_sprawl() -> None:
    ordinary = SpeculativeOverlay(
        overlay_id="ovr_a",
        name="Mechanism A",
        mechanism="Use mechanism A",
        candidate_change="Replace the local mechanism",
        behavioral_difference="Predicts a lower failure rate on boundary case A.",
    )
    protected = SpeculativeOverlay(
        overlay_id="ovr_b",
        name="Mechanism B",
        mechanism="Use mechanism B",
        candidate_change="Introduce a different mechanism",
        behavioral_difference="Changes the chosen action under boundary case B.",
        unlock_contract=UnlockContract(
            potential_unlock="Resolve the central incompatibility",
            blocking_dependency="A missing discriminator",
            next_probe="Run the boundary discriminator",
            continuation_evidence="A distinct prediction survives",
            kill_condition="The prediction is falsified",
        ),
    )
    excess = SpeculativeOverlay(
        overlay_id="ovr_c",
        name="Mechanism C",
        mechanism="Use mechanism C",
        candidate_change="Add another mechanism",
        behavioral_difference="Would choose a third implementation.",
    )
    projected, notes = admit_overlays(
        [ordinary, protected, excess],
        existing={},
        normal_limit=1,
        hard_limit=2,
        require_behavioral_difference=True,
    )
    assert projected[ordinary.overlay_id].status == OverlayStatus.ACTIVE
    assert projected[protected.overlay_id].status == OverlayStatus.ACTIVE
    assert excess.overlay_id not in projected
    assert any("hard overlay limit" in item for item in notes.rejected)


def test_persistent_lead_reconstructs_after_resume_failure_and_counts_failed_attempt(
    tmp_path: Path, fake_config
) -> None:
    provider = ResumeFailsOnce()
    engine = FrontierEngine.create(
        "Produce one exact-task result and preserve continuity.",
        config=fake_config(),
        provider=provider,
    )
    try:
        asyncio.run(engine.execute())
        reconstruction_events = [
            item for item in engine.events() if item.event_type == et.LEAD_RECONSTRUCTION
        ]
        assert len(reconstruction_events) == 1
        reconstructed = reconstruction_events[0].payload["lead_session"]
        assert reconstructed["status"] == "reconstructed_verified"
        assert reconstruction_events[0].payload["resume_failure_trace"] is not None
        # Normal deterministic path uses five calls; failed resume is also durable usage.
        assert engine.state.usage.calls == 6
        assert engine.state.lead_session.status.value == "continuous"
    finally:
        engine.close()


def test_failed_resume_and_reconstruction_preserve_both_costs(tmp_path: Path, fake_config) -> None:
    engine = FrontierEngine.create(
        "Preserve both failed continuity attempts.",
        config=fake_config(run={"fail_fast_on_provider_error": True}),
        provider=ResumeAndReconstructionFail(),
    )
    try:
        with pytest.raises(ProviderCallError) as caught:
            asyncio.run(engine.execute())
        usage = caught.value.frontier_usage  # type: ignore[attr-defined]
        assert usage.calls == 2
        assert usage.model_requests == 4
        assert usage.input_tokens == 14
        failed = [item for item in engine.events() if item.event_type == et.CHECKPOINT_FAILED]
        assert failed[-1].payload["usage"]["calls"] == 2
    finally:
        engine.close()


def test_forced_summit_keeps_upper_tail_capability_reachable_without_grid_search(
    tmp_path: Path, fake_config
) -> None:
    config = fake_config(
        summit={"mode": "on", "profile": "deep"},
        run={
            "budget": {
                "max_rounds": 3,
                "max_calls": 12,
                "max_parallel": 2,
                "synthesis_reserve_calls": 3,
            }
        },
    )
    engine = FrontierEngine.create(
        "Solve the same task while permitting bounded upper-tail mechanism search.",
        config=config,
    )
    try:
        asyncio.run(engine.execute())
        assert engine.state.summit_active
        assert "summit.mode=on" in engine.state.summit_reasons
        summit_actions = [
            record.spec
            for record in engine.state.actions.values()
            if record.spec.topology.value == "summit"
        ]
        assert summit_actions
        assert any("exact-task" in item.target for item in summit_actions)
        assert any(item.discovery_operator is not None for item in summit_actions)
        assert engine.state.summit_lineages
        assert sum(item.attempts for item in engine.state.discovery_records.values()) >= 1
        assert sum(item.accepted_results for item in engine.state.discovery_records.values()) >= 1
        assert len(engine.state.overlays) <= config.cognition.hard_overlay_limit
    finally:
        engine.close()


def test_extension_archives_prior_seal_replans_and_releases_fresh(
    tmp_path: Path, fake_config
) -> None:
    config = fake_config(
        run={
            "budget": {
                "max_rounds": 2,
                "max_calls": 8,
                "max_parallel": 2,
                "synthesis_reserve_calls": 3,
            }
        }
    )
    engine = FrontierEngine.create("Develop this exact task deeply.", config=config)
    run_dir = engine.run_dir
    try:
        asyncio.run(engine.execute())
        first_count = engine.ledger.count()
        first_budget = engine.config.run.budget.max_calls
        first_digest = engine.state.final_artifact.blob.digest  # type: ignore[union-attr]
        asyncio.run(engine.extend(additional_calls=6))
        assert engine.state.phase.value == "complete"
        assert engine.state.metadata["extension_count"] == 1
        assert engine.config.run.budget.max_calls == first_budget + 6
        assert engine.ledger.count() > first_count
        assert engine.state.metadata["semantic_ci_passed"] is True
        assert engine.state.completion_case is not None
        assert engine.state.final_artifact is not None
        assert engine.state.final_artifact.blob.digest != first_digest
        assert (run_dir / "seal-history" / "seal-001.json").is_file()
        assert not (run_dir / "extension.intent.json").exists()
        assert engine.verify_integrity()["sealed"] is True
    finally:
        engine.close()

    loaded = FrontierEngine.load(run_dir)
    try:
        assert loaded.config.run.budget.max_calls == first_budget + 6
        assert loaded.state.metadata["extension_count"] == 1
    finally:
        loaded.close()


def test_semantic_ci_flags_loss_of_a_protected_strength() -> None:
    from frontier_harness.models import CompletionCase, RunState
    from frontier_harness.semantic_ci import run_semantic_ci

    source = capture_task_source("Preserve the unique causal mechanism in the final report.")
    state = RunState(
        run_id="run_semantic_ci",
        created_at=utc_now(),
        source_prompt=source.original_text,
        task_source=source,
        artifact_spine=ArtifactSpine(
            central_thesis="The unique causal mechanism explains the result.",
            must_preserve=["unique causal mechanism"],
        ),
    )
    report = run_semantic_ci(
        state=state,
        prior_text="The unique causal mechanism explains the result.",
        final_text="The result is acceptable.",
        model_findings=[
            SemanticRegressionFinding(
                severity="low",
                property="unique causal mechanism",
                prior_value="Present",
                final_value="Preserved",
                disposition="preserved",
                rationale="Self-reported by the synthesizer.",
            )
        ],
        completion_case=CompletionCase(task_source_digest=source.digest),
    )
    assert report.passed is False
    assert any(
        item.property == "unique causal mechanism" and item.disposition == "restore"
        for item in report.findings
    )


def test_completion_case_cannot_claim_an_open_obligation_is_satisfied() -> None:
    from frontier_harness.cognition import completion_case_gaps
    from frontier_harness.models import RunState

    source = capture_task_source("Deliver the verified result.")
    obligation = Obligation(
        obligation_id="obl_open",
        title="Verify the result",
        requirement="The result must be verified",
        kind="verification",
        acceptance="A passing independent check",
        impact=Impact.FATAL,
        status=ObligationStatus.OPEN,
        release_blocking=True,
    )
    state = RunState(
        run_id="run_open_claim",
        created_at=utc_now(),
        source_prompt=source.original_text,
        task_source=source,
        obligations={obligation.obligation_id: obligation},
    )
    completion = CompletionCase(
        task_source_digest=source.digest,
        claims=[
            CompletionClaim(
                obligation_id=obligation.obligation_id,
                artifact_location="Final artifact",
                evidence_or_test=["self assertion"],
                status="satisfied",
            )
        ],
    )
    assert completion_case_gaps(state, completion) == [
        "release-blocking obligation unresolved: obl_open"
    ]


def test_task_source_requirements_are_compiled_even_with_model_obligations() -> None:
    source = capture_task_source(
        "The final video must be five minutes. Do not ship without watching the rendered sequence."
    )
    contract = GoalContract(
        original_request=source.original_text,
        deliverable="A finished educational video",
    )
    charter, guards = compile_guard_obligations(
        source, contract, fallback_charter(source, contract)
    )
    exact_traces = {item.requirement_id: item for item in charter.requirement_traces}
    assert any("must be five minutes" in item.source_text for item in exact_traces.values())
    watched = next(
        item
        for item in guards
        if any(
            "watching the rendered sequence" in exact_traces[requirement_id].source_text
            for requirement_id in item.source_requirement_ids
        )
    )
    assert watched.release_blocking
    assert EvidenceModality.TEMPORAL_VISUAL in watched.required_evidence_modalities
    assert any(trace.category == "prohibition" for trace in charter.requirement_traces)


def test_requirement_compiler_recovers_markdown_blocks_without_fragment_debt() -> None:
    from frontier_harness.cognition import compile_requirement_traces

    traces = compile_requirement_traces(
        """# Required files and build contract

The renderer must produce the master and proxy
without human interaction.

- Do not ship an unverified export.
- Optional background notes may be included.
"""
    )
    texts = [item.source_text for item in traces]
    assert texts == [
        "The renderer must produce the master and proxy without human interaction.",
        "Do not ship an unverified export.",
    ]


def test_guard_compiler_maps_exact_source_traces_into_one_coherent_obligation() -> None:
    source = capture_task_source(
        "The final video must be five minutes. Do not ship without watching the rendered sequence."
    )
    contract = GoalContract(
        original_request=source.original_text,
        deliverable="A finished educational video",
    )
    charter = fallback_charter(source, contract)
    existing = ObligationDraft(
        local_key="finished-film",
        title="Finished verified film",
        requirement="Deliver the five-minute film only after watching the rendered sequence.",
        kind="deliverable",
        acceptance="The exact master exists and was watched.",
        impact=Impact.FATAL,
        release_blocking=True,
        source_requirement_ids=[
            item.requirement_id for item in charter.requirement_traces
        ],
    )
    compiled, guards = compile_guard_obligations(
        source,
        contract,
        charter,
        existing_drafts=[existing],
    )
    covered = set(existing.source_requirement_ids)
    assert not guards
    assert covered == {
        item.requirement_id for item in compiled.requirement_traces if item.release_blocking
    }


def test_model_cannot_invent_an_unavailable_human_release_channel() -> None:
    from frontier_harness.cognition import instantiate_obligations

    source = capture_task_source("The rendered film must be inspected before release.")
    contract = GoalContract(original_request=source.original_text, deliverable="A film")
    charter = fallback_charter(source, contract)
    created, _, notes = instantiate_obligations(
        [
            ObligationDraft(
                local_key="review",
                title="Inspect film",
                requirement="Inspect the rendered film",
                kind="verification",
                acceptance="Inspection passes",
                impact=Impact.FATAL,
                required_evidence_modalities=[
                    EvidenceModality.TEMPORAL_VISUAL,
                    EvidenceModality.HUMAN_OBSERVATION,
                ],
            )
        ],
        charter=charter,
        human_evidence_available=False,
    )
    assert created[0].required_evidence_modalities == [EvidenceModality.TEMPORAL_VISUAL]
    assert any("model-invented" in warning for warning in notes.warnings)


def test_local_evidence_cannot_satisfy_a_whole_artifact_obligation() -> None:
    from frontier_harness.cognition import completion_case_gaps
    from frontier_harness.models import RunState

    source = capture_task_source("The whole film must sustain motion quality.")
    blob = BlobRef(digest="c" * 64, size=1, relative_path="blobs/c")
    artifact = ArtifactRef(
        artifact_id="art_scope",
        version=1,
        blob=blob,
        created_at=utc_now(),
    )
    obligation = Obligation(
        obligation_id="obl_scope",
        title="Whole-film motion",
        requirement="Sustain motion quality across the whole film",
        kind="verification",
        acceptance="Whole-film temporal inspection passes",
        impact=Impact.FATAL,
        status=ObligationStatus.SATISFIED,
        required_evidence_modalities=[EvidenceModality.TEMPORAL_VISUAL],
        required_artifact_scope="whole_artifact",
    )
    evidence = EvidenceRecord(
        evidence_id="evd_slice",
        kind="slice_review",
        summary="A representative slice passed",
        scope="30-second slice",
        artifact_scope="sequence",
        independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
        modalities=[EvidenceModality.TEMPORAL_VISUAL],
        artifact_digest=artifact.blob.digest,
    )
    state = RunState(
        run_id="run_scope",
        created_at=utc_now(),
        source_prompt=source.original_text,
        task_source=source,
        current_artifact=artifact,
        final_artifact=artifact,
        obligations={obligation.obligation_id: obligation},
        evidence={evidence.evidence_id: evidence},
    )
    completion = CompletionCase(
        task_source_digest=source.digest,
        artifact_digest=artifact.blob.digest,
        claims=[
            CompletionClaim(
                obligation_id=obligation.obligation_id,
                artifact_location="master.mp4",
                evidence_or_test=[evidence.evidence_id],
                status="satisfied",
            )
        ],
    )
    assert any("whole_artifact" in gap for gap in completion_case_gaps(state, completion))
    state.evidence[evidence.evidence_id] = evidence.model_copy(
        update={"artifact_scope": "whole_artifact"}
    )
    assert completion_case_gaps(state, completion) == []


def test_experiential_release_construction_gets_one_fresh_global_challenge() -> None:
    from frontier_harness.models import RunState

    engine = object.__new__(FrontierEngine)
    source = ActionSpec(
        action_id="act_render",
        round_index=1,
        kind=ActionKind.INSTRUMENT,
        target="full film render",
        assignment="Render and inspect the full film",
        impact=Impact.FATAL,
        cost=CostBand.EXPENSIVE,
        independence_class=IndependenceClass.SAME_MODEL,
        expected_decision_effect="admit or repair the full film",
        artifact_scope="release",
        observation_modalities=[EvidenceModality.TEMPORAL_VISUAL, EvidenceModality.AUDIO],
    )
    engine.state = RunState(
        run_id="run_review",
        created_at=utc_now(),
        actions={source.action_id: ActionRecord(spec=source)},
    )
    proposals: list[ActionProposal] = []
    engine._ensure_fresh_global_review(proposals, accepted_action_ids=[source.action_id])
    assert len(proposals) == 1
    review = proposals[0]
    assert review.kind == ActionKind.DISCRIMINATE
    assert review.artifact_scope == "release"
    assert review.independence_class == IndependenceClass.DIFFERENT_CONDITIONING
    assert review.potency_check
    engine._ensure_fresh_global_review(proposals, accepted_action_ids=[source.action_id])
    assert len(proposals) == 1


def test_completion_requires_artifact_bound_evidence_in_the_declared_modality() -> None:
    from frontier_harness.cognition import completion_case_gaps
    from frontier_harness.models import RunState

    source = capture_task_source("The final video must be watched.")
    blob = BlobRef(
        digest="a" * 64,
        size=1,
        relative_path="blobs/a",
    )
    artifact = ArtifactRef(
        artifact_id="art_final",
        version=1,
        blob=blob,
        created_at=utc_now(),
    )
    obligation = Obligation(
        obligation_id="obl_video",
        title="Watch the final video",
        requirement="The final video must be watched",
        kind="verification",
        acceptance="Temporal inspection",
        impact=Impact.FATAL,
        status=ObligationStatus.SATISFIED,
        required_evidence_modalities=[EvidenceModality.TEMPORAL_VISUAL],
    )
    stale = EvidenceRecord(
        evidence_id="evd_stale",
        kind="review",
        summary="Watched an earlier cut",
        scope="earlier cut",
        independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
        modalities=[EvidenceModality.TEMPORAL_VISUAL],
        artifact_digest="b" * 64,
    )
    state = RunState(
        run_id="run_modality",
        created_at=utc_now(),
        source_prompt=source.original_text,
        task_source=source,
        current_artifact=artifact,
        final_artifact=artifact,
        obligations={obligation.obligation_id: obligation},
        evidence={stale.evidence_id: stale},
    )
    completion = CompletionCase(
        task_source_digest=source.digest,
        artifact_digest=artifact.blob.digest,
        claims=[
            CompletionClaim(
                obligation_id=obligation.obligation_id,
                artifact_location="final video",
                evidence_or_test=[stale.evidence_id],
                status="satisfied",
            )
        ],
    )
    assert any("temporal_visual" in gap for gap in completion_case_gaps(state, completion))
    state.evidence[stale.evidence_id] = stale.model_copy(
        update={"artifact_digest": artifact.blob.digest}
    )
    assert completion_case_gaps(state, completion) == []


def test_legacy_mode_preserves_sparse_control_without_v35_promotions(
    tmp_path: Path, fake_config
) -> None:
    config = fake_config(
        cognition={
            "mode": "legacy",
            "persistent_lead": False,
            "semantic_regression": True,
        },
        summit={"mode": "off"},
    )
    engine = FrontierEngine.create(
        "Solve this task through the sparse issue and probe path.",
        config=config,
    )
    try:
        asyncio.run(engine.execute())
        assert engine.state.phase.value == "complete"
        assert engine.state.obligations == {}
        assert engine.state.cruxes == {}
        assert engine.state.overlays == {}
        assert engine.state.summit_lineages == {}
        assert engine.state.summit_active is False
        assert engine.state.lead_session.thread_id is None
        assert engine.state.issues
        assert engine.verify_integrity()["sealed"] is True
    finally:
        engine.close()
