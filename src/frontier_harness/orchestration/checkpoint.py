"""Fresh semantic checkpoint: integrate evidence and choose the next frontier."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .. import events as et
from ..adapters.base import CallWorkspace
from ..capsule import CapsuleSpec
from ..cognition import (
    AdmissionNotes,
    FrontierKernelNotes,
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
    ActionContract,
    ActionKind,
    ActionProposal,
    ActionReceipt,
    ActionSpec,
    ActionStatus,
    ArtifactRef,
    ArtifactSpine,
    CheckpointOutput,
    CognitiveTopology,
    CostBand,
    Crux,
    CruxStatus,
    DiscoveryRecord,
    EpistemicMode,
    FrontierKernel,
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


@dataclass(slots=True)
class SemanticProjection:
    """Causal state changes proposed by one checkpoint, before the ledger commit."""

    issue_upserts: list[Issue]
    issue_keymap: dict[str, str]
    issue_notes: list[str]
    obligations: dict[str, Obligation]
    obligation_upserts: list[Obligation]
    obligation_keymap: dict[str, str]
    obligation_notes: list[str]
    obligation_admission: AdmissionNotes
    cruxes: dict[str, Crux]
    crux_upserts: list[Crux]
    crux_keymap: dict[str, str]
    crux_notes: list[str]
    crux_admission: AdmissionNotes
    substrate_upserts: list[SubstrateEntry]
    substrate_admission: AdmissionNotes
    overlay_upserts: list[SpeculativeOverlay]
    overlay_admission: AdmissionNotes


@dataclass(slots=True)
class SummitProjection:
    active: bool
    reasons: list[str]
    lineages: dict[str, SummitLineage]
    lineage_upserts: list[SummitLineage]
    lineage_admission: dict[str, Any]
    discovery: dict[str, DiscoveryRecord]
    discovery_upserts: list[DiscoveryRecord]


@dataclass(slots=True)
class CheckpointPlan:
    spine: ArtifactSpine | None
    spine_notes: list[str]
    frontier_kernel: FrontierKernel
    frontier_notes: FrontierKernelNotes
    actions: list[ActionSpec]
    action_contracts: list[ActionContract]
    dropped_actions: list[str]
    stop: bool
    stop_reason: str | None


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
                    "blocker. Scheduler evidence: "
                    f"{canonical_json(replan.model_dump(mode='json'))}"
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
                CapsuleSpec(
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
                    purpose="checkpoint",
                ),
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

        semantic = self._project_semantics(
            engine=engine,
            output=output,
            charter=charter,
        )

        summit = self._project_summit(
            engine=engine,
            output=output,
            accepted=accepted,
        )

        plan = self._build_plan(
            engine=engine,
            output=output,
            accepted=accepted,
            completed=completed,
            round_index=round_index,
            semantic=semantic,
            summit=summit,
        )
        engine._append(
            et.CHECKPOINT_COMPLETED,
            self._checkpoint_payload(
                engine=engine,
                call_id=call_id,
                output=output,
                artifact=artifact,
                charter=charter,
                reframe_valid=reframe_valid,
                reframe_notes=reframe_notes,
                normalization=normalization,
                accepted=accepted,
                rejected=rejected,
                receipt_updates=receipt_updates,
                semantic=semantic,
                summit=summit,
                plan=plan,
                round_index=round_index,
                result=result,
                trace=trace,
                use_lead=use_lead,
            ),
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

    def _project_semantics(
        self,
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
        charter: TaskCharter | None,
    ) -> SemanticProjection:
        adaptive = engine.config.cognition.mode == "adaptive"
        next_seq = engine.journal.count() + 1
        issue_upserts, new_issue_keys, issue_notes = engine._apply_issue_updates(
            output.issue_updates,
            output.new_issues,
        )
        issue_keymap = engine._issue_local_keys([*engine.state.issues.values(), *issue_upserts])
        issue_keymap.update(new_issue_keys)
        (
            obligations,
            obligation_upserts,
            obligation_keymap,
            obligation_notes,
            obligation_admission,
        ) = self._project_obligations(
            engine=engine,
            output=output,
            charter=charter,
            adaptive=adaptive,
            next_seq=next_seq,
        )
        (
            cruxes,
            crux_upserts,
            crux_keymap,
            crux_notes,
            crux_admission,
        ) = self._project_cruxes(
            engine=engine,
            output=output,
            obligations=obligations,
            adaptive=adaptive,
            next_seq=next_seq,
        )
        projected_substrate, substrate_admission = admit_substrate_entries(
            output.substrate_entries if adaptive else [],
            existing=engine.state.substrate if adaptive else {},
        )
        projected_overlays, overlay_admission = admit_overlays(
            output.overlays if adaptive else [],
            existing=engine.state.overlays if adaptive else {},
            normal_limit=engine.config.cognition.normal_overlay_limit,
            hard_limit=engine.config.cognition.hard_overlay_limit,
            require_behavioral_difference=(
                engine.config.cognition.require_behavioral_overlay_difference
            ),
        )
        return SemanticProjection(
            issue_upserts=issue_upserts,
            issue_keymap=issue_keymap,
            issue_notes=issue_notes,
            obligations=obligations,
            obligation_upserts=obligation_upserts,
            obligation_keymap=obligation_keymap,
            obligation_notes=obligation_notes,
            obligation_admission=obligation_admission,
            cruxes=cruxes,
            crux_upserts=crux_upserts,
            crux_keymap=crux_keymap,
            crux_notes=crux_notes,
            crux_admission=crux_admission,
            substrate_upserts=cast(
                list[SubstrateEntry],
                engine._changed_models(engine.state.substrate, projected_substrate),
            ),
            substrate_admission=substrate_admission,
            overlay_upserts=cast(
                list[SpeculativeOverlay],
                engine._changed_models(engine.state.overlays, projected_overlays),
            ),
            overlay_admission=overlay_admission,
        )

    @staticmethod
    def _project_obligations(
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
        charter: TaskCharter | None,
        adaptive: bool,
        next_seq: int,
    ) -> tuple[
        dict[str, Obligation],
        list[Obligation],
        dict[str, str],
        list[str],
        AdmissionNotes,
    ]:
        projected, notes = apply_obligation_updates(
            engine.state.obligations if adaptive else {},
            output.obligation_updates if adaptive else [],
            updated_seq=next_seq,
        )
        created, _, admission = instantiate_obligations(
            output.new_obligations if adaptive else [],
            existing=projected.values(),
            capacity=max(
                32,
                len(projected) + len(output.new_obligations if adaptive else []),
            ),
            created_seq=next_seq,
            charter=charter,
            human_evidence_available=engine.config.cognition.human_evidence_available,
        )
        projected.update({item.obligation_id: item for item in created})
        return (
            projected,
            cast(
                list[Obligation],
                engine._changed_models(engine.state.obligations, projected),
            ),
            engine._obligation_local_keys(projected.values()),
            notes,
            admission,
        )

    def _project_cruxes(
        self,
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
        obligations: dict[str, Obligation],
        adaptive: bool,
        next_seq: int,
    ) -> tuple[dict[str, Crux], list[Crux], dict[str, str], list[str], AdmissionNotes]:
        projected, notes = apply_crux_updates(
            engine.state.cruxes if adaptive else {},
            output.crux_updates if adaptive else [],
            updated_seq=next_seq,
            active_limit=engine.config.cognition.max_active_cruxes,
        )
        projected, recompile_notes = reactivate_cruxes_for_open_obligations(
            projected,
            obligations,
            updated_seq=next_seq,
            active_limit=engine.config.cognition.max_active_cruxes,
        )
        notes.extend(recompile_notes)
        created, _, admission = instantiate_cruxes(
            output.new_cruxes if adaptive else [],
            obligations=obligations.values(),
            existing=projected.values(),
            active_limit=engine.config.cognition.max_active_cruxes,
            total_limit=max(8, engine.config.cognition.max_active_cruxes * 4),
            created_seq=next_seq,
        )
        projected.update({item.crux_id: item for item in created})
        if adaptive:
            self._promote_dormant_cruxes(
                projected,
                notes=notes,
                active_limit=engine.config.cognition.max_active_cruxes,
                updated_seq=next_seq,
            )
        return (
            projected,
            cast(list[Crux], engine._changed_models(engine.state.cruxes, projected)),
            engine._crux_local_keys(projected.values()),
            notes,
            admission,
        )

    @staticmethod
    def _promote_dormant_cruxes(
        cruxes: dict[str, Crux],
        *,
        notes: list[str],
        active_limit: int,
        updated_seq: int,
    ) -> None:
        active_count = sum(item.status == CruxStatus.ACTIVE for item in cruxes.values())
        dormant = sorted(
            (item for item in cruxes.values() if item.status == CruxStatus.DORMANT),
            key=lambda item: (-IMPACT_RANK[item.unlock_value], item.created_seq, item.crux_id),
        )
        for item in dormant[: max(0, active_limit - active_count)]:
            item.status = CruxStatus.ACTIVE
            item.updated_seq = updated_seq
            notes.append(f"promoted dormant crux: {item.crux_id}")

    @staticmethod
    def _summit_activation(
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
    ) -> tuple[bool, list[str]]:
        triggers = ceiling_trigger_reasons(output.ceiling_scan)
        reasons = unique_preserving_order([*engine.state.summit_reasons, *triggers])
        active = engine.state.summit_active
        if engine.config.cognition.mode != "adaptive":
            return active, reasons
        if engine.config.summit.mode == "on":
            return True, unique_preserving_order(["summit.mode=on", *reasons])
        if engine.config.summit.mode == "auto" and output.ceiling_scan:
            concrete = (
                not engine.config.summit.require_concrete_auto_trigger
                or output.ceiling_scan.concrete_trigger
            )
            active = active or bool(triggers and concrete)
        return active, reasons

    def _project_summit(
        self,
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
        accepted: list[str],
    ) -> SummitProjection:
        active, reasons = self._summit_activation(engine=engine, output=output)
        lineages = dict(engine.state.summit_lineages)
        admission: dict[str, Any] = {}
        if active and output.lineages:
            candidates = []
            for candidate in output.lineages:
                incumbent = engine.state.summit_lineages.get(candidate.lineage_id)
                candidates.append(
                    candidate.model_copy(
                        update={
                            "candidate_artifact": (
                                incumbent.candidate_artifact if incumbent else None
                            )
                        }
                    )
                )
            lineages, decision = engine.summit_archive.admit(
                engine.state.summit_lineages,
                candidates,
            )
            admission = {
                "accepted": decision.accepted,
                "replaced": decision.replaced,
                "rejected": decision.rejected,
                "demoted": decision.demoted,
            }
        discovery = engine.experimental_frontier.seed_records(
            lineages,
            engine.state.discovery_records,
        )
        discovery = engine.experimental_frontier.integrate(
            discovery,
            engine.state.actions,
            accepted,
        )
        return SummitProjection(
            active=active,
            reasons=reasons,
            lineages=lineages,
            lineage_upserts=cast(
                list[SummitLineage],
                engine._changed_models(engine.state.summit_lineages, lineages),
            ),
            lineage_admission=admission,
            discovery=discovery,
            discovery_upserts=cast(
                list[DiscoveryRecord],
                engine._changed_models(engine.state.discovery_records, discovery),
            ),
        )

    def _build_plan(
        self,
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
        accepted: list[str],
        completed: list[str],
        round_index: int,
        semantic: SemanticProjection,
        summit: SummitProjection,
    ) -> CheckpointPlan:
        spine, spine_notes = reconcile_artifact_spine(
            engine.state.artifact_spine,
            output.artifact_spine,
        )
        if spine is None and engine.state.contract is not None:
            spine = fallback_spine(engine.state.contract, output.artifact_summary)
        proposals = self._build_proposals(
            engine=engine,
            output=output,
            accepted=accepted,
            semantic=semantic,
            summit=summit,
        )
        frontier_kernel, frontier_notes = reconcile_frontier_kernel(
            engine.state.frontier_kernel,
            output.frontier_kernel,
            cruxes=list(semantic.cruxes.values()),
            spine=spine,
            next_actions=proposals,
            round_index=round_index,
            eligible_action_ids=completed,
        )
        engine._ensure_frame_pressure(
            proposals,
            obligations=semantic.obligations,
            cruxes=semantic.cruxes,
            frontier_kernel=frontier_kernel,
        )
        actions, contracts, dropped = engine._instantiate_actions(
            proposals,
            issue_keymap=semantic.issue_keymap,
            obligation_keymap=semantic.obligation_keymap,
            crux_keymap=semantic.crux_keymap,
            round_index=round_index + 1,
        )
        stop, stop_reason = self._stop_decision(
            engine=engine,
            output=output,
            actions=actions,
            issue_upserts=semantic.issue_upserts,
            projected_obligations=semantic.obligations,
            projected_cruxes=semantic.cruxes,
        )
        return CheckpointPlan(
            spine=spine,
            spine_notes=spine_notes,
            frontier_kernel=frontier_kernel,
            frontier_notes=frontier_notes,
            actions=actions,
            action_contracts=contracts,
            dropped_actions=dropped,
            stop=stop,
            stop_reason=stop_reason,
        )

    def _build_proposals(
        self,
        *,
        engine: FrontierEngine,
        output: CheckpointOutput,
        accepted: list[str],
        semantic: SemanticProjection,
        summit: SummitProjection,
    ) -> list[ActionProposal]:
        proposals = list(output.actions)
        engine._ensure_fresh_global_review(proposals, accepted_action_ids=accepted)
        has_summit = any(item.topology == CognitiveTopology.SUMMIT for item in proposals)
        crux_ids, obligation_ids = self._summit_action_scope(semantic)
        reason = summit.reasons[0] if summit.reasons else "active Summit capability"
        if summit.active and not summit.lineages and not has_summit:
            proposals.append(
                self._summit_seed_action(
                    crux_ids=crux_ids,
                    obligation_ids=obligation_ids,
                    reason=reason,
                )
            )
        elif (
            summit.active
            and summit.lineages
            and engine.config.summit.experimental_frontier
            and not has_summit
        ):
            plans = engine.experimental_frontier.select(
                summit.lineages,
                summit.discovery,
                limit=engine.config.summit.max_discovery_actions_per_round,
            )
            proposals.extend(
                engine.experimental_frontier.to_action(
                    plan,
                    crux_ids=crux_ids,
                    obligation_ids=obligation_ids,
                    summit_reason=reason,
                )
                for plan in plans
            )
        if not proposals and summit.active and not engine.config.summit.experimental_frontier:
            development = engine.summit_archive.select_development_batch(
                summit.lineages,
                limit=1,
            )
            if development:
                proposals.append(
                    self._summit_development_action(
                        development[0],
                        crux_ids=crux_ids,
                        reason=reason,
                    )
                )
        if not proposals:
            proposals.extend(
                engine._fallback_action_proposals(
                    obligations=semantic.obligations,
                    cruxes=semantic.cruxes,
                )
            )
        return proposals

    @staticmethod
    def _summit_action_scope(
        semantic: SemanticProjection,
    ) -> tuple[list[str], list[str]]:
        crux_ids = [
            item.crux_id for item in semantic.cruxes.values() if item.status == CruxStatus.ACTIVE
        ][:1]
        obligation_ids = [
            item.obligation_id
            for item in semantic.obligations.values()
            if item.release_blocking
            and item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
        ][:2]
        return crux_ids, obligation_ids

    @staticmethod
    def _summit_seed_action(
        *,
        crux_ids: list[str],
        obligation_ids: list[str],
        reason: str,
    ) -> ActionProposal:
        return ActionProposal(
            kind=ActionKind.EXPLORE,
            target="exact-task upper-tail mechanism search",
            assignment=(
                "Seed at most two genuinely mechanismally distinct Summit lineages for "
                "the exact immutable task. Name concrete mechanisms, assumptions, "
                "discriminating predictions, next dependencies, and bounded kill conditions."
            ),
            obligation_ids=obligation_ids,
            crux_ids=crux_ids,
            impact=Impact.HIGH,
            cost=CostBand.MODERATE,
            independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
            topology=CognitiveTopology.SUMMIT,
            epistemic_mode=EpistemicMode.THINK,
            expected_decision_effect=(
                "Either establish a viable upper-tail mechanism or close the concrete "
                "ceiling risk with scoped negative evidence."
            ),
            reusable_value=ValueBand.HIGH,
            distinctive_angle="mechanism-level exact-task support expansion",
            summit_reason=reason,
        )

    @staticmethod
    def _summit_development_action(
        lineage: SummitLineage,
        *,
        crux_ids: list[str],
        reason: str,
    ) -> ActionProposal:
        dependency = (
            lineage.unresolved_questions[0] if lineage.unresolved_questions else lineage.mechanism
        )
        return ActionProposal(
            kind=ActionKind.EXPLORE,
            target=lineage.name,
            assignment=(
                "Develop this exact-task Summit lineage only through its next unresolved "
                f"dependency or decisive falsifier: {dependency}"
            ),
            crux_ids=crux_ids,
            impact=Impact.HIGH,
            cost=CostBand.MODERATE,
            independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
            topology=CognitiveTopology.SUMMIT,
            epistemic_mode=EpistemicMode.THINK,
            expected_decision_effect=(
                "Either mature the lineage into a viable mechanism, extract reusable "
                "residue, or falsify it within its bounded unlock contract."
            ),
            reusable_value=ValueBand.HIGH,
            distinctive_angle=lineage.mechanism,
            lineage_id=lineage.lineage_id,
            summit_reason=reason,
        )

    @staticmethod
    def _checkpoint_payload(
        *,
        engine: FrontierEngine,
        call_id: str,
        output: CheckpointOutput,
        artifact: ArtifactRef,
        charter: TaskCharter | None,
        reframe_valid: bool,
        reframe_notes: list[str],
        normalization: list[str],
        accepted: list[str],
        rejected: list[str],
        receipt_updates: list[ActionReceipt],
        semantic: SemanticProjection,
        summit: SummitProjection,
        plan: CheckpointPlan,
        round_index: int,
        result: ProviderCallResult[CheckpointOutput],
        trace: CallTrace,
        use_lead: bool,
    ) -> dict[str, Any]:
        def dump(items: Sequence[Any]) -> list[dict[str, Any]]:
            return [item.model_dump(mode="json") for item in items]

        return {
            "call_id": call_id,
            "artifact": artifact.model_dump(mode="json"),
            "task_charter": charter.model_dump(mode="json") if charter else None,
            "artifact_spine": plan.spine.model_dump(mode="json") if plan.spine else None,
            "frontier_kernel": plan.frontier_kernel.model_dump(mode="json"),
            "issue_upserts": dump(semantic.issue_upserts),
            "obligation_upserts": dump(semantic.obligation_upserts),
            "crux_upserts": dump(semantic.crux_upserts),
            "substrate_entries": dump(semantic.substrate_upserts),
            "overlays": dump(semantic.overlay_upserts),
            "lineages": dump(summit.lineage_upserts),
            "discovery_records": dump(summit.discovery_upserts),
            "summit_active": summit.active,
            "summit_reasons": summit.reasons,
            "accepted_action_ids": accepted,
            "rejected_action_ids": unique_preserving_order(rejected),
            "receipt_updates": dump(receipt_updates),
            "actions": dump(plan.actions),
            "action_contracts": dump(plan.action_contracts),
            "lead_session": (
                engine.state.lead_session.model_dump(mode="json") if use_lead else None
            ),
            "stop_requested": plan.stop,
            "stop_reason": plan.stop_reason,
            "frame_break": output.frame_break if reframe_valid else None,
            "clean_synthesis_needed": output.clean_synthesis_needed,
            "completed_round_index": round_index,
            "usage": result.usage.model_dump(mode="json"),
            "normalization_notes": normalization,
            "issue_update_notes": semantic.issue_notes,
            "obligation_update_notes": semantic.obligation_notes,
            "crux_update_notes": semantic.crux_notes,
            "obligation_admission": asdict(semantic.obligation_admission),
            "crux_admission": asdict(semantic.crux_admission),
            "substrate_admission": asdict(semantic.substrate_admission),
            "overlay_admission": asdict(semantic.overlay_admission),
            "lineage_admission": summit.lineage_admission,
            "reframe_notes": reframe_notes,
            "dropped_action_proposals": plan.dropped_actions,
            "frontier_kernel_notes": asdict(plan.frontier_notes),
            "artifact_spine_notes": plan.spine_notes,
            "ceiling_scan": (
                output.ceiling_scan.model_dump(mode="json") if output.ceiling_scan else None
            ),
            **trace.payload(),
        }

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
        rejected.extend(item for item in completed if item not in accepted and item not in rejected)
        receipts = []
        for action_id in completed:
            record = engine.state.actions.get(action_id)
            if record is None or record.receipt is None:
                continue
            receipts.append(
                record.receipt.model_copy(
                    update={
                        "integration_status": ("accepted" if action_id in accepted else "rejected")
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
            (output.frame_break or output.task_charter is not None)
            and engine.config.cognition.require_reframe_witness
            and charter_change_requires_witness(current, output.task_charter)
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
