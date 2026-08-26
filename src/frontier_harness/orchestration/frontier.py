"""Adaptive frontier loop over scheduler-selected semantic work."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import events as et
from ..errors import FrontierError
from ..models import ActionSpec, ActionStatus, CognitiveTopology
from ..scheduler import SelectionResult

if TYPE_CHECKING:
    from ..engine import FrontierEngine

TERMINAL_ACTION_STATUSES = {
    ActionStatus.COMPLETE,
    ActionStatus.FAILED,
    ActionStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class IterationGate:
    restart: bool = False
    stop_reason: str | None = None


class FrontierLoop:
    """Spend bounded horizons until convergence or a hard envelope stops work."""

    async def execute(self, engine: FrontierEngine) -> str:
        while not engine.state.stop_requested:
            gate = await self._entry_gate(engine)
            if gate.stop_reason:
                return gate.stop_reason
            if gate.restart:
                continue

            pending = [
                engine.state.actions[action_id]
                for action_id in engine.state.pending_action_ids
                if action_id in engine.state.actions
            ]
            if pending and all(item.status in TERMINAL_ACTION_STATUSES for item in pending):
                round_index = max(item.spec.round_index for item in pending)
                if not await engine._checkpoint(
                    [item.spec.action_id for item in pending], round_index
                ):
                    return "checkpoint failed or lacked remaining call budget"
                continue

            proposals = [item.spec for item in pending if item.status == ActionStatus.PROPOSED]
            if not proposals:
                if await engine._replan_dead_frontier(
                    reason="no proposed action remained while release debt was still open",
                    action_records=pending,
                ):
                    continue
                return "no decision-relevant action remains"

            available, stop_reason = self._worker_capacity(engine, proposals)
            if stop_reason:
                return stop_reason
            selection = self._select(engine, proposals, available)
            self._record_selection(engine, selection)
            if not selection.selected:
                if await engine._replan_dead_frontier(
                    reason=(
                        "scheduler found no executable action; every proposal was dominated, "
                        "correlated, deferred, or below threshold"
                    ),
                    action_records=pending,
                ):
                    continue
                return (
                    "planner deadlock: unresolved release debt remains but no executable "
                    "action survived an independent replan"
                )

            round_state = engine.state.model_copy(deep=True)
            allocations = self._allocate_calls(engine, selection.selected, available)
            await asyncio.gather(
                *(
                    engine._execute_action(
                        action,
                        round_state,
                        max_provider_calls=allocations[action.action_id],
                    )
                    for action in selection.selected
                )
            )
            await engine._control_boundary()
            if not await engine._checkpoint(
                [action.action_id for action in selection.selected],
                selection.selected[0].round_index,
            ):
                return "checkpoint failed or lacked remaining call budget"
        return engine.state.stop_reason or "semantic controller requested stop"

    @staticmethod
    async def _entry_gate(engine: FrontierEngine) -> IterationGate:
        if failures := engine.state.runtime.verification.dead_end:
            raise FrontierError(
                "Staged verification remained failed after a corrective Lead checkpoint, "
                "and no corrective action was proposed: " + "; ".join(failures)
            )
        if await engine._control_boundary():
            if await engine._checkpoint([], engine.state.round_index):
                return IterationGate(restart=True)
            return IterationGate(
                stop_reason="operator steering admitted but replanning checkpoint failed"
            )
        if engine.state.runtime.verification.replan_pending:
            if await engine._checkpoint([], engine.state.round_index):
                return IterationGate(restart=True)
            return IterationGate(
                stop_reason=(
                    "staged verification failed and its corrective checkpoint could not run"
                )
            )
        round_limit = engine.config.run.budget.max_rounds
        if round_limit is not None and engine.state.round_index >= round_limit:
            return IterationGate(stop_reason="round budget reached")
        return IterationGate(stop_reason=engine._budget_limit_reason(calls=False))

    @staticmethod
    def _worker_capacity(
        engine: FrontierEngine,
        proposals: list[ActionSpec],
    ) -> tuple[int, str | None]:
        reserve = engine._completion_reserve_calls()
        available = max(0, engine._active_calls_remaining() - reserve - 1)
        if available > 0:
            return available, None
        if engine._resource_boundary(proposals):
            reserve = engine._completion_reserve_calls()
            available = max(0, engine._active_calls_remaining() - reserve - 1)
        if available > 0:
            return available, None

        decision = engine.state.resource_state.last_decision if engine.state.resource_state else None
        if decision and decision.extension_recommended:
            return 0, (
                "hard resource envelope reached with useful work remaining; "
                "extension recommended"
            )
        detail = "; ".join(decision.reasons) if decision else "completion reserve reached"
        return 0, f"adaptive resource governor stopped frontier work: {detail}"

    @staticmethod
    def _select(
        engine: FrontierEngine,
        proposals: list[ActionSpec],
        available_calls: int,
    ) -> SelectionResult:
        control_cap = int(
            engine.config.run.budget.max_calls
            * engine.config.cognition.max_control_call_fraction
        )
        if engine.config.cognition.max_control_call_fraction > 0:
            control_cap = max(1, control_cap)
        control_used = sum(
            record.status in TERMINAL_ACTION_STATUSES and not record.spec.substantive
            for record in engine.state.actions.values()
        )
        eligible: list[ActionSpec] = []
        deferred: dict[str, str] = {}
        for proposal in proposals:
            if not proposal.substantive and control_used >= control_cap:
                deferred[proposal.action_id] = (
                    "discretionary control-action budget exhausted; substantive reasoning "
                    "and release reserve protected"
                )
            else:
                eligible.append(proposal)

        selection = engine.scheduler.select(
            eligible,
            max_parallel=engine.config.run.budget.max_parallel,
            available_calls=available_calls,
            obligations=engine.state.obligations,
            target_stalls=engine._target_stalls(),
            human_evidence_available=engine.config.cognition.human_evidence_available,
            require_execution_trigger=engine.config.cognition.require_execution_trigger,
            frontier_kernel=engine.state.frontier_kernel,
            action_records=engine.state.actions,
            frontier_advancing_action_ids=set(engine.state.frontier_advancing_action_ids),
        )
        selection.deferred.update(deferred)
        FrontierLoop._serialize_lead(selection)
        return selection

    @staticmethod
    def _serialize_lead(selection: SelectionResult) -> None:
        lead = next(
            (
                action
                for action in selection.selected
                if action.topology == CognitiveTopology.LEAD
            ),
            None,
        )
        if lead is None:
            return
        for action in selection.selected:
            if action.action_id == lead.action_id:
                continue
            selection.deferred[action.action_id] = (
                "serialized behind the persistent Lead action"
            )
            selection.selected_reasons.pop(action.action_id, None)
        selection.selected = [lead]

    @staticmethod
    def _record_selection(engine: FrontierEngine, selection: SelectionResult) -> None:
        engine._append(
            et.ACTION_SELECTED,
            {
                "selected": selection.selected_reasons,
                "dominated": selection.dominated,
                "deferred": selection.deferred,
            },
            actor="scheduler",
        )

    @staticmethod
    def _allocate_calls(
        engine: FrontierEngine,
        selected: list[ActionSpec],
        available_calls: int,
    ) -> dict[str, int]:
        allocations = {action.action_id: 1 for action in selected}
        spare = max(0, available_calls - len(selected))
        while spare:
            admitted = False
            for action in selected:
                if allocations[action.action_id] >= engine.config.provider.schema_attempts:
                    continue
                allocations[action.action_id] += 1
                spare -= 1
                admitted = True
                if spare == 0:
                    break
            if not admitted:
                break
        return allocations
