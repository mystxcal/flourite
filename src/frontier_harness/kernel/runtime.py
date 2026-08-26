"""Projection of run lifecycle, operator control, and recovery state."""

from __future__ import annotations

from typing import Any

from .. import events as et
from ..ledger import LedgerEvent
from ..models import (
    BudgetContract,
    FrontierReplanRequest,
    ObligationStatus,
    ReleaseRecovery,
    RepairLoopStop,
    ResourceDecision,
    ResourceState,
    RunExtensionRecord,
    RunPhase,
    RunState,
)


class RuntimeProjector:
    event_types = frozenset(
        {
            et.RUN_EXTENDED,
            et.RUN_PAUSED,
            et.RUN_RESUMED,
            et.RUN_STOPPED,
            et.RESOURCE_INITIALIZED,
            et.RESOURCE_DECIDED,
            et.REPAIR_LOOP_STOPPED,
            et.FRONTIER_REPLAN_REQUESTED,
            et.RELEASE_RECOVERY_REQUESTED,
            et.RUN_COMPLETED,
            et.RUN_FAILED,
            et.PATCH_APPLIED,
        }
    )

    def apply(self, state: RunState, event: LedgerEvent) -> None:
        payload = event.payload
        if event.event_type == et.RUN_EXTENDED:
            self._extend(state, payload)
        elif event.event_type in {et.RUN_PAUSED, et.RUN_RESUMED, et.RUN_STOPPED}:
            self._control(state, event)
        elif event.event_type in {et.RESOURCE_INITIALIZED, et.RESOURCE_DECIDED}:
            state.resource_state = ResourceState.model_validate(payload["resource_state"])
            if event.event_type == et.RESOURCE_DECIDED:
                decision = ResourceDecision.model_validate(payload["decision"])
                state.runtime.resources.decision = decision
                state.runtime.resources.extension_recommended = decision.extension_recommended
        elif event.event_type == et.REPAIR_LOOP_STOPPED:
            state.runtime.resources.repair_loop_stop = RepairLoopStop.model_validate(payload)
        elif event.event_type == et.FRONTIER_REPLAN_REQUESTED:
            fingerprint = str(payload["fingerprint"])
            history = state.runtime.planning.frontier_replan_fingerprints
            if fingerprint not in history:
                history.append(fingerprint)
            state.runtime.planning.frontier_replan_pending = FrontierReplanRequest.model_validate(
                payload
            )
        elif event.event_type == et.RELEASE_RECOVERY_REQUESTED:
            self._release_recovery(state, event)
        elif event.event_type == et.RUN_COMPLETED:
            self._complete(state, payload)
        elif event.event_type == et.RUN_FAILED:
            state.phase = RunPhase.FAILED
            state.stop_requested = True
            state.stop_reason = payload.get("error", "run failed")
        elif event.event_type == et.PATCH_APPLIED:
            state.runtime.completion.patch_applied = payload
        else:  # pragma: no cover
            raise ValueError(f"Runtime projector cannot handle {event.event_type}")

    @staticmethod
    def _remember_command(state: RunState, payload: dict[str, Any]) -> None:
        command_id = payload.get("command_id")
        if command_id and str(command_id) not in state.runtime.control.processed_command_ids:
            state.runtime.control.processed_command_ids.append(str(command_id))

    def _control(self, state: RunState, event: LedgerEvent) -> None:
        defaults = {
            et.RUN_PAUSED: ("paused", "operator paused"),
            et.RUN_RESUMED: ("running", "operator resumed"),
            et.RUN_STOPPED: ("stopped", "operator stopped"),
        }
        status, detail = defaults[event.event_type]
        state.runtime.control.status = status  # type: ignore[assignment]
        state.runtime.control.detail = str(event.payload.get("detail", detail))
        self._remember_command(state, event.payload)

    @staticmethod
    def _extend(state: RunState, payload: dict[str, Any]) -> None:
        state.phase = RunPhase.ACTIVE
        state.stop_requested = False
        state.stop_reason = None
        state.release = None
        state.final_artifact = None
        state.pending_action_ids = []
        state.completion_case = None
        state.semantic_regression_findings = []
        state.resource_state = None
        if state.contract is not None and payload.get("new_budget"):
            state.contract.budget = BudgetContract.model_validate(payload["new_budget"])
        verification = state.runtime.verification
        verification.semantic_ci_passed = False
        verification.semantic_ci_gaps = [
            "run extension requires fresh synthesis and release evidence"
        ]
        verification.deterministic_failures = []
        verification.adjudication = None
        state.runtime.extension.replan_pending = True
        state.runtime.completion = type(state.runtime.completion)()
        state.runtime.release.repair_completed = False
        state.runtime.release.repair_count = 0
        state.runtime.release.rejection_fingerprints = []
        state.runtime.resources.decision = None
        state.runtime.resources.extension_recommended = False
        state.runtime.resources.repair_loop_stop = None
        state.runtime.extension.count += 1
        state.runtime.extension.last_event = RunExtensionRecord.model_validate(payload)

    @staticmethod
    def _release_recovery(state: RunState, event: LedgerEvent) -> None:
        recovery = ReleaseRecovery.model_validate(event.payload["recovery"])
        route = recovery.route.value
        reopened: list[str] = []
        for obligation in state.obligations.values():
            if not obligation.release_blocking or obligation.status not in {
                ObligationStatus.SATISFIED,
                ObligationStatus.DEFERRED,
            }:
                continue
            structural = route in {"reconstruct", "reframe"}
            evidentiary = route == "reobserve" and bool(
                obligation.required_evidence_modalities
                or obligation.kind in {"verification", "claim", "coherence"}
            )
            if not (structural or evidentiary):
                continue
            obligation.status = ObligationStatus.OPEN
            obligation.evidence_references = []
            obligation.artifact_location = ""
            obligation.resolution = None
            obligation.residual_uncertainty = recovery.reason
            obligation.reopen_condition = "Fresh scoped evidence after causal recovery"
            obligation.updated_seq = event.seq
            reopened.append(obligation.obligation_id)
        state.phase = RunPhase.ACTIVE
        state.stop_requested = False
        state.stop_reason = None
        state.final_artifact = None
        state.pending_action_ids = []
        state.completion_case = None
        state.semantic_regression_findings = []
        state.runtime.verification.semantic_ci_passed = False
        state.runtime.verification.semantic_ci_gaps = [
            "release evidence invalidated an upstream commitment"
        ]
        state.runtime.release.replan_pending = recovery
        state.runtime.release.reopened_obligation_ids = reopened
        state.runtime.release.recovery_history.append(recovery)

    @staticmethod
    def _complete(state: RunState, payload: dict[str, Any]) -> None:
        state.phase = RunPhase.COMPLETE
        state.stop_requested = True
        state.stop_reason = str(payload.get("stop_reason", state.stop_reason or "")) or None
        for key in (
            "output_path",
            "deliverable_paths",
            "deterministic_checks_run",
            "deterministic_checks_passed",
            "release_required",
            "release_gate_run",
            "release_gate_succeeded",
            "release_report_releaseable",
            "release_gate_passed",
            "releaseable",
            "release_finding_count",
            "mutation_gate_passed",
            "mutation_gate_block_reason",
            "source_apply_blocked_reason",
            "apply_result",
        ):
            if key in payload:
                setattr(state.runtime.completion, key, payload[key])
        if "repair_completed" in payload:
            state.runtime.release.repair_completed = bool(payload["repair_completed"])
