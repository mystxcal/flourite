"""Fresh semantic checkpoint: integrate evidence and choose the next frontier."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .. import events as et
from ..adapters.base import CallWorkspace
from ..cognition import (
    admit_overlays,
    admit_substrate_entries,
    apply_crux_updates,
    apply_obligation_updates,
    ceiling_trigger_reasons,
    charter_change_requires_witness,
    fallback_spine,
    instantiate_cruxes,
    instantiate_obligations,
    reactivate_cruxes_for_open_obligations,
    reconcile_artifact_spine,
    reconcile_frontier_kernel,
    validate_reframe,
)
from ..errors import FrontierError
from ..execution.calls import CallTrace
from ..ids import new_id
from ..models import (
    ActionKind,
    ActionProposal,
    ActionReceipt,
    ActionSpec,
    ActionStatus,
    ArtifactRef,
    CheckpointOutput,
    CognitiveTopology,
    CostBand,
    Crux,
    CruxStatus,
    DiscoveryRecord,
    EpistemicMode,
    Impact,
    IndependenceClass,
    Issue,
    IssueStatus,
    Obligation,
    ObligationStatus,
    Role,
    SpeculativeOverlay,
    SubstrateEntry,
    SummitLineage,
    TaskCharter,
    ValueBand,
)
from ..prompts import checkpoint_prompt
from ..providers import ProviderCallResult
from ..util import canonical_json, normalize_key, unique_preserving_order, utc_now

if TYPE_CHECKING:
    from ..engine import FrontierEngine

IMPACT_RANK = {Impact.LOW: 1, Impact.MEDIUM: 2, Impact.HIGH: 3, Impact.FATAL: 4}


class CheckpointExecutor:
    """Turn completed work into one authoritative semantic transition."""

    async def execute(
        self,
        engine: FrontierEngine,
        action_ids: Sequence[str],
        round_index: int,
    ) -> bool:
        repairing_verification = engine.state.runtime.verification.replan_pending
        if not engine._can_call():
            return False
        call_id = new_id("call")
        engine._append(
            et.CHECKPOINT_STARTED,
            {
                "call_id": call_id,
                "round_index": round_index,
                "action_ids": list(action_ids),
                "started_at": utc_now(),
            },
            actor="controller",
        )
        current = engine.state.current_artifact
        if current is None:
            raise FrontierError("Checkpoint cannot run without a current artifact")
        workspace = engine.adapter.open_call(
            call_id=call_id,
            call_kind="checkpoint",
            current_artifact=current,
        )
        try:
            completed = [
                action_id
                for action_id in action_ids
                if action_id in engine.state.actions
                and engine.state.actions[action_id].status == ActionStatus.COMPLETE
            ]
            stalled_targets = {
                target: count
                for target, count in engine._target_stalls().items()
                if count >= engine.config.frontier.max_stalled_actions_per_target
            }
            checkpoint_notes = ""
            if stalled_targets:
                checkpoint_notes = (
                    "Diminishing-return boundary reached for these semantic targets: "
                    + "; ".join(
                        f"{target} ({count} non-informative attempts)"
                        for target, count in sorted(stalled_targets.items())
                    )
                    + ". Do not propose another local mutation on them. Reopen the causal "
                    "model through a reframe, reconstruction, ceiling audit, or mechanism graft."
                )
            if engine.state.runtime.bootstrap.independent_checkpoint_required:
                checkpoint_notes += (
                    "\nIndependent stage gate: the construction Lead produced a whole-artifact "
                    "or release-scope bootstrap. Audit its load-bearing architecture before "
                    "hardening it. If a representative slice could still falsify the direction, "
                    "schedule that discriminator rather than polishing or extending the whole."
                )
            if replan := engine.state.runtime.planning.frontier_replan_pending:
                checkpoint_notes += (
                    "\nPlanner deadlock: the prior action slate produced no executable action. "
                    "Do not repeat, rename, or defer the same slate. Preserve the unresolved debt "
                    "and propose a materially executable route, or identify a genuine external "
                    f"blocker. Scheduler evidence: {canonical_json(replan)}"
                )
            if recovery := engine.state.runtime.release.replan_pending:
                checkpoint_notes += (
                    "\nRelease evidence falsified an upstream commitment. Treat this as causal "
                    "evidence, not a repair checklist. Explicitly revise every matching Artifact "
                    "Spine invariant, reopen dependent obligations and cruxes, and schedule the "
                    "typed recovery route at the earliest failed boundary. Recovery evidence: "
                    f"{canonical_json(recovery.model_dump(mode='json'))}"
                )
            capsule = engine._capsules.populate(
                workspace,
                task=engine.state.source_prompt,
                state=engine.state,
                assignment=(
                    "Integrate the completed sparse batch, update only causally affected "
                    "obligations/cruxes, preserve the artifact spine, and choose the next "
                    "minimum-sufficient action slate or stop."
                ),
                goal_contract=engine.state.contract,
                evidence_action_ids=list(action_ids),
                task_source=engine.state.task_source,
                extra_notes=checkpoint_notes,
                lens_purpose="checkpoint",
            )
            synthesis_interval = engine.config.frontier.clean_synthesis_every_rounds
            force_clean = engine.state.runtime.planning.clean_synthesis_needed or bool(
                synthesis_interval and round_index > 0 and round_index % synthesis_interval == 0
            )
            fresh_keeper = engine._fresh_frontier_keeper()
            prompt = checkpoint_prompt(
                workspace,
                profile=engine._profile,
                max_issues=engine.config.frontier.max_open_issues,
                max_actions=engine.config.frontier.max_actions_per_batch * 2,
                software=engine._software,
                force_clean_synthesis=force_clean,
                adaptive=engine.config.cognition.mode == "adaptive",
                max_cruxes=engine.config.cognition.max_active_cruxes,
                normal_overlay_limit=engine.config.cognition.normal_overlay_limit,
                summit_mode=engine.config.summit.mode,
                fresh_keeper=fresh_keeper,
            )
            use_lead = (
                engine.config.cognition.mode == "adaptive"
                and engine.config.cognition.persistent_lead
                and not fresh_keeper
            )
            result, trace = await engine._invoke(
                workspace,
                call_kind="checkpoint",
                role=Role.STRONG,
                prompt=prompt,
                response_model=CheckpointOutput,
                sandbox=engine._bootstrap_sandbox(),
                network_access=engine.config.provider.default_network_access,
                image_paths=[Path(item) for item in cast(list[str], capsule["image_paths"])],
                metadata={
                    "current_artifact_text": engine.adapter.artifact_text(current),
                    "open_issue_ids": [issue.issue_id for issue in engine.state.open_issues],
                    "open_obligation_ids": [
                        item.obligation_id for item in engine.state.open_obligations
                    ],
                    "active_crux_ids": [item.crux_id for item in engine.state.active_cruxes],
                    "completed_action_ids": completed,
                    "task_source_digest": (
                        engine.state.task_source.digest if engine.state.task_source else None
                    ),
                },
                use_lead=use_lead,
            )
            return self._integrate(
                engine=engine,
                call_id=call_id,
                output=result.response,
                completed=completed,
                current=current,
                workspace=workspace,
                round_index=round_index,
                result=result,
                trace=trace,
                use_lead=use_lead,
                repairing_verification=repairing_verification,
            )
        except BaseException as exc:
            usage, trace = engine._failure_parts(exc)
            recovery_artifact, recovery_capture_error = engine._capture_recovery_artifact(
                workspace,
                summary="Interrupted checkpoint workspace.",
                parent=current,
                source_action_ids=list(action_ids),
            )
            engine._append(
                et.CHECKPOINT_FAILED,
                {
                    "call_id": call_id,
                    "round_index": round_index,
                    "action_ids": list(action_ids),
                    "error": f"{type(exc).__name__}: {exc}",
                    "usage": usage.model_dump(mode="json"),
                    "recovery_artifact": (
                        recovery_artifact.model_dump(mode="json") if recovery_artifact else None
                    ),
                    "recovery_capture_error": recovery_capture_error,
                    **trace.payload(),
                },
                actor="controller",
            )
            if engine.config.run.fail_fast_on_provider_error:
                raise
            return False
        finally:
            engine._close_workspace(workspace)

    def _integrate(
        self,
        *,
        engine: FrontierEngine,
        call_id: str,
        output: CheckpointOutput,
        completed: list[str],
        current: ArtifactRef,
        workspace: CallWorkspace,
        round_index: int,
        result: ProviderCallResult[CheckpointOutput],
        trace: CallTrace,
        use_lead: bool,
        repairing_verification: bool,
    ) -> bool:
        accepted, rejected, receipt_updates = self._action_disposition(
            engine=engine,
            output=output,
            completed=completed,
        )
        charter, reframe_valid, reframe_notes = self._adjudicate_charter(
            engine=engine,
            output=output,
            call_id=call_id,
        )
        declared, normalization = engine._ensure_artifact_file(
            workspace,
            declared_path=output.artifact_path,
            summary=output.artifact_summary,
            current_artifact=current,
        )
        artifact = engine.adapter.capture_artifact(
            workspace,
            declared_path=declared,
            version=current.version + 1,
            summary=output.artifact_summary,
            parent=current,
            source_action_ids=accepted,
        )

        upserts, new_keymap, issue_notes = engine._apply_issue_updates(
            output.issue_updates,
            output.new_issues,
        )
        issue_keymap = engine._issue_local_keys([*engine.state.issues.values(), *upserts])
        issue_keymap.update(new_keymap)

        adaptive_mode = engine.config.cognition.mode == "adaptive"
        projected_obligations, obligation_notes = apply_obligation_updates(
            engine.state.obligations if adaptive_mode else {},
            output.obligation_updates if adaptive_mode else [],
            updated_seq=engine.journal.count() + 1,
        )
        new_obligations, obligation_keymap, obligation_admission = instantiate_obligations(
            output.new_obligations if adaptive_mode else [],
            existing=projected_obligations.values(),
            capacity=max(
                32,
                len(projected_obligations)
                + len(output.new_obligations if adaptive_mode else []),
            ),
            created_seq=engine.journal.count() + 1,
            charter=charter,
            human_evidence_available=engine.config.cognition.human_evidence_available,
        )
        projected_obligations.update({item.obligation_id: item for item in new_obligations})
        obligation_keymap = engine._obligation_local_keys(projected_obligations.values())
        obligation_upserts = cast(
            list[Obligation],
            engine._changed_models(engine.state.obligations, projected_obligations),
        )

        projected_cruxes, crux_notes = apply_crux_updates(
            engine.state.cruxes if adaptive_mode else {},
            output.crux_updates if adaptive_mode else [],
            updated_seq=engine.journal.count() + 1,
            active_limit=engine.config.cognition.max_active_cruxes,
        )
        projected_cruxes, obligation_recompile_notes = reactivate_cruxes_for_open_obligations(
            projected_cruxes,
            projected_obligations,
            updated_seq=engine.journal.count() + 1,
            active_limit=engine.config.cognition.max_active_cruxes,
        )
        crux_notes.extend(obligation_recompile_notes)
        new_cruxes, crux_keymap, crux_admission = instantiate_cruxes(
            output.new_cruxes if adaptive_mode else [],
            obligations=projected_obligations.values(),
            existing=projected_cruxes.values(),
            active_limit=engine.config.cognition.max_active_cruxes,
            total_limit=max(8, engine.config.cognition.max_active_cruxes * 4),
            created_seq=engine.journal.count() + 1,
        )
        projected_cruxes.update({item.crux_id: item for item in new_cruxes})
        active_now = [
            item for item in projected_cruxes.values() if item.status == CruxStatus.ACTIVE
        ]
        if adaptive_mode and len(active_now) < engine.config.cognition.max_active_cruxes:
            dormant = sorted(
                (
                    item
                    for item in projected_cruxes.values()
                    if item.status == CruxStatus.DORMANT
                ),
                key=lambda item: (
                    -IMPACT_RANK[item.unlock_value],
                    item.created_seq,
                    item.crux_id,
                ),
            )
            for item in dormant[: engine.config.cognition.max_active_cruxes - len(active_now)]:
                item.status = CruxStatus.ACTIVE
                item.updated_seq = engine.journal.count() + 1
                crux_notes.append(f"promoted dormant crux: {item.crux_id}")
        crux_keymap = engine._crux_local_keys(projected_cruxes.values())
        crux_upserts = cast(
            list[Crux], engine._changed_models(engine.state.cruxes, projected_cruxes)
        )

        projected_substrate, substrate_admission = admit_substrate_entries(
            output.substrate_entries if adaptive_mode else [],
            existing=engine.state.substrate if adaptive_mode else {},
        )
        substrate_upserts = cast(
            list[SubstrateEntry],
            engine._changed_models(engine.state.substrate, projected_substrate),
        )
        projected_overlays, overlay_admission = admit_overlays(
            output.overlays if adaptive_mode else [],
            existing=engine.state.overlays if adaptive_mode else {},
            normal_limit=engine.config.cognition.normal_overlay_limit,
            hard_limit=engine.config.cognition.hard_overlay_limit,
            require_behavioral_difference=(
                engine.config.cognition.require_behavioral_overlay_difference
            ),
        )
        overlay_upserts = cast(
            list[SpeculativeOverlay],
            engine._changed_models(engine.state.overlays, projected_overlays),
        )

        reasons = unique_preserving_order(
            [*engine.state.summit_reasons, *ceiling_trigger_reasons(output.ceiling_scan)]
        )
        summit_active = engine.state.summit_active
        if engine.config.cognition.mode == "adaptive":
            if engine.config.summit.mode == "on":
                summit_active = True
                reasons = unique_preserving_order(["summit.mode=on", *reasons])
            elif engine.config.summit.mode == "auto" and output.ceiling_scan:
                summit_active = summit_active or bool(
                    ceiling_trigger_reasons(output.ceiling_scan)
                    and (
                        not engine.config.summit.require_concrete_auto_trigger
                        or output.ceiling_scan.concrete_trigger
                    )
                )

        projected_lineages = dict(engine.state.summit_lineages)
        lineage_admission: dict[str, Any] = {}
        if summit_active and output.lineages:
            checkpoint_lineages = []
            for lineage_output in output.lineages:
                incumbent = engine.state.summit_lineages.get(lineage_output.lineage_id)
                checkpoint_lineages.append(
                    lineage_output.model_copy(
                        update={
                            "candidate_artifact": (
                                incumbent.candidate_artifact if incumbent else None
                            )
                        }
                    )
                )
            projected_lineages, decision = engine.summit_archive.admit(
                engine.state.summit_lineages, checkpoint_lineages
            )
            lineage_admission = {
                "accepted": decision.accepted,
                "replaced": decision.replaced,
                "rejected": decision.rejected,
                "demoted": decision.demoted,
            }
        lineage_upserts = cast(
            list[SummitLineage],
            engine._changed_models(engine.state.summit_lineages, projected_lineages),
        )
        projected_discovery = engine.experimental_frontier.seed_records(
            projected_lineages,
            engine.state.discovery_records,
        )
        projected_discovery = engine.experimental_frontier.integrate(
            projected_discovery,
            engine.state.actions,
            accepted,
        )
        discovery_upserts = cast(
            list[DiscoveryRecord],
            engine._changed_models(engine.state.discovery_records, projected_discovery),
        )

        spine, spine_notes = reconcile_artifact_spine(
            engine.state.artifact_spine, output.artifact_spine
        )
        if spine is None and engine.state.contract is not None:
            spine = fallback_spine(engine.state.contract, output.artifact_summary)

        proposals = list(output.actions)
        engine._ensure_fresh_global_review(
            proposals,
            accepted_action_ids=accepted,
        )
        if (
            summit_active
            and not projected_lineages
            and not any(item.topology == CognitiveTopology.SUMMIT for item in proposals)
        ):
            target_cruxes = [
                item.crux_id
                for item in projected_cruxes.values()
                if item.status == CruxStatus.ACTIVE
            ][:1]
            target_obligations = [
                item.obligation_id
                for item in projected_obligations.values()
                if item.release_blocking
                and item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
            ][:2]
            proposals.append(
                ActionProposal(
                    kind=ActionKind.EXPLORE,
                    target="exact-task upper-tail mechanism search",
                    assignment=(
                        "Seed at most two genuinely mechanismally distinct Summit lineages for the exact immutable task. "
                        "Name concrete mechanisms, assumptions, discriminating predictions, next dependencies, and bounded kill conditions."
                    ),
                    obligation_ids=target_obligations,
                    crux_ids=target_cruxes,
                    impact=Impact.HIGH,
                    cost=CostBand.MODERATE,
                    independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
                    topology=CognitiveTopology.SUMMIT,
                    epistemic_mode=EpistemicMode.THINK,
                    expected_decision_effect=(
                        "Either establish a viable upper-tail mechanism or close the concrete ceiling risk with scoped negative evidence."
                    ),
                    reusable_value=ValueBand.HIGH,
                    distinctive_angle="mechanism-level exact-task support expansion",
                    summit_reason=reasons[0] if reasons else "active Summit capability",
                )
            )
        elif (
            summit_active
            and projected_lineages
            and engine.config.summit.experimental_frontier
            and not any(item.topology == CognitiveTopology.SUMMIT for item in proposals)
        ):
            active_crux_ids = [
                item.crux_id
                for item in projected_cruxes.values()
                if item.status == CruxStatus.ACTIVE
            ][:1]
            blocking_obligation_ids = [
                item.obligation_id
                for item in projected_obligations.values()
                if item.release_blocking
                and item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
            ][:2]
            plans = engine.experimental_frontier.select(
                projected_lineages,
                projected_discovery,
                limit=engine.config.summit.max_discovery_actions_per_round,
            )
            proposals.extend(
                engine.experimental_frontier.to_action(
                    plan,
                    crux_ids=active_crux_ids,
                    obligation_ids=blocking_obligation_ids,
                    summit_reason=reasons[0] if reasons else "active Summit capability",
                )
                for plan in plans
            )
        if not proposals and summit_active and not engine.config.summit.experimental_frontier:
            development = engine.summit_archive.select_development_batch(
                projected_lineages, limit=1
            )
            if development:
                lineage = development[0]
                active_crux_ids = [
                    item.crux_id
                    for item in projected_cruxes.values()
                    if item.status == CruxStatus.ACTIVE
                ][:1]
                proposals.append(
                    ActionProposal(
                        kind=ActionKind.EXPLORE,
                        target=lineage.name,
                        assignment=(
                            "Develop this exact-task Summit lineage only through its next unresolved dependency or decisive falsifier: "
                            + (
                                lineage.unresolved_questions[0]
                                if lineage.unresolved_questions
                                else lineage.mechanism
                            )
                        ),
                        crux_ids=active_crux_ids,
                        impact=Impact.HIGH,
                        cost=CostBand.MODERATE,
                        independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
                        topology=CognitiveTopology.SUMMIT,
                        epistemic_mode=EpistemicMode.THINK,
                        expected_decision_effect=(
                            "Either mature the lineage into a viable mechanism, extract reusable residue, or falsify it within its bounded unlock contract."
                        ),
                        reusable_value=ValueBand.HIGH,
                        distinctive_angle=lineage.mechanism,
                        lineage_id=lineage.lineage_id,
                        summit_reason=reasons[0] if reasons else "explicit Summit mode",
                    )
                )

        if not proposals:
            proposals.extend(
                engine._fallback_action_proposals(
                    obligations=projected_obligations, cruxes=projected_cruxes
                )
            )

        frontier_kernel, frontier_notes = reconcile_frontier_kernel(
            engine.state.frontier_kernel,
            output.frontier_kernel,
            cruxes=list(projected_cruxes.values()),
            spine=spine,
            next_actions=proposals,
            round_index=round_index,
            eligible_action_ids=completed,
        )
        engine._ensure_frame_pressure(
            proposals,
            obligations=projected_obligations,
            cruxes=projected_cruxes,
            frontier_kernel=frontier_kernel,
        )
        actions, action_contracts, dropped_actions = engine._instantiate_actions(
            proposals,
            issue_keymap=issue_keymap,
            obligation_keymap=obligation_keymap,
            crux_keymap=crux_keymap,
            round_index=round_index + 1,
        )

        stop, stop_reason = self._stop_decision(
            engine=engine,
            output=output,
            actions=actions,
            issue_upserts=upserts,
            projected_obligations=projected_obligations,
            projected_cruxes=projected_cruxes,
        )
        engine._append(
            et.CHECKPOINT_COMPLETED,
            {
                "call_id": call_id,
                "artifact": artifact.model_dump(mode="json"),
                "task_charter": charter.model_dump(mode="json") if charter else None,
                "artifact_spine": spine.model_dump(mode="json") if spine else None,
                "frontier_kernel": frontier_kernel.model_dump(mode="json"),
                "issue_upserts": [item.model_dump(mode="json") for item in upserts],
                "obligation_upserts": [
                    item.model_dump(mode="json") for item in obligation_upserts
                ],
                "crux_upserts": [item.model_dump(mode="json") for item in crux_upserts],
                "substrate_entries": [
                    item.model_dump(mode="json") for item in substrate_upserts
                ],
                "overlays": [item.model_dump(mode="json") for item in overlay_upserts],
                "lineages": [item.model_dump(mode="json") for item in lineage_upserts],
                "discovery_records": [
                    item.model_dump(mode="json") for item in discovery_upserts
                ],
                "summit_active": summit_active,
                "summit_reasons": reasons,
                "accepted_action_ids": accepted,
                "rejected_action_ids": unique_preserving_order(rejected),
                "receipt_updates": [item.model_dump(mode="json") for item in receipt_updates],
                "actions": [item.model_dump(mode="json") for item in actions],
                "action_contracts": [item.model_dump(mode="json") for item in action_contracts],
                "lead_session": (
                    engine.state.lead_session.model_dump(mode="json") if use_lead else None
                ),
                "stop_requested": stop,
                "stop_reason": stop_reason,
                "frame_break": output.frame_break if reframe_valid else None,
                "clean_synthesis_needed": output.clean_synthesis_needed,
                "completed_round_index": round_index,
                "usage": result.usage.model_dump(mode="json"),
                "normalization_notes": normalization,
                "issue_update_notes": issue_notes,
                "obligation_update_notes": obligation_notes,
                "crux_update_notes": crux_notes,
                "obligation_admission": asdict(obligation_admission),
                "crux_admission": asdict(crux_admission),
                "substrate_admission": asdict(substrate_admission),
                "overlay_admission": asdict(overlay_admission),
                "lineage_admission": lineage_admission,
                "reframe_notes": reframe_notes,
                "dropped_action_proposals": dropped_actions,
                "frontier_kernel_notes": asdict(frontier_notes),
                "artifact_spine_notes": spine_notes,
                "ceiling_scan": (
                    output.ceiling_scan.model_dump(mode="json") if output.ceiling_scan else None
                ),
                **trace.payload(),
            },
            actor="lead" if use_lead else "controller",
        )
        engine._append(
            et.ROUND_COMPLETED,
            {
                "round_index": round_index,
                "accepted_action_ids": accepted,
                "rejected_action_ids": unique_preserving_order(rejected),
            },
            actor="controller",
        )
        engine._record_staged_checks(stage="preflight")
        engine._record_staged_checks(stage="candidate")
        if repairing_verification and engine.state.runtime.verification.replan_pending:
            corrective_actions = bool(engine.state.pending_action_ids)
            preflight = engine.state.runtime.verification.stages.get("preflight")
            candidate = engine.state.runtime.verification.stages.get("candidate")
            failures = unique_preserving_order(
                [
                    *(preflight.failures if preflight else []),
                    *(candidate.failures if candidate else []),
                ]
            )
            engine._append(
                et.CHECK_REPLAN_DECIDED,
                {
                    "decision": ("corrective_actions" if corrective_actions else "dead_end"),
                    "failures": failures,
                    "action_ids": list(engine.state.pending_action_ids),
                },
                actor="controller",
            )
        return True

    @staticmethod
    def _action_disposition(
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
        completed: list[str],
    ) -> tuple[list[str], list[str], list[ActionReceipt]]:
        accepted = [item for item in output.accepted_action_ids if item in completed]
        rejected = [
            item
            for item in output.rejected_action_ids
            if item in completed and item not in accepted
        ]
        rejected.extend(
            item for item in completed if item not in accepted and item not in rejected
        )
        receipts = []
        for action_id in completed:
            record = engine.state.actions.get(action_id)
            if record is None or record.receipt is None:
                continue
            receipts.append(
                record.receipt.model_copy(
                    update={
                        "integration_status": (
                            "accepted" if action_id in accepted else "rejected"
                        )
                    }
                )
            )
        return accepted, rejected, receipts

    @staticmethod
    def _stop_decision(
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
        actions: list[ActionSpec],
        issue_upserts: list[Issue],
        projected_obligations: dict[str, Obligation],
        projected_cruxes: dict[str, Crux],
    ) -> tuple[bool, str | None]:
        projected_issues = dict(engine.state.issues)
        projected_issues.update({item.issue_id: item for item in issue_upserts})
        unresolved = any(
            issue.status == IssueStatus.OPEN and issue.impact in {Impact.FATAL, Impact.HIGH}
            for issue in projected_issues.values()
        )
        unresolved = unresolved or any(
            item.release_blocking
            and item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
            for item in projected_obligations.values()
        )
        unresolved = unresolved or any(
            item.status == CruxStatus.ACTIVE for item in projected_cruxes.values()
        )
        unresolved = unresolved or any(
            action.topology == CognitiveTopology.SUMMIT for action in actions
        )

        if actions and output.stop and unresolved:
            return False, None
        if not actions and not unresolved:
            return True, output.stop_reason or (
                "No high-impact issue, release-blocking obligation, active crux, "
                "or decision-relevant next action remains."
            )
        if not actions and unresolved:
            return False, None
        return bool(output.stop), output.stop_reason

    @staticmethod
    def _adjudicate_charter(
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
        call_id: str,
    ) -> tuple[TaskCharter | None, bool, list[str]]:
        valid = True
        notes: list[str] = []
        current = engine.state.task_charter
        witness = output.reframe_witness
        if witness is not None and current is not None:
            valid, notes = validate_reframe(witness, charter=current)
            engine._append(
                et.REFRAME_ADMITTED if valid else et.REFRAME_REJECTED,
                {
                    "witness": witness.model_dump(mode="json"),
                    "problems": notes,
                    "call_id": call_id,
                },
                actor="controller",
            )
        elif (
            output.frame_break or output.task_charter is not None
        ) and engine.config.cognition.require_reframe_witness and charter_change_requires_witness(
            current, output.task_charter
        ):
            valid = False
            notes.append("material charter change omitted a reframe witness")
            engine._append(
                et.REFRAME_REJECTED,
                {"problems": notes, "call_id": call_id},
                actor="controller",
            )

        if output.task_charter is None or not valid:
            return current, valid, notes
        proposed = output.task_charter.model_copy(deep=True)
        source_digest = engine.state.task_source.digest if engine.state.task_source else None
        if source_digest and proposed.source_digest != source_digest:
            notes.append("task charter digest mismatch; prior charter preserved")
            return current, valid, notes
        if current is not None:
            proposed.hard_constraints = unique_preserving_order(
                [*current.hard_constraints, *proposed.hard_constraints]
            )
            proposed.unacceptable_failures = unique_preserving_order(
                [*current.unacceptable_failures, *proposed.unacceptable_failures]
            )
            proposed.evidence_requirements = unique_preserving_order(
                [*current.evidence_requirements, *proposed.evidence_requirements]
            )
            traces = {item.requirement_id: item for item in current.requirement_traces}
            traces.update({item.requirement_id: item for item in proposed.requirement_traces})
            proposed.requirement_traces = list(traces.values())
        old_constraints = {
            normalize_key(item) for item in (current.hard_constraints if current else [])
        }
        new_constraints = {normalize_key(item) for item in proposed.hard_constraints}
        if not old_constraints.issubset(new_constraints):
            notes.append(
                "task charter attempted to drop an existing hard constraint; prior charter preserved"
            )
            return current, valid, notes
        proposed.revision = max(proposed.revision, (current.revision + 1) if current else 1)
        return proposed, valid, notes
