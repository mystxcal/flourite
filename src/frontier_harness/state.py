"""Deterministic projection of the immutable ledger into current run state."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import events as et
from .ledger import LedgerEvent
from .models import (
    ActionContract,
    ActionReceipt,
    ActionRecord,
    ActionSpec,
    ActionStatus,
    ArtifactRef,
    ArtifactSpine,
    BudgetContract,
    CandidateDelta,
    CandidateStatus,
    CompletionCase,
    Crux,
    DiscoveryRecord,
    EvidenceRecord,
    FrontierKernel,
    GoalContract,
    InstrumentSpec,
    Issue,
    LeadSessionState,
    ObjectiveMeasurement,
    Obligation,
    ObligationStatus,
    Probe,
    ReleaseOutput,
    ResourceState,
    RunPhase,
    RunState,
    SemanticRegressionFinding,
    SpeculativeOverlay,
    SubstrateEntry,
    SummitLineage,
    TaskAmendment,
    TaskCharter,
    TaskSource,
    Usage,
    WorkerEnvelope,
)


class StateReducer:
    @staticmethod
    def _remember_control_event(state: RunState, payload: dict[str, Any]) -> None:
        command_id = payload.get("command_id")
        if not command_id:
            return
        processed = state.metadata.setdefault("processed_control_ids", [])
        if command_id not in processed:
            processed.append(command_id)

    def replay(self, events: Iterable[LedgerEvent]) -> RunState:
        state: RunState | None = None
        for event in events:
            state = self.apply(state, event)
        if state is None:
            raise ValueError("Cannot reconstruct state from an empty ledger")
        return state

    def apply(self, state: RunState | None, event: LedgerEvent) -> RunState:
        payload = event.payload
        if event.event_type == et.RUN_CREATED:
            if state is not None:
                raise ValueError("Duplicate run.created event")
            state = RunState(
                run_id=event.run_id,
                created_at=payload["created_at"],
                source_prompt=payload["source_prompt"],
                adapter=payload.get("adapter", "generic"),
                workspace=payload.get("workspace"),
                metadata=payload.get("metadata", {}),
            )
        elif state is None:
            raise ValueError(f"First event must be {et.RUN_CREATED}")
        elif event.event_type == et.TASK_SOURCE_CAPTURED:
            state.task_source = TaskSource.model_validate(payload["task_source"])
        elif event.event_type == et.TASK_SOURCE_AMENDED:
            if state.task_source is None:
                raise ValueError("task source amendment before source capture")
            amendment = payload["amendment"]
            state.task_source.amendments.append(TaskAmendment.model_validate(amendment))
            state.metadata["steering_replan_pending"] = True
            self._remember_control_event(state, payload)
        elif event.event_type == et.BOOTSTRAP_STARTED:
            state.phase = RunPhase.BOOTSTRAPPING
        elif event.event_type == et.BOOTSTRAP_FAILED:
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            state.metadata["bootstrap_error"] = payload.get("error", "bootstrap failed")
            if payload.get("recovery_artifact"):
                state.metadata["bootstrap_recovery_artifact"] = payload["recovery_artifact"]
            if payload.get("provider_thread_id"):
                state.metadata["bootstrap_recovery_thread_id"] = payload["provider_thread_id"]
            if payload.get("recovery_capture_error"):
                state.metadata["bootstrap_recovery_error"] = payload["recovery_capture_error"]
            state.phase = RunPhase.CREATED
        elif event.event_type == et.BOOTSTRAP_COMPLETED:
            state.metadata.pop("bootstrap_error", None)
            state.metadata["bootstrap_artifact_scope"] = payload.get(
                "artifact_scope", "targeted"
            )
            if payload.get("independent_checkpoint_required"):
                state.metadata["bootstrap_independent_checkpoint_required"] = True
            state.metadata.pop("bootstrap_recovery_artifact", None)
            state.metadata.pop("bootstrap_recovery_thread_id", None)
            state.metadata.pop("bootstrap_recovery_error", None)
            state.contract = GoalContract.model_validate(payload["contract"])
            if payload.get("task_charter"):
                charter = TaskCharter.model_validate(payload["task_charter"])
                state.task_charter = charter
                state.charter_history.append(charter)
            if payload.get("artifact_spine"):
                state.artifact_spine = ArtifactSpine.model_validate(payload["artifact_spine"])
            if payload.get("frontier_kernel"):
                kernel = FrontierKernel.model_validate(payload["frontier_kernel"])
                state.frontier_kernel = kernel
                state.frontier_advancing_action_ids = list(
                    dict.fromkeys(
                        [*state.frontier_advancing_action_ids, *kernel.source_action_ids]
                    )
                )
            state.obligations = {
                item["obligation_id"]: Obligation.model_validate(item)
                for item in payload.get("obligations", [])
            }
            state.cruxes = {
                item["crux_id"]: Crux.model_validate(item) for item in payload.get("cruxes", [])
            }
            state.overlays = {
                item["overlay_id"]: SpeculativeOverlay.model_validate(item)
                for item in payload.get("overlays", [])
            }
            state.summit_lineages = {
                item["lineage_id"]: SummitLineage.model_validate(item)
                for item in payload.get("lineages", [])
            }
            state.discovery_records = {
                item["lineage_id"]: DiscoveryRecord.model_validate(item)
                for item in payload.get("discovery_records", [])
            }
            state.summit_active = bool(payload.get("summit_active", False))
            state.summit_reasons = list(payload.get("summit_reasons", []))
            if payload.get("lead_session"):
                state.lead_session = LeadSessionState.model_validate(payload["lead_session"])
            artifact = ArtifactRef.model_validate(payload["artifact"])
            state.current_artifact = artifact
            state.artifact_history.append(artifact)
            state.issues = {
                item["issue_id"]: Issue.model_validate(item) for item in payload.get("issues", [])
            }
            actions = [ActionSpec.model_validate(item) for item in payload.get("actions", [])]
            action_contracts = {
                item["action_id"]: ActionContract.model_validate(item)
                for item in payload.get("action_contracts", [])
                if item.get("action_id")
            }
            for action in actions:
                state.actions[action.action_id] = ActionRecord(
                    spec=action, contract=action_contracts.get(action.action_id)
                )
            state.pending_action_ids = [action.action_id for action in actions]
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            state.stop_requested = bool(payload.get("stop_requested", False))
            state.stop_reason = payload.get("stop_reason")
            state.phase = RunPhase.ACTIVE
            state.metadata.pop("bootstrap_error", None)
            if payload.get("frame_break"):
                state.metadata.setdefault("frame_breaks", []).append(payload["frame_break"])
        elif event.event_type == et.ACTION_SELECTED:
            selected = payload.get("selected", {})
            dominated = payload.get("dominated", {})
            deferred = payload.get("deferred", {})
            for action_id, reason in selected.items():
                record = state.actions[action_id]
                record.status = ActionStatus.SELECTED
                record.selected_reason = reason
            for action_id, reason in dominated.items():
                record = state.actions[action_id]
                record.status = ActionStatus.DOMINATED
                record.rejection_reason = reason
            for action_id, reason in deferred.items():
                record = state.actions[action_id]
                record.status = ActionStatus.DEFERRED
                record.rejection_reason = reason
            state.pending_action_ids = list(selected)
        elif event.event_type == et.ACTION_STARTED:
            action_id = event.action_id or payload["action_id"]
            record = state.actions[action_id]
            record.status = ActionStatus.RUNNING
            record.started_at = payload["started_at"]
        elif event.event_type == et.ACTION_COMPLETED:
            action_id = event.action_id or payload["action_id"]
            record = state.actions[action_id]
            record.status = ActionStatus.COMPLETE
            record.result = WorkerEnvelope.model_validate(payload["result"])
            if payload.get("action_receipt"):
                receipt = ActionReceipt.model_validate(payload["action_receipt"])
                record.receipt = receipt
                state.action_receipts[receipt.action_id] = receipt
            if payload.get("objective_measurement"):
                record.objective_measurement = ObjectiveMeasurement.model_validate(
                    payload["objective_measurement"]
                )
            if payload.get("baseline_objective_measurement"):
                record.baseline_objective_measurement = ObjectiveMeasurement.model_validate(
                    payload["baseline_objective_measurement"]
                )
            if payload.get("result_blob"):
                from .models import BlobRef

                record.result_blob = BlobRef.model_validate(payload["result_blob"])
            if payload.get("raw_events_blob"):
                from .models import BlobRef

                record.raw_events_blob = BlobRef.model_validate(payload["raw_events_blob"])
            if payload.get("patch_blob"):
                from .models import BlobRef

                record.patch_blob = BlobRef.model_validate(payload["patch_blob"])
            record.completed_at = payload["completed_at"]
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            for evidence_item in payload.get("evidence", []):
                evidence = EvidenceRecord.model_validate(evidence_item)
                state.evidence[evidence.evidence_id] = evidence
            if payload.get("candidate_delta"):
                delta = CandidateDelta.model_validate(payload["candidate_delta"])
                state.candidate_deltas[delta.delta_id] = delta
            if payload.get("probe"):
                probe = Probe.model_validate(payload["probe"])
                state.probes[probe.probe_id] = probe
            for item in payload.get("substrate_entries", []):
                entry = SubstrateEntry.model_validate(item)
                state.substrate[entry.entry_id] = entry
            if payload.get("instrument"):
                instrument = InstrumentSpec.model_validate(payload["instrument"])
                state.instruments[instrument.instrument_id] = instrument
            if payload.get("overlay"):
                overlay = SpeculativeOverlay.model_validate(payload["overlay"])
                state.overlays[overlay.overlay_id] = overlay
            for item in payload.get("overlays", []):
                overlay = SpeculativeOverlay.model_validate(item)
                state.overlays[overlay.overlay_id] = overlay
            if payload.get("lineage"):
                lineage = SummitLineage.model_validate(payload["lineage"])
                state.summit_lineages[lineage.lineage_id] = lineage
            for item in payload.get("lineages", []):
                lineage = SummitLineage.model_validate(item)
                state.summit_lineages[lineage.lineage_id] = lineage
            for item in payload.get("discovery_records", []):
                discovery_record = DiscoveryRecord.model_validate(item)
                state.discovery_records[discovery_record.lineage_id] = discovery_record
            if payload.get("lead_session"):
                state.lead_session = LeadSessionState.model_validate(payload["lead_session"])
        elif event.event_type == et.ACTION_FAILED:
            action_id = event.action_id or payload["action_id"]
            record = state.actions[action_id]
            record.status = ActionStatus.FAILED
            record.error = payload["error"]
            record.completed_at = payload.get("completed_at")
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            if payload.get("action_receipt"):
                receipt = ActionReceipt.model_validate(payload["action_receipt"])
                record.receipt = receipt
                state.action_receipts[receipt.action_id] = receipt
            if payload.get("baseline_objective_measurement"):
                record.baseline_objective_measurement = ObjectiveMeasurement.model_validate(
                    payload["baseline_objective_measurement"]
                )
            for item in payload.get("discovery_records", []):
                discovery_record = DiscoveryRecord.model_validate(item)
                state.discovery_records[discovery_record.lineage_id] = discovery_record
        elif event.event_type == et.CHECKPOINT_STARTED:
            state.phase = RunPhase.ACTIVE
        elif event.event_type == et.CHECKPOINT_COMPLETED:
            artifact = ArtifactRef.model_validate(payload["artifact"])
            state.current_artifact = artifact
            state.artifact_history.append(artifact)
            for issue_item in payload.get("issue_upserts", []):
                issue = Issue.model_validate(issue_item)
                state.issues[issue.issue_id] = issue
            for item in payload.get("obligation_upserts", []):
                obligation = Obligation.model_validate(item)
                state.obligations[obligation.obligation_id] = obligation
            for item in payload.get("crux_upserts", []):
                crux = Crux.model_validate(item)
                state.cruxes[crux.crux_id] = crux
            for item in payload.get("substrate_entries", []):
                entry = SubstrateEntry.model_validate(item)
                state.substrate[entry.entry_id] = entry
            for item in payload.get("overlays", []):
                overlay = SpeculativeOverlay.model_validate(item)
                state.overlays[overlay.overlay_id] = overlay
            for item in payload.get("lineages", []):
                lineage = SummitLineage.model_validate(item)
                state.summit_lineages[lineage.lineage_id] = lineage
            for item in payload.get("discovery_records", []):
                discovery_record = DiscoveryRecord.model_validate(item)
                state.discovery_records[discovery_record.lineage_id] = discovery_record
            if payload.get("artifact_spine"):
                state.artifact_spine = ArtifactSpine.model_validate(payload["artifact_spine"])
            if payload.get("frontier_kernel"):
                kernel = FrontierKernel.model_validate(payload["frontier_kernel"])
                state.frontier_kernel = kernel
                state.frontier_advancing_action_ids = list(
                    dict.fromkeys(
                        [*state.frontier_advancing_action_ids, *kernel.source_action_ids]
                    )
                )
            if payload.get("task_charter"):
                charter = TaskCharter.model_validate(payload["task_charter"])
                state.task_charter = charter
                state.charter_history.append(charter)
            state.summit_active = bool(payload.get("summit_active", state.summit_active))
            state.summit_reasons = list(payload.get("summit_reasons", state.summit_reasons))
            if payload.get("lead_session"):
                state.lead_session = LeadSessionState.model_validate(payload["lead_session"])
            for action_id in payload.get("accepted_action_ids", []):
                if action_id in state.actions:
                    state.actions[action_id].rejection_reason = None
                for delta in state.candidate_deltas.values():
                    if delta.source_action_id == action_id:
                        delta.status = CandidateStatus.ACCEPTED
            for action_id in payload.get("rejected_action_ids", []):
                if action_id in state.actions:
                    state.actions[action_id].rejection_reason = "rejected at checkpoint"
                for delta in state.candidate_deltas.values():
                    if delta.source_action_id == action_id:
                        delta.status = CandidateStatus.REJECTED
            for item in payload.get("receipt_updates", []):
                receipt = ActionReceipt.model_validate(item)
                state.action_receipts[receipt.action_id] = receipt
                if receipt.action_id in state.actions:
                    state.actions[receipt.action_id].receipt = receipt
            actions = [ActionSpec.model_validate(item) for item in payload.get("actions", [])]
            action_contracts = {
                item["action_id"]: ActionContract.model_validate(item)
                for item in payload.get("action_contracts", [])
                if item.get("action_id")
            }
            for action in actions:
                state.actions[action.action_id] = ActionRecord(
                    spec=action, contract=action_contracts.get(action.action_id)
                )
            state.pending_action_ids = [action.action_id for action in actions]
            state.stop_requested = bool(payload.get("stop_requested", False))
            state.stop_reason = payload.get("stop_reason")
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            if payload.get("completed_round_index") is not None:
                state.round_index = int(payload["completed_round_index"])
            if payload.get("frame_break"):
                state.metadata.setdefault("frame_breaks", []).append(payload["frame_break"])
            state.metadata["clean_synthesis_needed"] = bool(
                payload.get("clean_synthesis_needed", False)
            )
            state.metadata.pop("checkpoint_error", None)
            state.metadata.pop("extension_replan_pending", None)
            state.metadata.pop("steering_replan_pending", None)
            state.metadata.pop("bootstrap_independent_checkpoint_required", None)
            state.metadata.pop("release_replan_pending", None)
            state.metadata.pop("frontier_replan_pending", None)
        elif event.event_type == et.CHECKPOINT_FAILED:
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            state.metadata["checkpoint_error"] = payload.get("error", "checkpoint failed")
        elif event.event_type == et.ROUND_COMPLETED:
            state.round_index = int(payload["round_index"])
        elif event.event_type == et.FINALIZATION_STARTED:
            state.phase = RunPhase.FINALIZING
        elif event.event_type == et.FINAL_SYNTHESIZED:
            artifact = ArtifactRef.model_validate(payload["artifact"])
            state.current_artifact = artifact
            state.final_artifact = artifact
            state.artifact_history.append(artifact)
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            state.metadata["remaining_uncertainty"] = payload.get("remaining_uncertainty", [])
            state.metadata["release_gate_recommended"] = bool(
                payload.get("release_gate_recommended", True)
            )
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
            state.metadata.pop("finalization_error", None)
        elif event.event_type == et.FINALIZATION_FAILED:
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            state.metadata["finalization_error"] = payload.get("error", "final synthesis failed")
        elif event.event_type in {et.DETERMINISTIC_CHECK_COMPLETED, et.EVIDENCE_RECORDED}:
            evidence = EvidenceRecord.model_validate(payload["evidence"])
            state.evidence[evidence.evidence_id] = evidence
        elif event.event_type == et.CHECK_STAGE_COMPLETED:
            stage = str(payload["stage"])
            failed = bool(payload.get("failed", False))
            artifact_digest = payload.get("artifact_digest")
            state.metadata[f"{stage}_check_artifact_digest"] = artifact_digest
            state.metadata[f"{stage}_check_failed"] = failed
            state.metadata[f"{stage}_check_failures"] = list(payload.get("failures", []))
            if stage in {"preflight", "candidate"}:
                state.metadata["verification_replan_pending"] = any(
                    state.metadata.get(f"{candidate}_check_artifact_digest") == artifact_digest
                    and state.metadata.get(f"{candidate}_check_failed") is True
                    for candidate in ("preflight", "candidate")
                )
                if not state.metadata["verification_replan_pending"]:
                    state.metadata.pop("verification_dead_end", None)
        elif event.event_type == et.CHECK_REPLAN_DECIDED:
            decision = str(payload["decision"])
            state.metadata["verification_replan_pending"] = False
            state.metadata["verification_replan_decision"] = decision
            if decision == "corrective_actions":
                state.metadata.pop("verification_dead_end", None)
            else:
                state.metadata["verification_dead_end"] = list(
                    payload.get("failures", [])
                )
            # A semantic stop cannot overrule failed executable acceptance.
            state.stop_requested = False
            state.stop_reason = None
        elif event.event_type == et.RELEASE_COMPLETED:
            state.release = ReleaseOutput.model_validate(payload["release"])
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            fingerprint = payload.get("rejection_fingerprint")
            if fingerprint:
                fingerprints = state.metadata.setdefault("release_rejection_fingerprints", [])
                fingerprints.append(fingerprint)
            state.metadata.pop("release_error", None)
        elif event.event_type == et.RELEASE_FAILED:
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            state.metadata["release_error"] = payload.get("error", "release challenge failed")
        elif event.event_type == et.REPAIR_COMPLETED:
            artifact = ArtifactRef.model_validate(payload["artifact"])
            state.current_artifact = artifact
            state.final_artifact = artifact
            state.artifact_history.append(artifact)
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            state.metadata["repair_completed"] = True
            state.metadata["repair_count"] = int(state.metadata.get("repair_count", 0)) + 1
            state.metadata["repair_remaining_uncertainty"] = payload.get(
                "remaining_uncertainty", []
            )
            if payload.get("artifact_spine"):
                state.artifact_spine = ArtifactSpine.model_validate(payload["artifact_spine"])
            if payload.get("completion_case"):
                state.completion_case = CompletionCase.model_validate(payload["completion_case"])
            if payload.get("lead_session"):
                state.lead_session = LeadSessionState.model_validate(payload["lead_session"])
            state.metadata.pop("repair_error", None)
        elif event.event_type == et.REPAIR_FAILED:
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            state.metadata["repair_error"] = payload.get("error", "repair failed")
        elif event.event_type == et.TASK_CHARTER_UPDATED:
            charter = TaskCharter.model_validate(payload["task_charter"])
            state.task_charter = charter
            state.charter_history.append(charter)
        elif event.event_type == et.ARTIFACT_SPINE_UPDATED:
            state.artifact_spine = ArtifactSpine.model_validate(payload["artifact_spine"])
        elif event.event_type == et.OBLIGATIONS_UPDATED:
            for item in payload.get("obligations", []):
                obligation = Obligation.model_validate(item)
                state.obligations[obligation.obligation_id] = obligation
        elif event.event_type == et.CRUXES_UPDATED:
            for item in payload.get("cruxes", []):
                crux = Crux.model_validate(item)
                state.cruxes[crux.crux_id] = crux
        elif event.event_type == et.SUBSTRATE_UPDATED:
            for item in payload.get("entries", []):
                entry = SubstrateEntry.model_validate(item)
                state.substrate[entry.entry_id] = entry
        elif event.event_type == et.OVERLAYS_UPDATED:
            for item in payload.get("overlays", []):
                overlay = SpeculativeOverlay.model_validate(item)
                state.overlays[overlay.overlay_id] = overlay
        elif event.event_type == et.INSTRUMENT_UPDATED:
            instrument = InstrumentSpec.model_validate(payload["instrument"])
            state.instruments[instrument.instrument_id] = instrument
        elif event.event_type == et.SUMMIT_ACTIVATED:
            state.summit_active = True
            state.summit_reasons = list(payload.get("reasons", []))
        elif event.event_type == et.SUMMIT_ARCHIVE_UPDATED:
            for item in payload.get("lineages", []):
                lineage = SummitLineage.model_validate(item)
                state.summit_lineages[lineage.lineage_id] = lineage
            for item in payload.get("discovery_records", []):
                discovery_record = DiscoveryRecord.model_validate(item)
                state.discovery_records[discovery_record.lineage_id] = discovery_record
        elif event.event_type in {et.LEAD_SESSION_UPDATED, et.LEAD_RECONSTRUCTION}:
            state.lead_session = LeadSessionState.model_validate(payload["lead_session"])
        elif event.event_type == et.ACTION_CONTRACTED:
            contract = ActionContract.model_validate(payload["contract"])
            if contract.action_id and contract.action_id in state.actions:
                state.actions[contract.action_id].contract = contract
        elif event.event_type == et.ACTION_RECEIPTED:
            receipt = ActionReceipt.model_validate(payload["receipt"])
            state.action_receipts[receipt.action_id] = receipt
            if receipt.action_id in state.actions:
                state.actions[receipt.action_id].receipt = receipt
        elif event.event_type == et.SEMANTIC_REGRESSION_COMPLETED:
            state.semantic_regression_findings = [
                SemanticRegressionFinding.model_validate(item)
                for item in payload.get("findings", [])
            ]
            state.metadata["semantic_ci_passed"] = bool(payload.get("passed", False))
            state.metadata["semantic_ci_gaps"] = list(payload.get("completion_gaps", []))
            if "deterministic_failures" in payload:
                state.metadata["semantic_ci_deterministic_failures"] = list(
                    payload.get("deterministic_failures", [])
                )
            if payload.get("adjudication"):
                state.metadata["semantic_ci_adjudication"] = payload["adjudication"]
        elif event.event_type == et.COMPLETION_CASE_BUILT:
            state.completion_case = CompletionCase.model_validate(payload["completion_case"])
        elif event.event_type == et.RUN_EXTENDED:
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
            state.metadata["semantic_ci_passed"] = False
            state.metadata["semantic_ci_gaps"] = [
                "run extension requires fresh synthesis and release evidence"
            ]
            state.metadata.pop("semantic_ci_deterministic_failures", None)
            state.metadata.pop("semantic_ci_adjudication", None)
            state.metadata["extension_replan_pending"] = True
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
                "repair_completed",
                "repair_count",
                "release_rejection_fingerprints",
                "mutation_gate_passed",
                "mutation_gate_block_reason",
                "source_apply_blocked_reason",
                "apply_result",
            ):
                state.metadata.pop(key, None)
            state.metadata.pop("resource_decision", None)
            state.metadata.pop("resource_extension_recommended", None)
            state.metadata.pop("repair_loop_stop", None)
            state.metadata["extension_count"] = int(state.metadata.get("extension_count", 0)) + 1
            state.metadata["extension"] = payload
        elif event.event_type == et.RUN_PAUSED:
            state.metadata["control_status"] = "paused"
            state.metadata["control_detail"] = payload.get("detail", "operator paused")
            self._remember_control_event(state, payload)
        elif event.event_type == et.RUN_RESUMED:
            state.metadata["control_status"] = "running"
            state.metadata["control_detail"] = payload.get("detail", "operator resumed")
            self._remember_control_event(state, payload)
        elif event.event_type == et.RUN_STOPPED:
            state.metadata["control_status"] = "stopped"
            state.metadata["control_detail"] = payload.get("detail", "operator stopped")
            self._remember_control_event(state, payload)
        elif event.event_type in {et.RESOURCE_INITIALIZED, et.RESOURCE_DECIDED}:
            state.resource_state = ResourceState.model_validate(payload["resource_state"])
            if event.event_type == et.RESOURCE_DECIDED:
                state.metadata["resource_decision"] = payload.get("decision", {})
                if payload.get("decision", {}).get("extension_recommended"):
                    state.metadata["resource_extension_recommended"] = True
                else:
                    state.metadata.pop("resource_extension_recommended", None)
        elif event.event_type == et.REPAIR_LOOP_STOPPED:
            state.metadata["repair_loop_stop"] = payload
        elif event.event_type == et.FRONTIER_REPLAN_REQUESTED:
            fingerprint = str(payload["fingerprint"])
            history = state.metadata.setdefault("frontier_replan_fingerprints", [])
            if fingerprint not in history:
                history.append(fingerprint)
            state.metadata["frontier_replan_pending"] = payload
        elif event.event_type == et.RELEASE_RECOVERY_REQUESTED:
            recovery = payload["recovery"]
            route = str(recovery["route"])
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
                obligation.residual_uncertainty = str(recovery["reason"])
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
            state.metadata["semantic_ci_passed"] = False
            state.metadata["semantic_ci_gaps"] = [
                "release evidence invalidated an upstream commitment"
            ]
            state.metadata["release_replan_pending"] = recovery
            state.metadata["release_reopened_obligations"] = reopened
            history = state.metadata.setdefault("release_recovery_history", [])
            history.append(recovery)
        elif event.event_type == et.RUN_COMPLETED:
            state.phase = RunPhase.COMPLETE
            state.stop_requested = True
            state.stop_reason = payload.get("stop_reason", state.stop_reason)
            completion_keys = (
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
                "repair_completed",
                "mutation_gate_passed",
                "mutation_gate_block_reason",
                "source_apply_blocked_reason",
                "apply_result",
            )
            for key in completion_keys:
                if key in payload:
                    state.metadata[key] = payload[key]
        elif event.event_type == et.RUN_FAILED:
            state.phase = RunPhase.FAILED
            state.stop_requested = True
            state.stop_reason = payload.get("error", "run failed")
        elif event.event_type == et.PATCH_APPLIED:
            state.metadata["patch_applied"] = payload

        state.last_event_seq = event.seq
        state.last_event_hash = event.event_hash
        return state


def state_summary(state: RunState) -> dict[str, Any]:
    """Compact semantic state for controller capsules and CLI status."""

    recent_records = list(state.actions.values())[-12:]
    return {
        "run_id": state.run_id,
        "phase": state.phase.value,
        "round_index": state.round_index,
        "artifact": state.current_artifact.model_dump(mode="json")
        if state.current_artifact
        else None,
        "open_issues": [issue.model_dump(mode="json") for issue in state.open_issues],
        "recent_actions": [
            {
                "action_id": record.spec.action_id,
                "kind": record.spec.kind.value,
                "target": record.spec.target,
                "issue_ids": record.spec.issue_ids,
                "status": record.status.value,
                "findings": record.result.findings if record.result else [],
                "unresolved_risks": record.result.unresolved_risks if record.result else [],
                "error": record.error,
                "receipt": record.receipt.model_dump(mode="json") if record.receipt else None,
                "objective_measurement": (
                    record.objective_measurement.model_dump(mode="json")
                    if record.objective_measurement
                    else None
                ),
                "baseline_objective_measurement": (
                    record.baseline_objective_measurement.model_dump(mode="json")
                    if record.baseline_objective_measurement
                    else None
                ),
            }
            for record in recent_records
        ],
        "active_candidate_deltas": [
            item.model_dump(mode="json")
            for item in state.candidate_deltas.values()
            if item.status.value == "proposed"
        ],
        "recent_probes": [
            item.model_dump(mode="json") for item in list(state.probes.values())[-8:]
        ],
        "pending_action_ids": state.pending_action_ids,
        "verification": {
            "replan_pending": bool(state.metadata.get("verification_replan_pending")),
            "preflight_failures": list(state.metadata.get("preflight_check_failures", [])),
            "candidate_failures": list(state.metadata.get("candidate_check_failures", [])),
        },
        "task_source_digest": state.task_source.digest if state.task_source else None,
        "task_charter": state.task_charter.model_dump(mode="json") if state.task_charter else None,
        "artifact_spine": state.artifact_spine.model_dump(mode="json")
        if state.artifact_spine
        else None,
        "frontier_kernel": state.frontier_kernel.model_dump(mode="json")
        if state.frontier_kernel
        else None,
        "open_obligations": [item.model_dump(mode="json") for item in state.open_obligations],
        "active_cruxes": [item.model_dump(mode="json") for item in state.active_cruxes],
        "active_overlays": [
            item.model_dump(mode="json")
            for item in state.overlays.values()
            if item.status.value in {"proposed", "active"}
        ],
        "summit_active": state.summit_active,
        "summit_reasons": state.summit_reasons,
        "summit_lineages": [
            item.model_dump(mode="json") for item in state.summit_lineages.values()
        ],
        "discovery_records": [
            item.model_dump(mode="json") for item in state.discovery_records.values()
        ],
        "lead_session": state.lead_session.model_dump(mode="json"),
        "completion_case": state.completion_case.model_dump(mode="json")
        if state.completion_case
        else None,
        "usage": state.usage.model_dump(mode="json"),
        "resource_state": (
            state.resource_state.model_dump(mode="json") if state.resource_state else None
        ),
        "stop_requested": state.stop_requested,
        "stop_reason": state.stop_reason,
    }
