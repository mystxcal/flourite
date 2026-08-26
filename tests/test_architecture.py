from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

from frontier_harness.models import (
    ArtifactRef,
    BootstrapRuntimeState,
    EvidenceRecord,
    ExtensionRuntimeState,
    FinalOutput,
    FrontierReplanRequest,
    IndependenceClass,
    PlanningRuntimeState,
    RepairLoopStop,
    ResourceDecision,
    ResourceRuntimeState,
    RunExtensionRecord,
    RunState,
    SemanticAdjudication,
    VerificationRuntimeState,
)
from frontier_harness.orchestration.release import ReleasePolicy

SOURCE = Path(__file__).parents[1] / "src" / "frontier_harness"


def _class_method_sizes(path: Path, class_name: str) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    target = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name: (node.end_lineno or node.lineno) - node.lineno + 1
        for node in target.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_engine_remains_a_capability_facade_not_a_semantic_god_method() -> None:
    sizes = _class_method_sizes(SOURCE / "engine.py", "FrontierEngine")
    assert sizes["execute"] <= 10
    assert sizes["_execute_action"] <= 15
    assert sizes["_checkpoint"] <= 10
    assert sizes["_advance_frontier"] <= 10
    assert max(sizes.values()) <= 450


def test_canonical_runtime_boundaries_remain_cognitively_bounded() -> None:
    capsule = _class_method_sizes(SOURCE / "capsule.py", "CapsuleBuilder")
    checkpoint = _class_method_sizes(
        SOURCE / "orchestration" / "checkpoint.py",
        "CheckpointExecutor",
    )
    provider = _class_method_sizes(
        SOURCE / "execution" / "calls.py",
        "ProviderCallExecutor",
    )
    engine = _class_method_sizes(SOURCE / "engine.py", "FrontierEngine")

    assert capsule["populate"] <= 70
    assert checkpoint["_integrate"] <= 130
    assert checkpoint["_project_semantics"] <= 90
    assert provider["invoke"] <= 65
    assert provider["_run_with_recovery"] <= 90
    assert engine["_invoke"] <= 40


def test_engine_depends_on_adapter_capabilities_not_concrete_adapters() -> None:
    source = (SOURCE / "engine.py").read_text(encoding="utf-8")
    assert "MarkdownAdapter" not in source
    assert "SoftwareAdapter" not in source


def test_engine_has_one_authoritative_event_write_path() -> None:
    source = (SOURCE / "engine.py").read_text(encoding="utf-8")
    assert "ledger.append(" not in source
    assert source.count("self.journal.append(") == 1
    assert source.count("journal.append(") == 3


def test_runtime_policy_fields_are_typed_domain_objects() -> None:
    assert ArtifactRef in get_args(
        BootstrapRuntimeState.model_fields["recovery_artifact"].annotation
    )
    assert SemanticAdjudication in get_args(
        VerificationRuntimeState.model_fields["adjudication"].annotation
    )
    assert FrontierReplanRequest in get_args(
        PlanningRuntimeState.model_fields["frontier_replan_pending"].annotation
    )
    assert ResourceDecision in get_args(ResourceRuntimeState.model_fields["decision"].annotation)
    assert RepairLoopStop in get_args(
        ResourceRuntimeState.model_fields["repair_loop_stop"].annotation
    )
    assert RunExtensionRecord in get_args(
        ExtensionRuntimeState.model_fields["last_event"].annotation
    )


def test_release_policy_is_pure_and_fail_closed() -> None:
    state = RunState(
        run_id="run-test",
        source_prompt="ship it",
        created_at="2026-01-01T00:00:00Z",
    )
    output = FinalOutput(artifact_path="final.md", summary="candidate")
    assert ReleasePolicy.should_challenge(
        policy="always",
        adaptive_mode=False,
        state=state,
        final_output=output,
        checks=[],
    )

    state.runtime.verification.semantic_ci_passed = False
    decision = ReleasePolicy.mutation_gate(
        state=state,
        checks=[
            EvidenceRecord(
                evidence_id="evidence-1",
                kind="test",
                summary="failed",
                scope="release",
                independence_class=IndependenceClass.DETERMINISTIC_TOOL,
                negative_result=True,
            )
        ],
        release_required=False,
        release=None,
        repair_completed=False,
    )
    assert decision.mutation_gate_passed is False
    assert decision.deterministic_checks_passed is False
