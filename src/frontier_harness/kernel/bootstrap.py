"""Projection of immutable task capture and first artifact construction."""

from __future__ import annotations

from typing import Any

from .. import events as et
from ..ledger import LedgerEvent
from ..models import (
    ActionContract,
    ActionRecord,
    ActionSpec,
    ArtifactRef,
    ArtifactSpine,
    Crux,
    DiscoveryRecord,
    FrontierKernel,
    GoalContract,
    Issue,
    LeadSessionState,
    Obligation,
    RunPhase,
    RunState,
    SpeculativeOverlay,
    SummitLineage,
    TaskAmendment,
    TaskCharter,
    TaskSource,
    Usage,
)


class BootstrapProjector:
    event_types = frozenset(
        {
            et.TASK_SOURCE_CAPTURED,
            et.TASK_SOURCE_AMENDED,
            et.BOOTSTRAP_STARTED,
            et.BOOTSTRAP_FAILED,
            et.BOOTSTRAP_COMPLETED,
        }
    )

    def apply(self, state: RunState, event: LedgerEvent) -> None:
        payload = event.payload
        if event.event_type == et.TASK_SOURCE_CAPTURED:
            state.task_source = TaskSource.model_validate(payload["task_source"])
        elif event.event_type == et.TASK_SOURCE_AMENDED:
            if state.task_source is None:
                raise ValueError("task source amendment before source capture")
            state.task_source.amendments.append(TaskAmendment.model_validate(payload["amendment"]))
            state.runtime.control.steering_replan_pending = True
            self._remember_command(state, payload)
        elif event.event_type == et.BOOTSTRAP_STARTED:
            state.phase = RunPhase.BOOTSTRAPPING
        elif event.event_type == et.BOOTSTRAP_FAILED:
            state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
            runtime = state.runtime.bootstrap
            runtime.error = payload.get("error", "bootstrap failed")
            runtime.recovery_artifact = payload.get("recovery_artifact")
            runtime.recovery_thread_id = payload.get("provider_thread_id")
            runtime.recovery_error = payload.get("recovery_capture_error")
            state.phase = RunPhase.CREATED
        elif event.event_type == et.BOOTSTRAP_COMPLETED:
            self._complete(state, payload)
        else:  # pragma: no cover
            raise ValueError(f"Bootstrap projector cannot handle {event.event_type}")

    @staticmethod
    def _remember_command(state: RunState, payload: dict[str, Any]) -> None:
        command_id = payload.get("command_id")
        if command_id and str(command_id) not in state.runtime.control.processed_command_ids:
            state.runtime.control.processed_command_ids.append(str(command_id))

    @staticmethod
    def _complete(state: RunState, payload: dict[str, Any]) -> None:
        runtime = state.runtime.bootstrap
        runtime.error = None
        runtime.artifact_scope = payload.get("artifact_scope", "targeted")
        runtime.independent_checkpoint_required = bool(
            payload.get("independent_checkpoint_required")
        )
        runtime.recovery_artifact = None
        runtime.recovery_thread_id = None
        runtime.recovery_error = None
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
                dict.fromkeys([*state.frontier_advancing_action_ids, *kernel.source_action_ids])
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
        state.summit_reasons = [str(item) for item in payload.get("summit_reasons", [])]
        if payload.get("lead_session"):
            state.lead_session = LeadSessionState.model_validate(payload["lead_session"])
        artifact = ArtifactRef.model_validate(payload["artifact"])
        state.current_artifact = artifact
        state.artifact_history.append(artifact)
        state.issues = {
            item["issue_id"]: Issue.model_validate(item) for item in payload.get("issues", [])
        }
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
        state.usage = state.usage.plus(Usage.model_validate(payload.get("usage", {})))
        state.stop_requested = bool(payload.get("stop_requested", False))
        state.stop_reason = payload.get("stop_reason")
        state.phase = RunPhase.ACTIVE
        if frame_break := payload.get("frame_break"):
            state.runtime.planning.frame_breaks.append(frame_break)
