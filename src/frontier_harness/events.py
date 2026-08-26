"""Ledger event type names kept in one place."""

RUN_CREATED = "run.created"
BOOTSTRAP_STARTED = "bootstrap.started"
BOOTSTRAP_COMPLETED = "bootstrap.completed"
BOOTSTRAP_FAILED = "bootstrap.failed"
ACTION_SELECTED = "action.selected"
ACTION_STARTED = "action.started"
ACTION_ATTEMPT_STARTED = "action_attempt.started"
ACTION_ATTEMPT_FINISHED = "action_attempt.finished"
ACTION_COMPLETED = "action.completed"
ACTION_FAILED = "action.failed"
CHECKPOINT_STARTED = "checkpoint.started"
CHECKPOINT_COMPLETED = "checkpoint.completed"
CHECKPOINT_FAILED = "checkpoint.failed"
ROUND_COMPLETED = "round.completed"
FINALIZATION_STARTED = "finalization.started"
FINAL_SYNTHESIZED = "final.synthesized"
FINALIZATION_FAILED = "finalization.failed"
DETERMINISTIC_CHECK_COMPLETED = "check.completed"
CHECK_STAGE_COMPLETED = "check.stage_completed"
CHECK_REPLAN_DECIDED = "check.replan_decided"
EVIDENCE_RECORDED = "evidence.recorded"
RELEASE_COMPLETED = "release.completed"
RELEASE_FAILED = "release.failed"
REPAIR_COMPLETED = "repair.completed"
REPAIR_FAILED = "repair.failed"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
PATCH_APPLIED = "patch.applied"

# v3.5 additive semantic events. Existing event types and projections remain
# valid; these records make continuity, obligations, Summit activation, and
# release evidence inspectable rather than hidden in prompts.
TASK_SOURCE_CAPTURED = "task_source.captured"
TASK_SOURCE_AMENDED = "task_source.amended"
TASK_CHARTER_UPDATED = "task_charter.updated"
REFRAME_ADMITTED = "reframe.admitted"
REFRAME_REJECTED = "reframe.rejected"
LEAD_SESSION_UPDATED = "lead_session.updated"
LEAD_RECONSTRUCTION = "lead_session.reconstructed"
ARTIFACT_SPINE_UPDATED = "artifact_spine.updated"
OBLIGATIONS_UPDATED = "obligations.updated"
CRUXES_UPDATED = "cruxes.updated"
SUBSTRATE_UPDATED = "substrate.updated"
OVERLAYS_UPDATED = "overlays.updated"
INSTRUMENT_UPDATED = "instrument.updated"
SUMMIT_ACTIVATED = "summit.activated"
SUMMIT_ARCHIVE_UPDATED = "summit.archive_updated"
ACTION_CONTRACTED = "action.contracted"
ACTION_RECEIPTED = "action.receipted"
SEMANTIC_REGRESSION_COMPLETED = "semantic_regression.completed"
COMPLETION_CASE_BUILT = "completion_case.built"
RUN_EXTENDED = "run.extended"
RUN_PAUSED = "run.paused"
RUN_RESUMED = "run.resumed"
RUN_STOPPED = "run.stopped"
RESOURCE_INITIALIZED = "resource.initialized"
RESOURCE_DECIDED = "resource.decided"
REPAIR_LOOP_STOPPED = "repair.loop_stopped"
FRONTIER_REPLAN_REQUESTED = "frontier.replan_requested"
RELEASE_RECOVERY_REQUESTED = "release.recovery_requested"

# This is the compatibility boundary for ledger replay. Adding an event is an
# architectural change: it must be named here and handled explicitly by the
# state projector (even when the event is intentionally observation-only).
EVENT_TYPES = frozenset(
    {
        RUN_CREATED,
        BOOTSTRAP_STARTED,
        BOOTSTRAP_COMPLETED,
        BOOTSTRAP_FAILED,
        ACTION_SELECTED,
        ACTION_STARTED,
        ACTION_ATTEMPT_STARTED,
        ACTION_ATTEMPT_FINISHED,
        ACTION_COMPLETED,
        ACTION_FAILED,
        CHECKPOINT_STARTED,
        CHECKPOINT_COMPLETED,
        CHECKPOINT_FAILED,
        ROUND_COMPLETED,
        FINALIZATION_STARTED,
        FINAL_SYNTHESIZED,
        FINALIZATION_FAILED,
        DETERMINISTIC_CHECK_COMPLETED,
        CHECK_STAGE_COMPLETED,
        CHECK_REPLAN_DECIDED,
        EVIDENCE_RECORDED,
        RELEASE_COMPLETED,
        RELEASE_FAILED,
        REPAIR_COMPLETED,
        REPAIR_FAILED,
        RUN_COMPLETED,
        RUN_FAILED,
        PATCH_APPLIED,
        TASK_SOURCE_CAPTURED,
        TASK_SOURCE_AMENDED,
        TASK_CHARTER_UPDATED,
        REFRAME_ADMITTED,
        REFRAME_REJECTED,
        LEAD_SESSION_UPDATED,
        LEAD_RECONSTRUCTION,
        ARTIFACT_SPINE_UPDATED,
        OBLIGATIONS_UPDATED,
        CRUXES_UPDATED,
        SUBSTRATE_UPDATED,
        OVERLAYS_UPDATED,
        INSTRUMENT_UPDATED,
        SUMMIT_ACTIVATED,
        SUMMIT_ARCHIVE_UPDATED,
        ACTION_CONTRACTED,
        ACTION_RECEIPTED,
        SEMANTIC_REGRESSION_COMPLETED,
        COMPLETION_CASE_BUILT,
        RUN_EXTENDED,
        RUN_PAUSED,
        RUN_RESUMED,
        RUN_STOPPED,
        RESOURCE_INITIALIZED,
        RESOURCE_DECIDED,
        REPAIR_LOOP_STOPPED,
        FRONTIER_REPLAN_REQUESTED,
        RELEASE_RECOVERY_REQUESTED,
    }
)

OBSERVATION_ONLY_EVENT_TYPES = frozenset({REFRAME_ADMITTED, REFRAME_REJECTED})
