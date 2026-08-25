"""Non-authoritative live projection of semantic and provider activity."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from . import events as et
from .control import RunControlPlane, RuntimeStatus
from .ledger import LedgerEvent
from .models import RunState
from .util import utc_now


class LiveObserver:
    """Keep transient operator state useful without making it load-bearing."""

    def __init__(self, control: RunControlPlane, logger: logging.Logger) -> None:
        self.control = control
        self.logger = logger
        self._active_calls: dict[str, tuple[str, str | None]] = {}

    def set_runtime(
        self,
        status: RuntimeStatus,
        *,
        phase: str,
        detail: str,
        current_call_id: str | None = None,
        current_action_id: str | None = None,
    ) -> None:
        try:
            previous = self.control.runtime()
            self.control.set_runtime(
                status=status,
                phase=phase,
                pid=os.getpid(),
                started_at=previous.started_at or utc_now(),
                current_call_id=current_call_id,
                current_action_id=current_action_id,
                detail=detail,
            )
        except Exception:
            self.logger.exception("live runtime update failed")

    def record_activity(
        self,
        *,
        kind: str,
        label: str,
        message: str,
        state: str = "active",
        call_id: str | None = None,
        action_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.control.record_activity(
                kind=kind,
                label=label,
                message=message,
                state=state,
                call_id=call_id,
                action_id=action_id,
                payload=payload,
            )
        except Exception:
            self.logger.exception("live activity update failed")

    def ledger_event(self, event: LedgerEvent, state: RunState) -> None:
        """Project one semantic event, skipping internal bookkeeping noise."""

        label = event.event_type.replace(".", " ")
        message = label
        activity_state = "active"
        if event.event_type == et.BOOTSTRAP_STARTED:
            message = "building the first complete artifact"
        elif event.event_type == et.BOOTSTRAP_COMPLETED:
            message = (
                f"baseline ready · {len(state.open_issues)} issues · "
                f"{len(state.pending_action_ids)} proposed actions"
            )
            activity_state = "done"
        elif event.event_type == et.ACTION_SELECTED:
            message = f"{len(event.payload.get('selected', {}))} actions selected"
        elif event.event_type == et.ACTION_STARTED:
            record = state.actions.get(event.action_id or "")
            message = record.spec.target if record else str(event.action_id or "action")
        elif event.event_type == et.ACTION_COMPLETED:
            message = f"{event.action_id} retained"
            activity_state = "done"
        elif event.event_type == et.ACTION_FAILED:
            message = f"{event.action_id} failed · residue retained"
            activity_state = "warn"
        elif event.event_type == et.CHECKPOINT_STARTED:
            message = "reducing evidence into the current artifact"
        elif event.event_type == et.CHECKPOINT_COMPLETED:
            message = f"round {state.round_index} · {len(state.open_issues)} open issues"
            activity_state = "done"
        elif event.event_type == et.FINALIZATION_STARTED:
            message = "rebuilding one coherent deliverable"
        elif event.event_type == et.FINAL_SYNTHESIZED:
            message = "final synthesis captured"
            activity_state = "done"
        elif event.event_type == et.RELEASE_COMPLETED:
            message = "fresh release challenge completed"
            activity_state = "done"
        elif event.event_type == et.RELEASE_FAILED:
            message = "release challenge failed · diagnostics retained"
            activity_state = "warn"
        elif event.event_type == et.REPAIR_COMPLETED:
            message = "bounded release repair integrated"
            activity_state = "done"
        elif event.event_type == et.REPAIR_LOOP_STOPPED:
            message = str(event.payload.get("reason", "repair loop stopped"))
            activity_state = "warn"
        elif event.event_type == et.RESOURCE_INITIALIZED:
            resource = event.payload.get("resource_state", {})
            message = (
                f"adaptive horizon {resource.get('active_call_limit', '?')} · "
                f"hard ceiling {resource.get('hard_call_limit', '?')}"
            )
            activity_state = "done"
        elif event.event_type == et.RESOURCE_DECIDED:
            decision = event.payload.get("decision", {})
            reasons = "; ".join(decision.get("reasons", []))
            message = f"{str(decision.get('kind', 'decision')).replace('_', ' ')} · {reasons}"
            activity_state = "warn" if decision.get("kind") == "extension_required" else "done"
        elif event.event_type == et.SEMANTIC_REGRESSION_COMPLETED:
            message = "semantic regression checks completed"
            activity_state = "done"
        elif event.event_type == et.COMPLETION_CASE_BUILT:
            message = "completion case built"
            activity_state = "done"
        elif event.event_type == et.RUN_COMPLETED:
            message = "result sealed"
            activity_state = "done"
        elif event.event_type == et.TASK_SOURCE_AMENDED:
            message = "operator steering admitted · replanning required"
            activity_state = "done"
        elif event.event_type == et.RUN_PAUSED:
            message = "run paused at a safe boundary"
            activity_state = "warn"
        elif event.event_type == et.RUN_RESUMED:
            message = "run resumed"
            activity_state = "done"
        elif event.event_type == et.RUN_STOPPED:
            message = "run stopped · state remains resumable"
            activity_state = "warn"
        elif event.event_type.endswith(".failed"):
            message = f"{label} · diagnostics retained"
            activity_state = "warn"
        else:
            return

        self.record_activity(
            kind="ledger",
            label=label,
            message=message,
            state=activity_state,
            action_id=event.action_id,
            payload={"event_seq": event.seq, "event_type": event.event_type},
        )
        try:
            runtime = self.control.runtime()
            if runtime.status not in {
                RuntimeStatus.PAUSED,
                RuntimeStatus.STOPPED,
                RuntimeStatus.COMPLETE,
                RuntimeStatus.FAILED,
            }:
                self.control.set_runtime(
                    status=(
                        runtime.status
                        if runtime.status != RuntimeStatus.IDLE
                        else RuntimeStatus.RUNNING
                    ),
                    phase=state.phase.value,
                    pid=runtime.pid or os.getpid(),
                    started_at=runtime.started_at,
                    current_call_id=runtime.current_call_id,
                    current_action_id=event.action_id or runtime.current_action_id,
                    detail=event.event_type,
                )
        except Exception:
            self.logger.exception("live control projection failed")

    def provider_callback(
        self,
        *,
        call_id: str,
        call_kind: str,
        action_id: str | None,
    ) -> Callable[[dict[str, Any]], None]:
        """Map OMP events to a compact stream without raw text or thinking."""

        def callback(event: dict[str, Any]) -> None:
            event_type = str(event.get("type", "provider.activity"))
            if event_type == "tool_execution_start":
                self.record_activity(
                    kind="tool",
                    label=str(event.get("toolName") or "tool"),
                    message=str(event.get("intent") or "executing")[:240],
                    call_id=call_id,
                    action_id=action_id,
                    payload={"argument_summary": event.get("arguments", {})},
                )
            elif event_type == "tool_execution_end":
                failed = bool(event.get("isError"))
                self.record_activity(
                    kind="tool",
                    label=str(event.get("toolName") or "tool"),
                    message="failed" if failed else "completed",
                    state="warn" if failed else "done",
                    call_id=call_id,
                    action_id=action_id,
                )
            elif event_type == "message_end":
                message = event.get("message")
                message = message if isinstance(message, dict) else {}
                usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                for block in message.get("content", []):
                    if not isinstance(block, dict) or block.get("type") != "toolCall":
                        continue
                    if block.get("name") != "task":
                        continue
                    arguments = block.get("arguments")
                    arguments = arguments if isinstance(arguments, dict) else {}
                    for task_name in arguments.get("task_names", []):
                        self.record_activity(
                            kind="subagent",
                            label=str(task_name)[:80],
                            message="started assigned work",
                            call_id=call_id,
                            action_id=action_id,
                        )
                self.record_activity(
                    kind="model",
                    label=call_kind,
                    message=f"model turn completed · {message.get('stopReason', 'complete')}",
                    state="done",
                    call_id=call_id,
                    action_id=action_id,
                    payload={"usage": usage},
                )
            elif event_type == "subagent_activity":
                state = str(event.get("state") or "active")
                self.record_activity(
                    kind="subagent",
                    label=str(event.get("agent") or "subagent")[:80],
                    message=str(event.get("message") or "reported progress")[:240],
                    state=state if state in {"active", "done", "warn"} else "active",
                    call_id=call_id,
                    action_id=action_id,
                )
            elif event_type == "session":
                self.record_activity(
                    kind="model",
                    label=call_kind,
                    message="model session connected",
                    call_id=call_id,
                    action_id=action_id,
                )

        return callback

    def begin_call(
        self,
        *,
        phase: str,
        call_id: str,
        call_kind: str,
        action_id: str | None,
    ) -> None:
        self._active_calls[call_id] = (call_kind, action_id)
        count = len(self._active_calls)
        self.record_activity(
            kind="model",
            label=call_kind,
            message="model call started",
            call_id=call_id,
            action_id=action_id,
        )
        self.set_runtime(
            RuntimeStatus.RUNNING,
            phase=phase,
            detail=(
                f"{call_kind} · {count} model call{'s' if count != 1 else ''} active"
            ),
            current_call_id=call_id,
            current_action_id=action_id,
        )

    def finish_call(self, call_id: str, *, phase: str, failed: bool = False) -> None:
        call_kind, action_id = self._active_calls.pop(call_id, ("model", None))
        if failed:
            self.record_activity(
                kind="model",
                label=call_kind,
                message="model call failed · durable diagnostics retained",
                state="warn",
                call_id=call_id,
                action_id=action_id,
            )
        active = next(iter(self._active_calls.items()), None)
        self.set_runtime(
            RuntimeStatus.RUNNING,
            phase=phase,
            detail=(
                f"{len(self._active_calls)} model calls active"
                if active
                else "controller boundary"
            ),
            current_call_id=active[0] if active else None,
            current_action_id=active[1][1] if active else None,
        )
