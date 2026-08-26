"""Deterministic projection of the immutable ledger into current run state."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar

from . import events as et
from .kernel import PROJECTORS, EventProjector
from .ledger import LedgerEvent
from .models import (
    CheckStageState,
    RunState,
)


class StateReducer:
    _PROJECTORS_BY_EVENT: ClassVar[dict[str, EventProjector]] = {
        event_type: projector for projector in PROJECTORS for event_type in projector.event_types
    }
    _LEGACY_RUNTIME_KEYS = frozenset(
        {
            "processed_control_ids",
            "steering_replan_pending",
            "bootstrap_error",
            "bootstrap_artifact_scope",
            "bootstrap_independent_checkpoint_required",
            "bootstrap_recovery_artifact",
            "bootstrap_recovery_thread_id",
            "bootstrap_recovery_error",
            "frame_breaks",
            "clean_synthesis_needed",
            "checkpoint_error",
            "extension_replan_pending",
            "release_replan_pending",
            "frontier_replan_pending",
            "frontier_replan_fingerprints",
            "remaining_uncertainty",
            "release_gate_recommended",
            "finalization_error",
            "verification_replan_pending",
            "verification_replan_decision",
            "verification_dead_end",
            "release_rejection_fingerprints",
            "release_error",
            "repair_completed",
            "repair_count",
            "repair_remaining_uncertainty",
            "repair_error",
            "semantic_ci_passed",
            "semantic_ci_gaps",
            "semantic_ci_deterministic_failures",
            "semantic_ci_adjudication",
            "extension_count",
            "extension",
            "control_status",
            "control_detail",
            "resource_decision",
            "resource_extension_recommended",
            "repair_loop_stop",
            "release_reopened_obligations",
            "release_recovery_history",
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
            "patch_applied",
        }
    )

    @classmethod
    def _sync_legacy_metadata(cls, state: RunState) -> None:
        """Maintain a derived read mirror while callers migrate to typed state."""

        for key in cls._LEGACY_RUNTIME_KEYS:
            state.metadata.pop(key, None)
        for stage in state.runtime.verification.stages:
            for suffix in ("artifact_digest", "failed", "failures"):
                state.metadata.pop(f"{stage}_check_{suffix}", None)

        bootstrap = state.runtime.bootstrap
        control = state.runtime.control
        verification = state.runtime.verification
        planning = state.runtime.planning
        release = state.runtime.release
        resources = state.runtime.resources
        extension = state.runtime.extension
        completion = state.runtime.completion

        state.metadata.update(
            {
                "bootstrap_artifact_scope": bootstrap.artifact_scope,
                "processed_control_ids": list(control.processed_command_ids),
                "control_status": control.status,
                "control_detail": control.detail,
                "verification_replan_pending": verification.replan_pending,
                "semantic_ci_gaps": list(verification.semantic_ci_gaps),
                "frontier_replan_fingerprints": list(planning.frontier_replan_fingerprints),
                "clean_synthesis_needed": planning.clean_synthesis_needed,
                "remaining_uncertainty": list(release.remaining_uncertainty),
                "release_gate_recommended": release.gate_recommended,
                "repair_count": release.repair_count,
                "repair_completed": release.repair_completed,
                "release_rejection_fingerprints": list(release.rejection_fingerprints),
                "release_reopened_obligations": list(release.reopened_obligation_ids),
                "release_recovery_history": [
                    item.model_dump(mode="json") for item in release.recovery_history
                ],
                "extension_count": extension.count,
            }
        )
        optional: dict[str, Any] = {
            "bootstrap_error": bootstrap.error,
            "bootstrap_recovery_artifact": (
                bootstrap.recovery_artifact.model_dump(mode="json")
                if bootstrap.recovery_artifact
                else None
            ),
            "bootstrap_recovery_thread_id": bootstrap.recovery_thread_id,
            "bootstrap_recovery_error": bootstrap.recovery_error,
            "checkpoint_error": planning.checkpoint_error,
            "frontier_replan_pending": (
                planning.frontier_replan_pending.model_dump(mode="json")
                if planning.frontier_replan_pending
                else None
            ),
            "verification_replan_decision": verification.replan_decision,
            "semantic_ci_passed": verification.semantic_ci_passed,
            "semantic_ci_adjudication": (
                verification.adjudication.model_dump(mode="json")
                if verification.adjudication
                else None
            ),
            "finalization_error": release.finalization_error,
            "release_error": release.release_error,
            "repair_error": release.repair_error,
            "release_replan_pending": (
                release.replan_pending.model_dump(mode="json") if release.replan_pending else None
            ),
            "resource_decision": (
                resources.decision.model_dump(mode="json") if resources.decision else None
            ),
            "repair_loop_stop": (
                resources.repair_loop_stop.model_dump(mode="json")
                if resources.repair_loop_stop
                else None
            ),
            "extension": (
                extension.last_event.model_dump(mode="json") if extension.last_event else None
            ),
        }
        cls._put_non_none(state.metadata, optional)
        cls._put_truthy(
            state.metadata,
            {
                "bootstrap_independent_checkpoint_required": (
                    bootstrap.independent_checkpoint_required
                ),
                "steering_replan_pending": control.steering_replan_pending,
                "frame_breaks": list(planning.frame_breaks),
                "extension_replan_pending": extension.replan_pending,
                "verification_dead_end": list(verification.dead_end),
                "semantic_ci_deterministic_failures": list(verification.deterministic_failures),
                "repair_remaining_uncertainty": list(release.repair_remaining_uncertainty),
                "resource_extension_recommended": resources.extension_recommended,
            },
        )
        for stage, result in verification.stages.items():
            state.metadata[f"{stage}_check_artifact_digest"] = result.artifact_digest
            state.metadata[f"{stage}_check_failed"] = result.failed
            state.metadata[f"{stage}_check_failures"] = list(result.failures)
        for key, value in completion.model_dump(mode="json", exclude_none=True).items():
            state.metadata[key] = value

    @staticmethod
    def _put_non_none(target: dict[str, Any], values: dict[str, Any]) -> None:
        for key, value in values.items():
            if value is not None:
                target[key] = value

    @staticmethod
    def _put_truthy(target: dict[str, Any], values: dict[str, Any]) -> None:
        for key, value in values.items():
            if value:
                target[key] = value

    def replay(self, events: Iterable[LedgerEvent]) -> RunState:
        state: RunState | None = None
        for event in events:
            state = self.apply(state, event)
        if state is None:
            raise ValueError("Cannot reconstruct state from an empty ledger")
        return state

    def apply(self, state: RunState | None, event: LedgerEvent) -> RunState:
        if event.event_type not in et.EVENT_TYPES:
            raise ValueError(f"Unsupported event type: {event.event_type}")
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
        elif projector := self._PROJECTORS_BY_EVENT.get(event.event_type):
            projector.apply(state, event)
        elif event.event_type in et.OBSERVATION_ONLY_EVENT_TYPES:
            pass
        else:
            # EVENT_TYPES and this dispatch are deliberately separate. The
            # explicit failure makes a newly declared event impossible to
            # replay as a silent no-op.
            raise ValueError(f"Event type has no state projection: {event.event_type}")

        self._sync_legacy_metadata(state)
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
            "replan_pending": state.runtime.verification.replan_pending,
            "preflight_failures": list(
                state.runtime.verification.stages.get("preflight", CheckStageState()).failures
            ),
            "candidate_failures": list(
                state.runtime.verification.stages.get("candidate", CheckStageState()).failures
            ),
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
