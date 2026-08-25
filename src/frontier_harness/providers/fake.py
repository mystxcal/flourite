"""Deterministic provider for tests, demos, and offline architecture work.

The fake provider deliberately exercises the v3.5 semantic boundary: immutable
Task Source, Lead continuity, obligations/cruxes, action receipts, shared
substrate, semantic synthesis, Completion Case, and bounded release. It makes no
network or model call.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ..models import (
    ActionKind,
    ActionObservation,
    ActionProposal,
    ArenaJudgeOutput,
    ArtifactSpine,
    BootstrapOutput,
    CharterAssertion,
    CharterProvenance,
    CheckpointOutput,
    CompletionCase,
    CompletionClaim,
    CostBand,
    CruxDraft,
    CruxStatus,
    CruxUpdate,
    EpistemicMode,
    FinalOutput,
    GoalContract,
    Impact,
    IndependenceClass,
    IssueDraft,
    IssueStatus,
    IssueUpdate,
    LeadContinuityAck,
    ObligationDraft,
    ObligationStatus,
    ObligationUpdate,
    ReleaseOutput,
    RepairOutput,
    SubstrateEntry,
    SummitLineage,
    SummitLineageStatus,
    TaskCharter,
    Uncertainty,
    Usage,
    ValueBand,
    WorkerEnvelope,
)
from ..util import atomic_write_text, canonical_json, sha256_text
from .base import (
    ModelProvider,
    ProviderCallRequest,
    ProviderCallResult,
    ProviderDoctorResult,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class FakeProvider(ModelProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.thread_id = "fake-lead-session"

    async def doctor(self) -> ProviderDoctorResult:
        return ProviderDoctorResult(
            ok=True,
            provider="fake",
            version="deterministic-3.5",
            auth_mode="offline",
            details=["Deterministic fake provider; no model call was made."],
        )

    @staticmethod
    def _artifact_path(request: ProviderCallRequest[ResponseT]) -> Path:
        path = request.expected_artifact_path or (request.cwd / "output" / "artifact.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _read_json(request: ProviderCallRequest[Any], name: str) -> dict[str, Any]:
        path = request.cwd / "context" / name
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _ack(self, request: ProviderCallRequest[Any]) -> LeadContinuityAck | None:
        if not request.lead_call:
            return None
        source = self._read_json(request, "TASK_SOURCE.json")
        state = self._read_json(request, "STATE.json")
        artifact = state.get("artifact") or {}
        blob = artifact.get("blob") or {}
        obligations = state.get("open_obligations") or []
        cruxes = state.get("active_cruxes") or []
        spine = state.get("artifact_spine") or {}
        metadata_obligations = [
            str(item) for item in request.metadata.get("open_obligation_ids", []) if item
        ]
        metadata_cruxes = [
            str(item) for item in request.metadata.get("active_crux_ids", []) if item
        ]
        digest = str(
            request.metadata.get("task_source_digest")
            or source.get("digest")
            or sha256_text(str(request.metadata.get("task", "task")).strip())
        )
        return LeadContinuityAck(
            task_source_digest=digest,
            current_artifact_digest=blob.get("digest"),
            active_obligation_ids=(
                metadata_obligations
                or [
                    str(item.get("obligation_id"))
                    for item in obligations
                    if item.get("obligation_id")
                ]
            ),
            active_crux_ids=(
                metadata_cruxes
                or [str(item.get("crux_id")) for item in cruxes if item.get("crux_id")]
            ),
            artifact_spine_revision=(
                int(spine["revision"]) if spine.get("revision") is not None else None
            ),
        )

    async def run(self, request: ProviderCallRequest[ResponseT]) -> ProviderCallResult[ResponseT]:
        started = time.monotonic()
        self.calls += 1
        response_type = request.response_model
        metadata = request.metadata
        artifact_path = self._artifact_path(request)
        relative_artifact = artifact_path.relative_to(request.cwd).as_posix()
        source_digest = str(
            metadata.get("task_source_digest")
            or self._read_json(request, "TASK_SOURCE.json").get("digest")
            or sha256_text(str(metadata.get("task", "task")).strip())
        )

        if response_type is BootstrapOutput:
            task = str(metadata.get("task", "Demonstrate Flourite"))
            atomic_write_text(
                artifact_path,
                "# Baseline artifact\n\n"
                f"Task: {task}\n\n"
                "This is a credible first pass. The live frontier contains only the "
                "uncertainties that could materially change it.\n",
            )
            response: BaseModel = BootstrapOutput(
                goal_contract=GoalContract(
                    original_request=task,
                    deliverable="A complete, decision-useful artifact",
                    hard_constraints=["Preserve the user's actual objective"],
                    soft_objectives=["High final value", "High value per token"],
                    stakes="medium",
                    quality_floor="high",
                ),
                task_charter=TaskCharter(
                    source_digest=source_digest,
                    deliverable="A complete, decision-useful artifact",
                    real_world_purpose="Satisfy the exact supplied task.",
                    assertions=[
                        CharterAssertion(
                            key="deliverable",
                            statement="A complete, decision-useful artifact",
                            provenance=CharterProvenance.EXPLICIT,
                        )
                    ],
                    hard_constraints=["Preserve the user's actual objective"],
                    soft_objectives=["High final value", "High value per token"],
                ),
                artifact_spine=ArtifactSpine(
                    central_thesis="Solve the exact task with one evidence-backed artifact.",
                    architecture=["Baseline", "Targeted validation", "Clean synthesis"],
                    hard_invariants=["Preserve the user's actual objective"],
                    must_preserve=["The exact requested deliverable"],
                ),
                obligations=[
                    ObligationDraft(
                        local_key="deliverable",
                        title="Deliver the exact requested artifact",
                        requirement="A complete, decision-useful artifact",
                        kind="deliverable",
                        acceptance="The final artifact directly answers the immutable task.",
                        impact=Impact.FATAL,
                        release_blocking=True,
                    ),
                    ObligationDraft(
                        local_key="task_fidelity",
                        title="Preserve task fidelity",
                        requirement="Preserve the user's actual objective",
                        kind="constraint",
                        acceptance="No reframe changes the requested destination.",
                        impact=Impact.FATAL,
                        release_blocking=True,
                    ),
                ],
                cruxes=[
                    CruxDraft(
                        local_key="central_validation",
                        title="Central result needs an independent challenge",
                        uncertainty="The baseline has not yet been tested by a distinct evidence channel.",
                        decision_controlled="A contrary result would materially revise the artifact.",
                        competing_possibilities=[
                            "The central result survives the scoped challenge.",
                            "The central result requires revision.",
                        ],
                        why_it_matters="It controls confidence in the load-bearing result.",
                        obligation_keys=["deliverable", "task_fidelity"],
                        discriminating_evidence=["Run a scoped deterministic challenge."],
                        unlock_value=Impact.HIGH,
                    )
                ],
                artifact_path=relative_artifact,
                artifact_summary="A coherent baseline that exposes one load-bearing issue.",
                issues=[
                    IssueDraft(
                        local_key="central_validation",
                        title="Central result needs an independent challenge",
                        description="The baseline has not yet been tested by a distinct evidence channel.",
                        impact=Impact.HIGH,
                        decision_sensitivity="A contrary result would materially revise the artifact.",
                    )
                ],
                actions=[
                    ActionProposal(
                        kind=ActionKind.DISCRIMINATE,
                        target="Central result",
                        assignment="Attempt the cheapest credible independent falsification of the central result.",
                        issue_ids=["central_validation"],
                        obligation_keys=["deliverable", "task_fidelity"],
                        crux_keys=["central_validation"],
                        impact=Impact.HIGH,
                        cost=CostBand.CHEAP,
                        independence_class=IndependenceClass.DETERMINISTIC_TOOL,
                        epistemic_mode=EpistemicMode.EXECUTE,
                        execution_trigger=(
                            "The central claim survived thought but still needs a deterministic "
                            "falsification before it can be retained."
                        ),
                        expected_decision_effect="Confirm or force revision of the central result.",
                        reusable_value=ValueBand.MEDIUM,
                    )
                ],
                continuity_ack=LeadContinuityAck(
                    task_source_digest=source_digest,
                    active_obligation_ids=[],
                    active_crux_ids=[],
                ),
            )
        elif response_type is WorkerEnvelope:
            result_path = request.cwd / "output" / "result.md"
            atomic_write_text(
                result_path,
                "# Targeted finding\n\nThe central result survived the scoped challenge. "
                "The test does not establish every peripheral claim.\n",
            )
            action_id = str(metadata.get("action_id", request.call_id))
            lineage = None
            if metadata.get("topology") == "summit":
                operator = str(metadata.get("discovery_operator") or "develop")
                raw_parents = metadata.get("parent_lineage_ids") or []
                parent_ids = [str(item) for item in raw_parents if item]
                incumbent_id = str(metadata.get("lineage_id") or "")
                creates_child = operator in {"mutate", "crossover", "revive"}
                lineage_id = (
                    f"lin-{action_id}" if creates_child or not incumbent_id else incumbent_id
                )
                lineage = SummitLineage(
                    lineage_id=lineage_id,
                    name=f"Offline {operator} lineage",
                    thesis="A causally distinct exact-task mechanism can improve the result.",
                    mechanism=f"Deterministic offline {operator} mechanism.",
                    unresolved_questions=["Does the mechanism survive one independent check?"],
                    evidence_for=["The offline boundary produced a concrete lineage state."],
                    behavioral_descriptors=[f"offline-{operator}"],
                    parent_lineage_ids=parent_ids,
                    generation=1 if parent_ids else 0,
                    development_history=[f"{operator} action {action_id}"],
                    status=SummitLineageStatus.ACTIVE,
                    quality=ValueBand.MEDIUM,
                    potential=ValueBand.HIGH,
                    novelty=ValueBand.HIGH,
                    leverage=ValueBand.HIGH,
                    robustness=ValueBand.MEDIUM,
                    uncertainty=Uncertainty.MEDIUM,
                )
            response = WorkerEnvelope(
                target=str(metadata.get("target", "Central result")),
                result_or_artifact_reference=result_path.relative_to(request.cwd).as_posix(),
                findings=["The targeted challenge found no material defect."],
                evidence_references=["output/result.md"],
                unresolved_risks=["Peripheral claims were outside this probe's scope."],
                materiality="high",
                decision_effect="Raises confidence in the current artifact without expanding scope.",
                scope="The central result under the scoped deterministic challenge.",
                negative_result=True,
                action_receipt=ActionObservation(
                    action_id=action_id,
                    observed_result="The targeted challenge found no material defect.",
                    state_changes=["The central validation crux can be resolved."],
                    decisions_changed=["Retain the central result."],
                    obligations_unlocked=list(metadata.get("active_obligation_ids", [])),
                    evidence_strength="strong",
                    evidence_scope="The central result under the scoped deterministic challenge.",
                    matched_outcome_index=0,
                    outcome_match="matched",
                ),
                substrate_entries=[
                    SubstrateEntry(
                        entry_id=f"sub-{action_id}",
                        kind="test",
                        statement="The scoped deterministic challenge found no material defect.",
                        scope="The central result only.",
                        evidence_references=["output/result.md"],
                        source_action_id=action_id,
                        global_admission=True,
                        confidence="verified",
                    )
                ],
                lineage=lineage,
                continuity_ack=self._ack(request),
            )
        elif response_type is CheckpointOutput:
            current = str(metadata.get("current_artifact_text", ""))
            atomic_write_text(
                artifact_path,
                current + "\n\n## Evidence-backed refinement\n\n"
                "A targeted independent challenge found no material defect in the load-bearing result.\n",
            )
            issue_ids = [str(item) for item in metadata.get("open_issue_ids", [])]
            obligation_ids = [str(item) for item in metadata.get("open_obligation_ids", [])]
            crux_ids = [str(item) for item in metadata.get("active_crux_ids", [])]
            response = CheckpointOutput(
                artifact_path=relative_artifact,
                artifact_summary="The baseline was strengthened using the decision-relevant probe.",
                artifact_spine=ArtifactSpine(
                    central_thesis="Solve the exact task with one evidence-backed artifact.",
                    architecture=["Baseline", "Validated central result", "Clean synthesis"],
                    key_decisions=["Retain the central result after scoped falsification."],
                    hard_invariants=["Preserve the user's actual objective"],
                    must_preserve=[
                        "The exact requested deliverable",
                        "The scoped validation result",
                    ],
                    revision=2,
                ),
                issue_updates=[
                    IssueUpdate(
                        issue_id=issue_id,
                        status=IssueStatus.RESOLVED,
                        uncertainty=Uncertainty.LOW,
                        evidence_for=["The scoped independent challenge found no material defect."],
                        resolution="Resolved to the quality floor by a targeted independent probe.",
                    )
                    for issue_id in issue_ids
                ],
                obligation_updates=[
                    ObligationUpdate(
                        obligation_id=obligation_id,
                        status=ObligationStatus.SATISFIED,
                        evidence_references=[
                            "The accepted deterministic challenge and integrated artifact."
                        ],
                        artifact_location="Baseline artifact and evidence-backed refinement",
                        resolution="Satisfied in the integrated artifact.",
                    )
                    for obligation_id in obligation_ids
                ],
                crux_updates=[
                    CruxUpdate(
                        crux_id=crux_id,
                        status=CruxStatus.RESOLVED,
                        resolution="The scoped challenge found no material defect.",
                        evidence_references=["The accepted deterministic challenge."],
                    )
                    for crux_id in crux_ids
                ],
                accepted_action_ids=list(metadata.get("completed_action_ids", [])),
                continuity_ack=self._ack(request),
                stop=True,
                stop_reason="No high-impact unresolved issue remains in the demo.",
            )
        elif response_type is FinalOutput:
            current = str(metadata.get("current_artifact_text", ""))
            atomic_write_text(
                artifact_path,
                current + "\n\n## Final synthesis\n\n"
                "The artifact is rebuilt coherently from the accepted decisions and evidence.\n",
            )
            state = self._read_json(request, "STATE.json")
            obligations = state.get("open_obligations") or []
            # STATE only exposes open obligations. Read the full run state when
            # available through the copied JSON; completed cases can still be
            # recovered from the obligation IDs supplied in metadata.
            all_ids = [str(item) for item in metadata.get("open_obligation_ids", [])]
            if not all_ids:
                all_ids = [
                    str(item.get("obligation_id"))
                    for item in obligations
                    if item.get("obligation_id")
                ]
            # The fake checkpoint normally closes all obligations, so inspect
            # the full capsule state file's task charter and use the engine's
            # completion normalizer to add any missing claims.
            response = FinalOutput(
                artifact_path=relative_artifact,
                summary="Clean final synthesis completed.",
                remaining_uncertainty=["Only low-impact peripheral uncertainty remains."],
                artifact_spine=ArtifactSpine(
                    central_thesis="Solve the exact task with one evidence-backed artifact.",
                    architecture=["Validated result", "Coherent final deliverable"],
                    key_decisions=["Retain the central result after scoped falsification."],
                    hard_invariants=["Preserve the user's actual objective"],
                    must_preserve=[
                        "The exact requested deliverable",
                        "The scoped validation result",
                    ],
                    revision=3,
                ),
                completion_case=CompletionCase(
                    task_source_digest=source_digest,
                    claims=[
                        CompletionClaim(
                            obligation_id=obligation_id,
                            artifact_location="Final artifact",
                            evidence_or_test=["Integrated evidence and final artifact"],
                            status="satisfied",
                        )
                        for obligation_id in all_ids
                    ],
                    preserved_insights=[
                        "The exact requested deliverable",
                        "The scoped validation result",
                    ],
                ),
                continuity_ack=self._ack(request),
                release_gate_recommended=True,
            )
        elif response_type is ArenaJudgeOutput:
            # Deterministic offline arena boundary. Position balancing is tested
            # by the arena runner; the fake judge deliberately returns A.
            response = ArenaJudgeOutput(
                winner="A",
                confidence="medium",
                rationale="Candidate A is selected by the deterministic offline judge fixture.",
                decisive_factors=["offline boundary exercised"],
            )
        elif response_type is ReleaseOutput:
            response = ReleaseOutput(
                findings=[],
                requires_repair=False,
                releaseable=True,
                rationale="No fatal error, major omission, task drift, unsupported load-bearing claim, or completion-case defect was found.",
                task_fidelity_passed=True,
                completion_case_valid=True,
                strongest_alternative_addressed=True,
            )
        elif response_type is RepairOutput:
            current = str(metadata.get("current_artifact_text", ""))
            atomic_write_text(artifact_path, current + "\n\nRelease findings repaired.\n")
            response = RepairOutput(
                artifact_path=relative_artifact,
                repaired_findings=["All supplied material findings"],
                continuity_ack=self._ack(request),
            )
        else:
            raise TypeError(f"FakeProvider does not support {response_type.__name__}")

        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(request.output_path, canonical_json(response.model_dump(mode="json")))
        raw_events_path = request.output_path.parent / "codex-events.jsonl"
        effective_thread_id = request.resume_thread_id or (
            self.thread_id if request.preserve_session or request.lead_call else None
        )
        events: list[dict[str, Any]] = []
        if effective_thread_id and not request.resume_thread_id:
            events.append({"type": "thread.started", "thread_id": effective_thread_id})
        events.append(
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 10}}
        )
        atomic_write_text(
            raw_events_path,
            "\n".join(json.dumps(item) for item in events) + "\n",
        )
        return ProviderCallResult(
            call_id=request.call_id,
            response=response_type.model_validate(response.model_dump(mode="json")),
            usage=Usage(calls=1, model_requests=1, input_tokens=10, output_tokens=10),
            duration_seconds=time.monotonic() - started,
            thread_id=effective_thread_id,
            resumed=bool(request.resume_thread_id),
            raw_events_path=raw_events_path,
            command=[
                "fake-provider",
                request.call_kind,
                *(["resume", request.resume_thread_id] if request.resume_thread_id else []),
            ],
        )
