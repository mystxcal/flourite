"""Semantic regression and completion-case validation.

The runtime cannot prove prose quality deterministically, but it can enforce the
traceability contract around load-bearing obligations and preserve explicitly
protected properties across final synthesis. Domain adapters may add stronger
executable checks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .cognition import completion_case_gaps
from .models import (
    ArtifactSpine,
    CompletionCase,
    ObligationStatus,
    RunState,
    SemanticRegressionFinding,
)
from .util import normalize_key


@dataclass(slots=True)
class SemanticCIReport:
    passed: bool
    findings: list[SemanticRegressionFinding] = field(default_factory=list)
    deterministic_failures: list[str] = field(default_factory=list)
    completion_gaps: list[str] = field(default_factory=list)
    protected_properties: list[str] = field(default_factory=list)

    @property
    def material_failures(self) -> list[SemanticRegressionFinding]:
        return [
            item
            for item in self.findings
            if item.severity in {"fatal", "high"}
            and item.disposition not in {"preserved", "irrelevant"}
        ]


def protected_properties(state: RunState) -> list[str]:
    values: list[str] = []
    if state.task_charter:
        values.extend(state.task_charter.hard_constraints)
        values.extend(state.task_charter.unacceptable_failures)
    if state.artifact_spine:
        values.extend(state.artifact_spine.hard_invariants)
        values.extend(state.artifact_spine.must_preserve)
        values.extend(state.artifact_spine.key_decisions)
    values.extend(
        item.requirement
        for item in state.obligations.values()
        if item.status == ObligationStatus.SATISFIED and item.release_blocking
    )
    values.extend(
        item.statement
        for item in state.substrate.values()
        if item.global_admission and item.confidence in {"high", "verified"}
    )
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


def _contains_semantically(final_text: str, property_text: str) -> bool:
    """Cheap conservative lexical guard, not a semantic judge.

    It only flags a possible loss when none of the property's informative terms
    remain. A model release challenge owns the actual semantic adjudication.
    """

    final_key = normalize_key(final_text)
    prop_key = normalize_key(property_text)
    if not prop_key:
        return True
    if prop_key in final_key:
        return True
    tokens = [token for token in prop_key.split() if len(token) >= 5]
    if not tokens:
        return True
    present = sum(token in final_key for token in tokens)
    return present >= max(1, (len(tokens) + 1) // 2)


def run_semantic_ci(
    *,
    state: RunState,
    final_text: str,
    prior_text: str,
    model_findings: Sequence[SemanticRegressionFinding],
    completion_case: CompletionCase | None,
) -> SemanticCIReport:
    findings = [item.model_copy(deep=True) for item in model_findings]
    deterministic_failures: list[str] = []
    properties = protected_properties(state)
    for value in properties:
        key = normalize_key(value)
        prior_present = _contains_semantically(prior_text, value)
        final_present = _contains_semantically(final_text, value)
        if prior_present and not final_present:
            # A model-authored "preserved" disposition is not permitted to
            # suppress the independent lexical guard.
            findings.append(
                SemanticRegressionFinding(
                    severity="high",
                    property=value,
                    prior_value="Present in the prior integrated artifact or explicit state.",
                    final_value="No lexical evidence of preservation was found in the final artifact.",
                    disposition="restore",
                    rationale=(
                        "Deterministic semantic CI detected a possible loss. The fresh release "
                        "challenge must restore it or explicitly justify the trade-off."
                    ),
                )
            )
            deterministic_failures.append(value)
        elif not any(normalize_key(item.property) == key for item in findings):
            findings.append(
                SemanticRegressionFinding(
                    severity="low",
                    property=value,
                    prior_value="Protected property.",
                    final_value="Appears preserved in the final artifact.",
                    disposition="preserved",
                    rationale="Cheap lexical guard found no regression signal.",
                )
            )

    gaps = completion_case_gaps(state, completion_case)
    material = [
        item
        for item in findings
        if item.severity in {"fatal", "high"}
        and item.disposition not in {"preserved", "irrelevant"}
    ]
    return SemanticCIReport(
        passed=not material and not gaps,
        findings=findings,
        deterministic_failures=deterministic_failures,
        completion_gaps=gaps,
        protected_properties=properties,
    )


def spine_changed_materially(before: ArtifactSpine | None, after: ArtifactSpine | None) -> bool:
    if before is None or after is None:
        return before != after
    return (
        normalize_key(before.central_thesis) != normalize_key(after.central_thesis)
        or {normalize_key(item) for item in before.hard_invariants}
        != {normalize_key(item) for item in after.hard_invariants}
        or {normalize_key(item) for item in before.key_decisions}
        != {normalize_key(item) for item in after.key_decisions}
    )
