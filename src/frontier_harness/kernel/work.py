"""Projection of action execution and Frontier Keeper integration."""

from __future__ import annotations

from typing import Any

from .. import events as et
from ..ledger import LedgerEvent
from ..models import (
    ActionAttempt,
    ActionAttemptStatus,
    ActionContract,
    ActionReceipt,
    ActionRecord,
    ActionSpec,
    ActionStatus,
    ArtifactRef,
    ArtifactSpine,
    BlobRef,
    CandidateDelta,
    CandidateStatus,
    Crux,
    DiscoveryRecord,
    EvidenceRecord,
    FrontierKernel,
    InstrumentSpec,
    Issue,
    LeadSessionState,
    ObjectiveMeasurement,
    Obligation,
    Probe,
    RunPhase,
    RunState,
    SpeculativeOverlay,
    SubstrateEntry,
    SummitLineage,
    TaskCharter,
    Usage,
    WorkerEnvelope,
)


class WorkProjector:
    event_types = frozenset(
        {
            et.ACTION_SELECTED,
            et.ACTION_STARTED,
            et.ACTION_ATTEMPT_STARTED,
            et.ACTION_ATTEMPT_FINISHED,
            et.ACTION_COMPLETED,
            et.ACTION_FAILED,
            et.CHECKPOINT_STARTED,
            et.CHECKPOINT_COMPLETED,
            et.CHECKPOINT_FAILED,
            et.ROUND_COMPLETED,
        }
    )

    def apply(self, state: RunState, event: LedgerEvent) -> None:
        if event.event_type == et.ACTION_SELECTED:
            self._selected(state, event.payload)
        elif event.event_type == et.ACTION_STARTED:
            action_id = event.action_id or event.payload["action_id"]
            record = state.actions[action_id]
            record.status = ActionStatus.RUNNING
            record.started_at = event.payload["started_at"]
        elif event.event_type == et.ACTION_ATTEMPT_STARTED:
            self._attempt_started(state, event)
        elif event.event_type == et.ACTION_ATTEMPT_FINISHED:
            self._attempt_finished(state, event)
        elif event.event_type == et.ACTION_COMPLETED:
            self._completed(state, event)
        elif event.event_type == et.ACTION_FAILED:
            self._failed(state, event)
        elif event.event_type == et.CHECKPOINT_STARTED:
            state.phase = RunPhase.ACTIVE
        elif event.event_type == et.CHECKPOINT_COMPLETED:
            self._checkpoint(state, event.payload)
        elif event.event_type == et.CHECKPOINT_FAILED:
            state.usage = state.usage.plus(Usage.model_validate(event.payload.get("usage", {})))
            state.runtime.planning.checkpoint_error = event.payload.get(
                "error", "checkpoint failed"
            )
        elif event.event_type == et.ROUND_COMPLETED:
            state.round_index = int(event.payload["round_index"])
        else:  # pragma: no cover
            raise ValueError(f"Work projector cannot handle {event.event_type}")

    @staticmethod
    def _attempt_started(state: RunState, event: LedgerEvent) -> None:
        payload = event.payload
        action_id = event.action_id or payload["action_id"]
        record = state.actions[action_id]
        record.status = ActionStatus.RUNNING
        record.started_at = payload["started_at"]
        record.attempts.append(
            ActionAttempt(
                attempt_id=payload["attempt_id"],
                action_id=action_id,
                status=ActionAttemptStatus.RUNNING,
                started_at=payload["started_at"],
            )
        )

    @staticmethod
    def _attempt_finished(state: RunState, event: LedgerEvent) -> None:
        payload = event.payload
        action_id = event.action_id or payload["action_id"]
        attempt_id = payload["attempt_id"]
        record = state.actions[action_id]
        attempt = next(
            (item for item in reversed(record.attempts) if item.attempt_id == attempt_id),
            None,
        )
        if attempt is None:
            raise ValueError(f"attempt finish without start: {attempt_id}")
        attempt.status = ActionAttemptStatus(payload["status"])
        attempt.completed_at = payload["completed_at"]
        attempt.error = payload.get("error")
        attempt.usage = Usage.model_validate(payload.get("usage", {}))
        for field in (
            "result_blob",
            "context_lens_blob",
            "boundary_blob",
            "raw_events_blob",
        ):
            if payload.get(field):
                setattr(attempt, field, BlobRef.model_validate(payload[field]))

    @staticmethod
    def _selected(state: RunState, payload: dict[str, Any]) -> None:
        for action_id, reason in payload.get("selected", {}).items():
            record = state.actions[action_id]
            record.status = ActionStatus.SELECTED
            record.selected_reason = reason
        for action_id, reason in payload.get("dominated", {}).items():
            record = state.actions[action_id]
            record.status = ActionStatus.DOMINATED
            record.rejection_reason = reason
        for action_id, reason in payload.get("deferred", {}).items():
            record = state.actions[action_id]
            record.status = ActionStatus.DEFERRED
            record.rejection_reason = reason
        state.pending_action_ids = list(payload.get("selected", {}))

    @staticmethod
    def _completed(state: RunState, event: LedgerEvent) -> None:
        payload = event.payload
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
        for field in ("result_blob", "raw_events_blob", "patch_blob"):
            if payload.get(field):
                setattr(record, field, BlobRef.model_validate(payload[field]))
        record.completed_at = payload["completed_at"]
        state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
        for item in payload.get("evidence", []):
            evidence = EvidenceRecord.model_validate(item)
            state.evidence[evidence.evidence_id] = evidence
        if payload.get("candidate_delta"):
            delta = CandidateDelta.model_validate(payload["candidate_delta"])
            state.candidate_deltas[delta.delta_id] = delta
        if payload.get("probe"):
            probe = Probe.model_validate(payload["probe"])
            state.probes[probe.probe_id] = probe
        WorkProjector._upsert_semantic_payload(state, payload)

    @staticmethod
    def _failed(state: RunState, event: LedgerEvent) -> None:
        payload = event.payload
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
            discovery = DiscoveryRecord.model_validate(item)
            state.discovery_records[discovery.lineage_id] = discovery

    @staticmethod
    def _upsert_semantic_payload(state: RunState, payload: dict[str, Any]) -> None:
        for item in payload.get("substrate_entries", []):
            entry = SubstrateEntry.model_validate(item)
            state.substrate[entry.entry_id] = entry
        if payload.get("instrument"):
            instrument = InstrumentSpec.model_validate(payload["instrument"])
            state.instruments[instrument.instrument_id] = instrument
        overlays = [
            *([payload["overlay"]] if payload.get("overlay") else []),
            *payload.get("overlays", []),
        ]
        for item in overlays:
            overlay = SpeculativeOverlay.model_validate(item)
            state.overlays[overlay.overlay_id] = overlay
        lineages = [
            *([payload["lineage"]] if payload.get("lineage") else []),
            *payload.get("lineages", []),
        ]
        for item in lineages:
            lineage = SummitLineage.model_validate(item)
            state.summit_lineages[lineage.lineage_id] = lineage
        for item in payload.get("discovery_records", []):
            discovery = DiscoveryRecord.model_validate(item)
            state.discovery_records[discovery.lineage_id] = discovery
        if payload.get("lead_session"):
            state.lead_session = LeadSessionState.model_validate(payload["lead_session"])

    @staticmethod
    def _checkpoint(state: RunState, payload: dict[str, Any]) -> None:
        artifact = ArtifactRef.model_validate(payload["artifact"])
        state.current_artifact = artifact
        state.artifact_history.append(artifact)
        WorkProjector._checkpoint_upserts(state, payload)
        WorkProjector._checkpoint_decisions(state, payload)
        actions = [ActionSpec.model_validate(item) for item in payload.get("actions", [])]
        contracts = {
            item["action_id"]: ActionContract.model_validate(item)
            for item in payload.get("action_contracts", [])
            if item.get("action_id")
        }
        for action in actions:
            state.actions[action.action_id] = ActionRecord(
                spec=action,
                contract=contracts.get(action.action_id),
            )
        state.pending_action_ids = [action.action_id for action in actions]
        state.stop_requested = bool(payload.get("stop_requested", False))
        state.stop_reason = payload.get("stop_reason")
        state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
        if payload.get("completed_round_index") is not None:
            state.round_index = int(payload["completed_round_index"])
        if frame_break := payload.get("frame_break"):
            state.runtime.planning.frame_breaks.append(frame_break)
        state.runtime.planning.clean_synthesis_needed = bool(
            payload.get("clean_synthesis_needed", False)
        )
        state.runtime.planning.checkpoint_error = None
        state.runtime.extension.replan_pending = False
        state.runtime.control.steering_replan_pending = False
        state.runtime.bootstrap.independent_checkpoint_required = False
        state.runtime.release.replan_pending = None
        state.runtime.planning.frontier_replan_pending = None

    @staticmethod
    def _checkpoint_upserts(state: RunState, payload: dict[str, Any]) -> None:
        for raw in payload.get("issue_upserts", []):
            issue = Issue.model_validate(raw)
            state.issues[issue.issue_id] = issue
        for raw in payload.get("obligation_upserts", []):
            obligation = Obligation.model_validate(raw)
            state.obligations[obligation.obligation_id] = obligation
        for raw in payload.get("crux_upserts", []):
            crux = Crux.model_validate(raw)
            state.cruxes[crux.crux_id] = crux
        for raw in payload.get("substrate_entries", []):
            entry = SubstrateEntry.model_validate(raw)
            state.substrate[entry.entry_id] = entry
        for raw in payload.get("overlays", []):
            overlay = SpeculativeOverlay.model_validate(raw)
            state.overlays[overlay.overlay_id] = overlay
        for raw in payload.get("lineages", []):
            lineage = SummitLineage.model_validate(raw)
            state.summit_lineages[lineage.lineage_id] = lineage
        for raw in payload.get("discovery_records", []):
            discovery = DiscoveryRecord.model_validate(raw)
            state.discovery_records[discovery.lineage_id] = discovery
        if payload.get("artifact_spine"):
            state.artifact_spine = ArtifactSpine.model_validate(payload["artifact_spine"])
        if payload.get("frontier_kernel"):
            kernel = FrontierKernel.model_validate(payload["frontier_kernel"])
            state.frontier_kernel = kernel
            state.frontier_advancing_action_ids = list(
                dict.fromkeys([*state.frontier_advancing_action_ids, *kernel.source_action_ids])
            )
        if payload.get("task_charter"):
            charter = TaskCharter.model_validate(payload["task_charter"])
            state.task_charter = charter
            state.charter_history.append(charter)
        state.summit_active = bool(payload.get("summit_active", state.summit_active))
        state.summit_reasons = list(payload.get("summit_reasons", state.summit_reasons))
        if payload.get("lead_session"):
            state.lead_session = LeadSessionState.model_validate(payload["lead_session"])

    @staticmethod
    def _checkpoint_decisions(state: RunState, payload: dict[str, Any]) -> None:
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
