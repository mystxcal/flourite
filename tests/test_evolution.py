from __future__ import annotations

from frontier_harness.evolution import (
    EvaluationSplit,
    HarnessCandidate,
    HarnessPromotionGate,
    HarnessTrial,
    TrialOutcome,
)


def _candidate() -> HarnessCandidate:
    return HarnessCandidate(
        candidate_id="cand_1",
        baseline_fingerprint="base",
        candidate_fingerprint="candidate",
        failure_mode="stagnant lineages receive repeated elaboration",
        causal_change="use runtime-owned stalls to trigger one semantic mutation",
        predicted_effect="fewer repeated attempts and at least one improved held-out artifact",
        prediction_scope="upper-tail software tasks",
        protected_properties=["task fidelity", "ledger replay"],
        changed_components=["discovery controller"],
        shadow_case_ids=["shadow-a", "shadow-b"],
        held_out_case_digests=["held-a-digest", "held-b-digest"],
        source_trace_references=["trace://failure-cluster"],
    )


def _trial(
    case_id: str,
    digest: str,
    split: EvaluationSplit,
    outcome: TrialOutcome,
    *,
    prediction: bool = False,
) -> HarnessTrial:
    return HarnessTrial(
        candidate_id="cand_1",
        case_id=case_id,
        case_digest=digest,
        split=split,
        seed="seed-1",
        baseline_budget_fingerprint="budget",
        candidate_budget_fingerprint="budget",
        outcome=outcome,
        predicted_effect_observed=prediction,
        protected_properties_preserved=["task fidelity", "ledger replay"],
        trace_references=[f"trace://{case_id}"],
    )


def test_promotion_requires_matched_held_out_generalization() -> None:
    trials = [
        _trial(
            "shadow-a",
            "shadow-a-digest",
            EvaluationSplit.SHADOW,
            TrialOutcome.CANDIDATE,
            prediction=True,
        ),
        _trial("shadow-b", "shadow-b-digest", EvaluationSplit.SHADOW, TrialOutcome.TIE),
        _trial("held-a", "held-a-digest", EvaluationSplit.HELD_OUT, TrialOutcome.CANDIDATE),
        _trial("held-b", "held-b-digest", EvaluationSplit.HELD_OUT, TrialOutcome.TIE),
    ]
    decision = HarnessPromotionGate().evaluate(_candidate(), trials)
    assert decision.promotable
    assert decision.held_out_record == {"candidate": 1, "baseline": 0, "tie": 1}


def test_promotion_fails_closed_on_leakage_regression_or_unmatched_budget() -> None:
    trials = [
        _trial("shadow-a", "same", EvaluationSplit.SHADOW, TrialOutcome.CANDIDATE, prediction=True),
        _trial("shadow-b", "shadow-b-digest", EvaluationSplit.SHADOW, TrialOutcome.TIE),
        _trial("held-a", "same", EvaluationSplit.HELD_OUT, TrialOutcome.CANDIDATE),
        _trial("held-b", "held-b-digest", EvaluationSplit.HELD_OUT, TrialOutcome.BASELINE),
    ]
    trials[2].hard_regressions = ["ledger replay broke"]
    trials[3].candidate_budget_fingerprint = "larger-budget"
    decision = HarnessPromotionGate().evaluate(_candidate(), trials)
    assert not decision.promotable
    assert "shadow and held-out cases overlap" in decision.reasons
    assert "a protected behavior has a hard regression" in decision.reasons
    assert "solver budgets are not matched" in decision.reasons
