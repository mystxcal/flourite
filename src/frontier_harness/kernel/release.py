"""Projection of artifact-bound verification and release transitions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import events as et
from ..ledger import LedgerEvent
from ..models import (
    ArtifactRef,
    ArtifactSpine,
    CheckStageState,
    CompletionCase,
    EvidenceRecord,
    LeadSessionState,
    ReleaseOutput,
    RunPhase,
    RunState,
    SemanticRegressionFinding,
    Usage,
)

Handler = Callable[[RunState, dict[str, Any]], None]


class ReleaseProjector:
    event_types = frozenset(
        {
            et.FINALIZATION_STARTED,
            et.FINAL_SYNTHESIZED,
            et.FINALIZATION_FAILED,
            et.DETERMINISTIC_CHECK_COMPLETED,
            et.EVIDENCE_RECORDED,
            et.CHECK_STAGE_COMPLETED,
            et.CHECK_REPLAN_DECIDED,
            et.RELEASE_COMPLETED,
            et.RELEASE_FAILED,
            et.REPAIR_COMPLETED,
            et.REPAIR_FAILED,
            et.SEMANTIC_REGRESSION_COMPLETED,
            et.COMPLETION_CASE_BUILT,
        }
    )

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {
            et.FINALIZATION_STARTED: self._finalization_started,
            et.FINAL_SYNTHESIZED: self._final_synthesized,
            et.FINALIZATION_FAILED: self._finalization_failed,
            et.DETERMINISTIC_CHECK_COMPLETED: self._evidence,
            et.EVIDENCE_RECORDED: self._evidence,
            et.CHECK_STAGE_COMPLETED: self._stage,
            et.CHECK_REPLAN_DECIDED: self._check_replan,
            et.RELEASE_COMPLETED: self._release_completed,
            et.RELEASE_FAILED: self._release_failed,
            et.REPAIR_COMPLETED: self._repair_completed,
            et.REPAIR_FAILED: self._repair_failed,
            et.SEMANTIC_REGRESSION_COMPLETED: self._semantic_regression,
            et.COMPLETION_CASE_BUILT: self._completion_case,
        }
        self.event_types = frozenset(self._handlers)

    def apply(self, state: RunState, event: LedgerEvent) -> None:
        try:
            handler = self._handlers[event.event_type]
        except KeyError as exc:  # pragma: no cover - registry enforces this
            raise ValueError(f"Release projector cannot handle {event.event_type}") from exc
        handler(state, event.payload)

    @staticmethod
    def _finalization_started(state: RunState, _: dict[str, Any]) -> None:
        state.phase = RunPhase.FINALIZING

    @staticmethod
    def _final_synthesized(state: RunState, payload: dict[str, Any]) -> None:
        artifact = ArtifactRef.model_validate(payload["artifact"])
        state.current_artifact = artifact
        state.final_artifact = artifact
        state.artifact_history.append(artifact)
        state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
        state.runtime.release.remaining_uncertainty = payload.get("remaining_uncertainty", [])
        state.runtime.release.gate_recommended = bool(payload.get("release_gate_recommended", True))
        if payload.get("artifact_spine"):
            state.artifact_spine = ArtifactSpine.model_validate(payload["artifact_spine"])
        state.semantic_regression_findings = [
            SemanticRegressionFinding.model_validate(item)
            for item in payload.get("semantic_regression", [])
        ]
        if payload.get("completion_case"):
            state.completion_case = CompletionCase.model_validate(payload["completion_case"])
        if payload.get("lead_session"):
            state.lead_session = LeadSessionState.model_validate(payload["lead_session"])
        state.phase = RunPhase.RELEASE
        state.runtime.release.finalization_error = None

    @staticmethod
    def _finalization_failed(state: RunState, payload: dict[str, Any]) -> None:
        state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
        state.runtime.release.finalization_error = payload.get("error", "final synthesis failed")

    @staticmethod
    def _evidence(state: RunState, payload: dict[str, Any]) -> None:
        evidence = EvidenceRecord.model_validate(payload["evidence"])
        state.evidence[evidence.evidence_id] = evidence

    @staticmethod
    def _check_replan(state: RunState, payload: dict[str, Any]) -> None:
        decision = str(payload["decision"])
        verification = state.runtime.verification
        verification.replan_pending = False
        verification.replan_decision = decision
        verification.dead_end = (
            [] if decision == "corrective_actions" else list(payload.get("failures", []))
        )
        state.stop_requested = False
        state.stop_reason = None

    @staticmethod
    def _release_completed(state: RunState, payload: dict[str, Any]) -> None:
        state.release = ReleaseOutput.model_validate(payload["release"])
        state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
        if fingerprint := payload.get("rejection_fingerprint"):
            state.runtime.release.rejection_fingerprints.append(fingerprint)
        state.runtime.release.release_error = None

    @staticmethod
    def _release_failed(state: RunState, payload: dict[str, Any]) -> None:
        state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
        state.runtime.release.release_error = payload.get("error", "release challenge failed")

    @staticmethod
    def _repair_failed(state: RunState, payload: dict[str, Any]) -> None:
        state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
        state.runtime.release.repair_error = payload.get("error", "repair failed")

    @staticmethod
    def _semantic_regression(state: RunState, payload: dict[str, Any]) -> None:
        state.semantic_regression_findings = [
            SemanticRegressionFinding.model_validate(item) for item in payload.get("findings", [])
        ]
        verification = state.runtime.verification
        verification.semantic_ci_passed = bool(payload.get("passed", False))
        verification.semantic_ci_gaps = list(payload.get("completion_gaps", []))
        if "deterministic_failures" in payload:
            verification.deterministic_failures = list(payload.get("deterministic_failures", []))
        if payload.get("adjudication"):
            verification.adjudication = payload["adjudication"]

    @staticmethod
    def _completion_case(state: RunState, payload: dict[str, Any]) -> None:
        state.completion_case = CompletionCase.model_validate(payload["completion_case"])

    @staticmethod
    def _stage(state: RunState, payload: dict[str, Any]) -> None:
        stage = str(payload["stage"])
        artifact_digest = payload.get("artifact_digest")
        state.runtime.verification.stages[stage] = CheckStageState(
            artifact_digest=str(artifact_digest) if artifact_digest is not None else None,
            failed=bool(payload.get("failed", False)),
            failures=[str(item) for item in payload.get("failures", [])],
        )
        if stage in {"preflight", "candidate"}:
            state.runtime.verification.replan_pending = any(
                (result := state.runtime.verification.stages.get(candidate)) is not None
                and result.artifact_digest == artifact_digest
                and result.failed
                for candidate in ("preflight", "candidate")
            )
            if not state.runtime.verification.replan_pending:
                state.runtime.verification.dead_end = []

    @staticmethod
    def _repair_completed(state: RunState, payload: dict[str, Any]) -> None:
        artifact = ArtifactRef.model_validate(payload["artifact"])
        state.current_artifact = artifact
        state.final_artifact = artifact
        state.artifact_history.append(artifact)
        state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
        state.runtime.release.repair_completed = True
        state.runtime.release.repair_count += 1
        state.runtime.release.repair_remaining_uncertainty = [
            str(item) for item in payload.get("remaining_uncertainty", [])
        ]
        if payload.get("artifact_spine"):
            state.artifact_spine = ArtifactSpine.model_validate(payload["artifact_spine"])
        if payload.get("completion_case"):
            state.completion_case = CompletionCase.model_validate(payload["completion_case"])
        if payload.get("lead_session"):
            state.lead_session = LeadSessionState.model_validate(payload["lead_session"])
        state.runtime.release.repair_error = None
