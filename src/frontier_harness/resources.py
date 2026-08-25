"""Evidence-driven compute allocation inside an operator-owned hard envelope."""

from __future__ import annotations

from .config import ResourcePolicy
from .models import (
    ActionStatus,
    BudgetContract,
    Impact,
    IssueStatus,
    ObligationStatus,
    ResourceDecision,
    ResourceDecisionKind,
    ResourceSnapshot,
    ResourceState,
    RunState,
)


class ResourceGovernor:
    """Grant rolling work horizons without turning heuristics into task law.

    The governor never raises the operator's hard envelope.  It only decides
    how much of that envelope is currently available to the frontier and how
    much must remain for a coherent synthesis/release path.
    """

    def __init__(
        self,
        *,
        policy: ResourcePolicy,
        budget: BudgetContract,
        release_expected: bool,
        max_material_repairs: int | None,
    ) -> None:
        self.policy = policy
        self.budget = budget
        self.release_expected = release_expected
        self.max_material_repairs = max_material_repairs

    @staticmethod
    def _informative_actions(state: RunState) -> int:
        informative = 0
        for record in state.actions.values():
            if record.status not in {
                ActionStatus.COMPLETE,
                ActionStatus.FAILED,
                ActionStatus.DOMINATED,
                ActionStatus.DEFERRED,
            }:
                continue
            receipt = record.receipt
            result = record.result
            if (
                bool(record.objective_measurement and record.objective_measurement.valid)
                or bool(
                    receipt
                    and (
                        receipt.state_changes
                        or receipt.decisions_changed
                        or receipt.obligations_unlocked
                        or receipt.obligations_invalidated
                        or receipt.forecast_was_useful
                        or receipt.evidence_strength in {"strong", "decisive"}
                    )
                )
                or bool(
                    result and (result.negative_result or result.materiality in {"high", "fatal"})
                )
            ):
                informative += 1
        return informative

    @classmethod
    def snapshot(cls, state: RunState) -> ResourceSnapshot:
        accepted = sum(
            bool(record.receipt and record.receipt.integration_status == "accepted")
            for record in state.actions.values()
        )
        failed = sum(record.status == ActionStatus.FAILED for record in state.actions.values())
        scope_rank = {
            "targeted": 0,
            "sequence": 1,
            "whole_artifact": 2,
            "release": 3,
        }
        scoped_evidence = 0
        for obligation in state.obligations.values():
            relevant = [
                evidence
                for evidence in state.evidence.values()
                if evidence.evidence_id in obligation.evidence_references
                or (
                    evidence.source_action_id in state.actions
                    and obligation.obligation_id
                    in state.actions[evidence.source_action_id].spec.obligation_ids
                )
            ]
            for evidence in relevant:
                if evidence.negative_result:
                    continue
                if scope_rank[evidence.artifact_scope] < scope_rank[obligation.required_artifact_scope]:
                    continue
                if not set(obligation.required_evidence_modalities).issubset(
                    set(evidence.modalities)
                ):
                    continue
                if evidence.modalities or evidence.establishes:
                    scoped_evidence += 1
                    break
        if not state.obligations:
            scoped_evidence = sum(
                bool(item.modalities or item.establishes) and not item.negative_result
                for item in state.evidence.values()
            )
        return ResourceSnapshot(
            calls=state.usage.calls,
            model_requests=state.usage.model_requests,
            input_tokens=state.usage.input_tokens,
            output_tokens=state.usage.output_tokens,
            wall_seconds=state.usage.wall_seconds,
            artifact_digest=(
                state.current_artifact.blob.digest if state.current_artifact else None
            ),
            accepted_actions=accepted,
            informative_actions=cls._informative_actions(state),
            failed_actions=failed,
            release_blockers=sum(
                item.release_blocking
                and item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
                for item in state.obligations.values()
            ),
            high_impact_issues=sum(
                item.status == IssueStatus.OPEN and item.impact in {Impact.FATAL, Impact.HIGH}
                for item in state.issues.values()
            ),
            active_cruxes=len(state.active_cruxes),
            scoped_evidence=scoped_evidence,
        )

    def completion_reserve(self, state: RunState) -> int:
        if self.policy.mode == "static":
            return self.budget.synthesis_reserve_calls

        # One clean synthesis is always protected.  A release challenge adds
        # one call.  When material release debt or semantic uncertainty exists,
        # protect one complete repair + fresh-challenge cycle.  Further repair
        # cycles must earn compute from the rolling governor rather than being
        # reserved speculatively.
        reserve = 1
        if self.release_expected:
            reserve += 1
            repairs_used = int(state.metadata.get("repair_count", 0))
            material_risk = (
                state.current_artifact is None
                or bool(state.release_blocking_obligations)
                or bool(state.high_impact_open_issues)
                or state.metadata.get("semantic_ci_passed") is not True
                or bool(
                    state.release
                    and (state.release.requires_repair or not state.release.releaseable)
                )
            )
            repairs_available = (
                self.max_material_repairs is None or repairs_used < self.max_material_repairs
            )
            if material_risk and repairs_available:
                reserve += 2
        return reserve

    def initial_state(self, state: RunState) -> ResourceState:
        hard = self.budget.max_calls
        if self.policy.mode == "static":
            active = hard
        else:
            reserve = self.completion_reserve(state)
            derived = 1 + self.budget.max_parallel + 1 + reserve
            requested = self.policy.initial_call_grant or derived
            active = min(hard, max(reserve + 2, requested))
        return ResourceState(
            mode=self.policy.mode,
            active_call_limit=active,
            hard_call_limit=hard,
            last_snapshot=self.snapshot(state),
        )

    @staticmethod
    def _progress(before: ResourceSnapshot, after: ResourceSnapshot) -> tuple[int, list[str]]:
        """Score decision-changing gradient, not visible activity.

        Mutating the artifact is deliberately worth too little to earn a new
        tranche by itself.  Negative results count when they eliminate a live
        branch; failed calls and mere token consumption do not.
        """

        # ``score`` counts independent material signal classes; it is not a
        # hand-tuned utility function.  Magnitude remains visible in reasons.
        score = 0
        reasons: list[str] = []
        artifact_changed = bool(
            after.artifact_digest and after.artifact_digest != before.artifact_digest
        )
        if artifact_changed:
            reasons.append("authoritative artifact changed")
        accepted_delta = after.accepted_actions - before.accepted_actions
        if after.accepted_actions > before.accepted_actions:
            reasons.append(f"{accepted_delta} new action result(s) were accepted")
        if artifact_changed and accepted_delta > 0:
            score += 1
        if after.informative_actions > before.informative_actions:
            delta = after.informative_actions - before.informative_actions
            reasons.append(f"{delta} new discriminative result(s) landed")
            score += 1
        if after.release_blockers < before.release_blockers:
            delta = before.release_blockers - after.release_blockers
            reasons.append(f"release-blocking debt decreased by {delta}")
            score += 1
        if after.high_impact_issues < before.high_impact_issues:
            delta = before.high_impact_issues - after.high_impact_issues
            reasons.append(f"high-impact issue debt decreased by {delta}")
            score += 1
        if after.active_cruxes < before.active_cruxes:
            delta = before.active_cruxes - after.active_cruxes
            reasons.append(f"active crux count decreased by {delta}")
            score += 1
        if after.scoped_evidence > before.scoped_evidence:
            delta = after.scoped_evidence - before.scoped_evidence
            reasons.append(f"scoped evidence coverage increased by {delta}")
            score += 1
        return score, reasons

    def decide(
        self,
        state: RunState,
        resource: ResourceState,
        *,
        actionable_actions: int,
    ) -> tuple[ResourceDecision, ResourceState]:
        snapshot = self.snapshot(state)
        gradient_score, progress = self._progress(resource.last_snapshot, snapshot)
        reserve = self.completion_reserve(state)
        debt = snapshot.release_blockers + snapshot.high_impact_issues + snapshot.active_cruxes
        stagnation_patience = (
            self.policy.max_stagnant_grants
            if self.policy.max_stagnant_grants is not None
            else ((debt + self.budget.max_parallel - 1) // self.budget.max_parallel if debt else 0)
        )
        before = resource.active_call_limit
        hard = resource.hard_call_limit
        reasons: list[str] = []

        if self.policy.mode == "static":
            kind = ResourceDecisionKind.FINALIZE
            after = hard
            stagnant = resource.stagnant_grants
            reasons.append("static resource mode reached its configured completion reserve")
        elif actionable_actions <= 0:
            kind = ResourceDecisionKind.FINALIZE
            after = before
            stagnant = resource.stagnant_grants
            reasons.append("no feasible decision-changing action remains")
        else:
            meaningful_progress = gradient_score > 0
            stagnant = 0 if meaningful_progress else resource.stagnant_grants + 1
            gradient_alive = meaningful_progress or (debt > 0 and stagnant <= stagnation_patience)
            if before < hard and gradient_alive:
                # Default tranche = the feasible worker wave plus its
                # integration checkpoint.  A one-action frontier therefore
                # does not receive a full-width parallel batch by habit.
                step = self.policy.grant_step_calls or (
                    min(self.budget.max_parallel, max(1, actionable_actions)) + 1
                )
                after = min(hard, before + step)
                kind = ResourceDecisionKind.GRANT
                reasons.append(
                    "fresh progress supports another work horizon"
                    if meaningful_progress
                    else "unresolved material debt earns a bounded exploration grace"
                )
            elif before >= hard and gradient_alive:
                after = before
                kind = ResourceDecisionKind.EXTENSION_REQUIRED
                reasons.append("useful work remains beyond the operator's hard envelope")
            else:
                after = before
                kind = ResourceDecisionKind.FINALIZE
                reasons.append("recent work did not produce enough gradient for another tranche")

        decision = ResourceDecision(
            kind=kind,
            active_call_limit_before=before,
            active_call_limit_after=after,
            hard_call_limit=hard,
            completion_reserve_calls=reserve,
            actionable_actions=actionable_actions,
            gradient_score=gradient_score,
            stagnation_patience=stagnation_patience,
            progress_reasons=progress,
            reasons=reasons,
            extension_recommended=kind == ResourceDecisionKind.EXTENSION_REQUIRED,
            snapshot=snapshot,
        )
        updated = resource.model_copy(
            update={
                "active_call_limit": after,
                "grant_count": resource.grant_count + int(kind == ResourceDecisionKind.GRANT),
                "stagnant_grants": stagnant,
                "last_snapshot": snapshot,
                "last_decision": decision,
            }
        )
        return decision, updated
