"""Evidence-driven compute allocation inside an operator-owned hard envelope."""

from __future__ import annotations

from .config import ResourcePolicy
from .models import (
    ActionStatus,
    BudgetContract,
    Impact,
    IndependenceClass,
    IssueStatus,
    ObligationStatus,
    ProgressVector,
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
            # Model-authored materiality and negative-result flags are useful
            # proposals, not resource currency.  A result is independently
            # informative here only when a runtime measurement landed or the
            # observed channel was confirmed.  Pure reasoning earns its next
            # horizon through the keeper-owned FrontierKernel revision.
            if bool(record.objective_measurement and record.objective_measurement.valid) or bool(
                receipt
                and receipt.evidence_channel_confirmed
                and (
                    receipt.state_changes
                    or receipt.decisions_changed
                    or receipt.obligations_unlocked
                    or receipt.obligations_invalidated
                    or receipt.forecast_was_useful
                    or receipt.evidence_strength in {"strong", "decisive"}
                )
            ):
                informative += 1
        return informative

    @staticmethod
    def _objective_improvements(state: RunState) -> int:
        improved = 0
        for record in state.actions.values():
            before = record.baseline_objective_measurement
            after = record.objective_measurement
            if before is None or after is None or not before.valid or not after.valid:
                continue
            metric = after.primary_metric
            if metric not in before.metrics or metric not in after.metrics:
                continue
            left, right = before.metrics[metric], after.metrics[metric]
            improved += int(
                (after.direction == "maximize" and right > left)
                or (after.direction == "minimize" and right < left)
            )
        return improved

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

        def credible(evidence: object) -> bool:
            item = evidence
            independence = getattr(item, "independence_class", None)
            if independence not in {
                IndependenceClass.SAME_MODEL,
                IndependenceClass.DIFFERENT_CONDITIONING,
            }:
                return True
            source_action_id = getattr(item, "source_action_id", None)
            source = state.actions.get(source_action_id or "")
            return bool(source and source.receipt and source.receipt.evidence_channel_confirmed)

        for obligation in state.obligations.values():
            relevant = [
                evidence
                for evidence in state.evidence.values()
                if credible(evidence)
                and (
                    evidence.evidence_id in obligation.evidence_references
                    or (
                        evidence.source_action_id in state.actions
                        and obligation.obligation_id
                        in state.actions[evidence.source_action_id].spec.obligation_ids
                    )
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
                credible(item)
                and bool(item.modalities or item.establishes)
                and not item.negative_result
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
            frontier_revision=(state.frontier_kernel.revision if state.frontier_kernel else 0),
            objective_improvements=cls._objective_improvements(state),
            productive_discoveries=sum(
                item.productive_results for item in state.discovery_records.values()
            ),
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
    def _progress(
        before: ResourceSnapshot, after: ResourceSnapshot
    ) -> tuple[ProgressVector, list[str]]:
        """Preserve decision-changing movement as a vector, not a fake utility.

        Mutating the artifact is deliberately worth too little to earn a new
        tranche by itself.  Negative results count when they eliminate a live
        branch; failed calls and mere token consumption do not.
        """

        reasons: list[str] = []
        artifact_changed = bool(
            after.artifact_digest and after.artifact_digest != before.artifact_digest
        )
        if artifact_changed:
            reasons.append("authoritative artifact changed")
        accepted_delta = after.accepted_actions - before.accepted_actions
        if after.accepted_actions > before.accepted_actions:
            reasons.append(f"{accepted_delta} new action result(s) were accepted")
        quality = int(artifact_changed and accepted_delta > 0)
        epistemic = 0
        feasibility_delta = 0
        exploration = 0
        reliability = 0
        if after.informative_actions > before.informative_actions:
            delta = after.informative_actions - before.informative_actions
            reasons.append(f"{delta} new discriminative result(s) landed")
            epistemic += delta
        if after.frontier_revision > before.frontier_revision:
            delta = after.frontier_revision - before.frontier_revision
            reasons.append(f"frontier understanding advanced by {delta} revision(s)")
            epistemic += delta
        if after.objective_improvements > before.objective_improvements:
            delta = after.objective_improvements - before.objective_improvements
            reasons.append(f"{delta} runtime objective improvement(s) landed")
            quality += delta
        if after.release_blockers < before.release_blockers:
            delta = before.release_blockers - after.release_blockers
            reasons.append(f"release-blocking debt decreased by {delta}")
            feasibility_delta += delta
        if after.high_impact_issues < before.high_impact_issues:
            delta = before.high_impact_issues - after.high_impact_issues
            reasons.append(f"high-impact issue debt decreased by {delta}")
            feasibility_delta += delta
        if after.active_cruxes < before.active_cruxes:
            delta = before.active_cruxes - after.active_cruxes
            reasons.append(f"active crux count decreased by {delta}")
            feasibility_delta += delta
        if after.productive_discoveries > before.productive_discoveries:
            delta = after.productive_discoveries - before.productive_discoveries
            reasons.append(f"{delta} productive mechanism discovery result(s) landed")
            exploration += delta
        if after.scoped_evidence > before.scoped_evidence:
            delta = after.scoped_evidence - before.scoped_evidence
            reasons.append(f"scoped evidence coverage increased by {delta}")
            reliability += delta
        feasibility = (
            feasibility_delta
            if feasibility_delta and any((quality, epistemic, exploration, reliability))
            else 0
        )
        if feasibility_delta and not feasibility:
            reasons.append(
                "debt-state movement lacked a new causal observation or accepted artifact "
                "effect, so it did not mint compute"
            )
        vector = ProgressVector(
            quality=quality,
            epistemic=epistemic,
            feasibility=feasibility,
            exploration=exploration,
            reliability=reliability,
            calls_spent=max(0, after.calls - before.calls),
            input_tokens_spent=max(0, after.input_tokens - before.input_tokens),
            output_tokens_spent=max(0, after.output_tokens - before.output_tokens),
            wall_seconds_spent=max(0.0, after.wall_seconds - before.wall_seconds),
        )
        return vector, reasons

    def decide(
        self,
        state: RunState,
        resource: ResourceState,
        *,
        actionable_actions: int,
        active_commitments: int = 0,
    ) -> tuple[ResourceDecision, ResourceState]:
        snapshot = self.snapshot(state)
        progress_vector, progress = self._progress(resource.last_snapshot, snapshot)
        gradient_score = sum(
            value > 0
            for value in (
                progress_vector.quality,
                progress_vector.epistemic,
                progress_vector.feasibility,
                progress_vector.exploration,
                progress_vector.reliability,
            )
        )
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
            meaningful_progress = progress_vector.productive
            stagnant = 0 if meaningful_progress else resource.stagnant_grants + 1
            commitment_alive = active_commitments > 0
            gradient_alive = (
                meaningful_progress
                or commitment_alive
                or (debt > 0 and stagnant <= stagnation_patience)
            )
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
                    else (
                        "a validated bounded continuation earns its next predicted step"
                        if commitment_alive
                        else "unresolved material debt earns a bounded exploration grace"
                    )
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
            active_commitments=active_commitments,
            gradient_score=gradient_score,
            progress_vector=progress_vector,
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
