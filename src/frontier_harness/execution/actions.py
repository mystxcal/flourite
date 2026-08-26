"""One durable action transaction: attempt, observation, and integration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .. import events as et
from ..adapters.base import CallWorkspace
from ..cognition import (
    admit_overlays,
    admit_substrate_entries,
    derive_action_receipt,
    finalize_action_receipt,
    observed_modalities_from_trace,
)
from ..ids import new_id
from ..models import (
    ActionContract,
    ActionKind,
    ActionReceipt,
    ActionSpec,
    ArtifactRef,
    BlobRef,
    CandidateDelta,
    CognitiveTopology,
    DiscoveryRecord,
    EvidenceModality,
    EvidenceRecord,
    IndependenceClass,
    InstrumentSpec,
    InstrumentStatus,
    ObjectiveMeasurement,
    Probe,
    ProbeStatus,
    Role,
    RunState,
    SpeculativeOverlay,
    SubstrateEntry,
    SummitLineage,
    Usage,
    WorkerEnvelope,
)
from ..prompts import worker_prompt
from ..providers import ProviderCallResult, ProviderTraceSummary
from ..util import atomic_write_text, safe_slug, unique_preserving_order, utc_now
from .calls import CallTrace

if TYPE_CHECKING:
    from ..engine import FrontierEngine


@dataclass(slots=True)
class ActionExecution:
    """Durable context owned by one action from claim through integration."""

    action: ActionSpec
    round_state: RunState
    lineage_parent_ids: list[str]
    lineage_base: ArtifactRef | None
    baseline_objective: ObjectiveMeasurement | None
    workspace: CallWorkspace
    attempt_id: str
    action_contract: ActionContract | None
    context_lens_digest: str | None = None
    attempt_finished: bool = False
    provider_usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class ActionCallSuccess:
    """Provider-boundary result awaiting semantic integration."""

    envelope: WorkerEnvelope
    result_blob: BlobRef
    result: ProviderCallResult[WorkerEnvelope]
    trace: CallTrace
    normalization: list[str]
    use_lead: bool


class ActionExecutor:
    """Execute and durably integrate one scheduler-selected action."""

    async def execute(
        self,
        engine: FrontierEngine,
        action: ActionSpec,
        round_state: RunState,
        *,
        max_provider_calls: int = 1,
    ) -> None:
        execution = self._prepare(engine, action, round_state)
        try:
            success = await self._invoke_action(
                engine,
                execution,
                max_provider_calls=max_provider_calls,
            )
            self._integrate_success(
                engine=engine,
                action=action,
                round_state=round_state,
                workspace=execution.workspace,
                envelope=success.envelope,
                result_blob=success.result_blob,
                result=success.result,
                trace=success.trace,
                normalization=success.normalization,
                lineage_base=execution.lineage_base,
                baseline_objective=execution.baseline_objective,
                action_contract=execution.action_contract,
                context_lens_digest=execution.context_lens_digest,
                use_lead=success.use_lead,
            )
        except asyncio.CancelledError:
            self._record_cancellation(engine, execution)
            raise
        except BaseException as exc:
            self._record_failure(engine, execution, exc)
            if engine.config.run.fail_fast_on_provider_error:
                raise
        finally:
            engine._close_workspace(execution.workspace)

    def _prepare(
        self,
        engine: FrontierEngine,
        action: ActionSpec,
        round_state: RunState,
    ) -> ActionExecution:
        lineage_parent_ids = unique_preserving_order(
            [
                *action.parent_lineage_ids,
                *([action.lineage_id] if action.lineage_id else []),
            ]
        )
        lineage_base = round_state.current_artifact
        for lineage_id in lineage_parent_ids:
            lineage = round_state.summit_lineages.get(lineage_id)
            if lineage is not None and lineage.candidate_artifact is not None:
                lineage_base = lineage.candidate_artifact
                break
        baseline_objective = self._measure_baseline(
            engine,
            action,
            round_state,
            lineage_parent_ids,
            lineage_base,
        )
        workspace = engine.adapter.open_call(
            call_id=action.action_id,
            call_kind=f"worker-{action.kind.value}",
            current_artifact=lineage_base,
        )
        attempt_id = new_id("attempt")
        engine._append(
            et.ACTION_ATTEMPT_STARTED,
            {
                "attempt_id": attempt_id,
                "action_id": action.action_id,
                "started_at": utc_now(),
                "round_index": action.round_index,
            },
            actor="worker",
            action_id=action.action_id,
        )
        record = round_state.actions.get(action.action_id)
        return ActionExecution(
            action=action,
            round_state=round_state,
            lineage_parent_ids=lineage_parent_ids,
            lineage_base=lineage_base,
            baseline_objective=baseline_objective,
            workspace=workspace,
            attempt_id=attempt_id,
            action_contract=record.contract if record else None,
        )

    @staticmethod
    def _measure_baseline(
        engine: FrontierEngine,
        action: ActionSpec,
        round_state: RunState,
        lineage_parent_ids: list[str],
        lineage_base: ArtifactRef | None,
    ) -> ObjectiveMeasurement | None:
        primary = lineage_parent_ids[0] if lineage_parent_ids else None
        record = round_state.discovery_records.get(primary) if primary else None
        needed = (
            action.topology == CognitiveTopology.SUMMIT
            and primary is not None
            and (record is None or record.best_objective is None)
            and engine.adapter.objective_enabled
        )
        if not needed:
            return None
        workspace = engine.adapter.open_call(
            call_id=f"{action.action_id}-baseline",
            call_kind="objective-baseline",
            current_artifact=lineage_base,
        )
        try:
            return engine.adapter.measure_candidate(workspace)
        finally:
            engine._close_workspace(workspace)

    async def _invoke_action(
        self,
        engine: FrontierEngine,
        execution: ActionExecution,
        *,
        max_provider_calls: int,
    ) -> ActionCallSuccess:
        action = execution.action
        round_state = execution.round_state
        workspace = execution.workspace
        capsule = engine._capsules.populate(
            workspace,
            task=round_state.source_prompt,
            state=round_state,
            assignment=action.assignment,
            goal_contract=round_state.contract,
            task_source=round_state.task_source,
            action_contract=execution.action_contract,
            lens_purpose="action",
        )
        execution.context_lens_digest = cast(str, capsule["context_lens_digest"])
        self._write_lineage_context(engine, execution)
        use_lead = (
            engine.config.cognition.mode == "adaptive"
            and engine.config.cognition.persistent_lead
            and action.topology == CognitiveTopology.LEAD
        )
        result, trace = await engine._invoke(
            workspace,
            call_kind=f"worker-{action.kind.value}",
            role=Role.STRONG if use_lead else engine._role_for_action(action),
            prompt=worker_prompt(
                workspace,
                action=action,
                profile=engine._profile,
                software=engine._software,
            ),
            response_model=WorkerEnvelope,
            sandbox=action.sandbox,
            network_access=action.network or engine.config.provider.default_network_access,
            image_paths=[Path(item) for item in cast(list[str], capsule["image_paths"])],
            metadata={
                "target": action.target,
                "action_id": action.action_id,
                "task_source_digest": (
                    round_state.task_source.digest if round_state.task_source else None
                ),
                "active_obligation_ids": list(action.obligation_ids),
                "active_crux_ids": list(action.crux_ids),
                "topology": action.topology.value,
                "lineage_id": action.lineage_id,
                "parent_lineage_ids": list(action.parent_lineage_ids),
                "discovery_operator": (
                    action.discovery_operator.value if action.discovery_operator else None
                ),
            },
            use_lead=use_lead,
            max_provider_calls=max_provider_calls,
        )
        envelope = result.response
        declared, normalization = engine._ensure_worker_result_file(workspace, envelope)
        if declared != envelope.result_or_artifact_reference:
            envelope = envelope.model_copy(update={"result_or_artifact_reference": declared})
        result_blob = engine.adapter.capture_worker_result(workspace, declared)
        engine._append(
            et.ACTION_ATTEMPT_FINISHED,
            {
                "attempt_id": execution.attempt_id,
                "action_id": action.action_id,
                "status": "succeeded",
                "completed_at": utc_now(),
                "result_blob": result_blob.model_dump(mode="json"),
                "boundary_blob": (
                    trace.boundary_blob.model_dump(mode="json") if trace.boundary_blob else None
                ),
                "raw_events_blob": (
                    trace.raw_events_blob.model_dump(mode="json") if trace.raw_events_blob else None
                ),
                "usage": result.usage.model_dump(mode="json"),
            },
            actor="worker",
            action_id=action.action_id,
        )
        execution.attempt_finished = True
        execution.provider_usage = result.usage
        return ActionCallSuccess(
            envelope=envelope,
            result_blob=result_blob,
            result=result,
            trace=trace,
            normalization=normalization,
            use_lead=use_lead,
        )

    @staticmethod
    def _write_lineage_context(
        engine: FrontierEngine,
        execution: ActionExecution,
    ) -> None:
        entries: list[dict[str, Any]] = []
        directory = execution.workspace.context_dir / "lineage-candidates"
        for lineage_id in execution.lineage_parent_ids:
            lineage = execution.round_state.summit_lineages.get(lineage_id)
            if lineage is None:
                continue
            entry: dict[str, Any] = {
                "lineage_id": lineage_id,
                "name": lineage.name,
                "mechanism": lineage.mechanism,
                "candidate_artifact": None,
            }
            if lineage.candidate_artifact is not None:
                directory.mkdir(parents=True, exist_ok=True)
                suffix = ".patch" if lineage.candidate_artifact.kind == "git-patch" else ".artifact"
                destination = directory / f"{safe_slug(lineage_id)}{suffix}"
                engine.blobs.materialize(lineage.candidate_artifact.blob, destination)
                entry["candidate_artifact"] = str(destination)
            entries.append(entry)
        if entries:
            atomic_write_text(
                execution.workspace.context_dir / "LINEAGE_CONTEXT.json",
                json.dumps(
                    {
                        "working_tree_base_lineage": (
                            execution.lineage_parent_ids[0]
                            if execution.lineage_parent_ids
                            else None
                        ),
                        "parents": entries,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )

    @staticmethod
    def _record_cancellation(
        engine: FrontierEngine,
        execution: ActionExecution,
    ) -> None:
        action = execution.action
        if not execution.attempt_finished:
            engine._append(
                et.ACTION_ATTEMPT_FINISHED,
                {
                    "attempt_id": execution.attempt_id,
                    "action_id": action.action_id,
                    "status": "cancelled",
                    "completed_at": utc_now(),
                    "error": "worker cancelled before provider completion",
                    "usage": Usage().model_dump(mode="json"),
                },
                actor="worker",
                action_id=action.action_id,
            )
        recovery, capture_error = engine._capture_recovery_artifact(
            execution.workspace,
            summary=f"Interrupted action workspace: {action.target}",
            parent=execution.lineage_base,
            source_action_ids=[action.action_id],
        )
        engine._append(
            et.ACTION_FAILED,
            {
                "action_id": action.action_id,
                "error": "worker cancelled before durable completion",
                "completed_at": utc_now(),
                "usage": Usage().model_dump(mode="json"),
                "recovery_artifact": recovery.model_dump(mode="json") if recovery else None,
                "recovery_capture_error": capture_error,
            },
            actor="worker",
            action_id=action.action_id,
        )

    @staticmethod
    def _record_failure(
        engine: FrontierEngine,
        execution: ActionExecution,
        exc: BaseException,
    ) -> None:
        action = execution.action
        usage, trace = engine._failure_parts(exc)
        if execution.attempt_finished and execution.provider_usage is not None:
            # The provider boundary is already durable. A later semantic
            # integration error must not erase the model usage it incurred.
            usage = execution.provider_usage
        if not execution.attempt_finished:
            engine._append(
                et.ACTION_ATTEMPT_FINISHED,
                {
                    "attempt_id": execution.attempt_id,
                    "action_id": action.action_id,
                    "status": "failed",
                    "completed_at": utc_now(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "boundary_blob": (
                        trace.boundary_blob.model_dump(mode="json")
                        if trace.boundary_blob
                        else None
                    ),
                    "raw_events_blob": (
                        trace.raw_events_blob.model_dump(mode="json")
                        if trace.raw_events_blob
                        else None
                    ),
                    "usage": usage.model_dump(mode="json"),
                },
                actor="worker",
                action_id=action.action_id,
            )
        recovery, capture_error = engine._capture_recovery_artifact(
            execution.workspace,
            summary=f"Failed action workspace: {action.target}",
            parent=execution.lineage_base,
            source_action_ids=[action.action_id],
        )
        provider_trace = ProviderTraceSummary.model_validate(trace.provider_trace_summary or {})
        receipt = finalize_action_receipt(
            ActionReceipt(
                action_id=action.action_id,
                observed_result=f"{type(exc).__name__}: {exc}",
                evidence_strength="none",
                evidence_scope=action.stop_condition,
                integration_status="failed",
                recommended_next_action=action.failure_handling,
            ),
            contract=execution.action_contract,
            trace=provider_trace,
            usage=usage,
        ).model_copy(
            update={
                "context_lens_digest": execution.context_lens_digest,
                "parent_artifact_digest": (
                    execution.lineage_base.blob.digest if execution.lineage_base else None
                ),
            }
        )
        discoveries: list[DiscoveryRecord] = []
        if action.topology == CognitiveTopology.SUMMIT:
            projected = engine.experimental_frontier.observe(
                engine.state.discovery_records,
                execution.round_state.summit_lineages,
                action=action,
                returned=None,
                receipt=receipt,
                baseline_objective=execution.baseline_objective,
                objective=None,
                negative_result=False,
                event_seq=engine.journal.count() + 1,
            )
            discoveries = cast(
                list[DiscoveryRecord],
                engine._changed_models(engine.state.discovery_records, projected),
            )
        engine._append(
            et.ACTION_FAILED,
            {
                "action_id": action.action_id,
                "error": f"{type(exc).__name__}: {exc}",
                "completed_at": utc_now(),
                "usage": usage.model_dump(mode="json"),
                "action_receipt": receipt.model_dump(mode="json"),
                "baseline_objective_measurement": (
                    execution.baseline_objective.model_dump(mode="json")
                    if execution.baseline_objective
                    else None
                ),
                "discovery_records": [item.model_dump(mode="json") for item in discoveries],
                "recovery_artifact": recovery.model_dump(mode="json") if recovery else None,
                "recovery_capture_error": capture_error,
                **trace.payload(),
            },
            actor="worker",
            action_id=action.action_id,
        )

    def _integrate_success(
        self,
        *,
        engine: FrontierEngine,
        action: ActionSpec,
        round_state: RunState,
        workspace: CallWorkspace,
        envelope: WorkerEnvelope,
        result_blob: BlobRef,
        result: ProviderCallResult[WorkerEnvelope],
        trace: CallTrace,
        normalization: list[str],
        lineage_base: ArtifactRef | None,
        baseline_objective: ObjectiveMeasurement | None,
        action_contract: ActionContract | None,
        context_lens_digest: str | None,
        use_lead: bool,
    ) -> None:
        patch_blob = engine.adapter.capture_worker_patch(workspace)
        evidence_artifacts = engine.adapter.capture_evidence_artifacts(
            workspace,
            envelope.evidence_artifact_paths,
        )
        candidate_artifact = (
            engine.adapter.capture_candidate_artifact(
                workspace,
                summary="; ".join(envelope.findings) or action.target,
                parent=lineage_base,
                source_action_ids=[action.action_id],
            )
            if action.topology == CognitiveTopology.SUMMIT and patch_blob is not None
            else None
        )
        if envelope.lineage is not None:
            inherited_candidate = round_state.summit_lineages.get(envelope.lineage.lineage_id)
            envelope.lineage.candidate_artifact = candidate_artifact or (
                inherited_candidate.candidate_artifact if inherited_candidate else None
            )
        objective = engine.adapter.measure_candidate(workspace) if patch_blob else None
        evidence, evidence_items = self._build_evidence(
            action=action,
            round_state=round_state,
            envelope=envelope,
            result_blob=result_blob,
            evidence_artifacts=evidence_artifacts,
            candidate_artifact=candidate_artifact,
            baseline_objective=baseline_objective,
            objective=objective,
        )
        candidate, probe = self._classify_result(
            action=action,
            envelope=envelope,
            evidence=evidence,
            candidate_artifact=candidate_artifact,
            patch_blob=patch_blob,
            result_blob=result_blob,
        )
        receipt, evidence = self._bind_receipt(
            action=action,
            envelope=envelope,
            action_contract=action_contract,
            result=result,
            context_lens_digest=context_lens_digest,
            lineage_base=lineage_base,
            evidence=evidence,
            objective=objective,
        )
        evidence_items[0] = evidence
        substrate_candidates: list[SubstrateEntry] = []
        for raw in envelope.substrate_entries:
            item = raw.model_copy(deep=True)
            item.source_action_id = action.action_id
            if item.global_admission and not item.evidence_references:
                item.evidence_references = [evidence.evidence_id]
            substrate_candidates.append(item)
        projected_substrate, substrate_notes = admit_substrate_entries(
            substrate_candidates,
            existing=round_state.substrate,
        )
        substrate_upserts = cast(
            list[SubstrateEntry],
            engine._changed_models(round_state.substrate, projected_substrate),
        )

        instrument = self._normalize_instrument(
            engine=engine,
            action=action,
            envelope=envelope,
            result_blob=result_blob,
        )
        overlay_upserts: list[SpeculativeOverlay] = []
        if envelope.overlay is not None:
            projected_overlays, overlay_notes = admit_overlays(
                [envelope.overlay],
                existing=round_state.overlays,
                normal_limit=engine.config.cognition.normal_overlay_limit,
                hard_limit=engine.config.cognition.hard_overlay_limit,
                require_behavioral_difference=(
                    engine.config.cognition.require_behavioral_overlay_difference
                ),
            )
            overlay_upserts = cast(
                list[SpeculativeOverlay],
                engine._changed_models(round_state.overlays, projected_overlays),
            )
        else:
            overlay_notes = None

        lineage_upserts: list[SummitLineage] = []
        lineage_notes: dict[str, Any] = {}
        if envelope.lineage is not None and round_state.summit_active:
            projected_lineages, decision = engine.summit_archive.admit(
                round_state.summit_lineages, [envelope.lineage]
            )
            lineage_upserts = cast(
                list[SummitLineage],
                engine._changed_models(round_state.summit_lineages, projected_lineages),
            )
            lineage_notes = {
                "accepted": decision.accepted,
                "replaced": decision.replaced,
                "rejected": decision.rejected,
                "demoted": decision.demoted,
            }
        elif envelope.lineage is not None:
            lineage_notes = {
                "rejected": {
                    envelope.lineage.lineage_id: "Summit lineage returned while Summit was inactive"
                }
            }

        projected_discovery = engine.experimental_frontier.seed_records(
            round_state.summit_lineages,
            engine.state.discovery_records,
        )
        if action.topology == CognitiveTopology.SUMMIT:
            projected_discovery = engine.experimental_frontier.observe(
                projected_discovery,
                round_state.summit_lineages,
                action=action,
                returned=envelope.lineage,
                receipt=receipt,
                baseline_objective=baseline_objective,
                objective=objective,
                negative_result=envelope.negative_result,
                event_seq=engine.journal.count() + 1,
            )
        discovery_upserts = cast(
            list[DiscoveryRecord],
            engine._changed_models(engine.state.discovery_records, projected_discovery),
        )

        engine._append(
            et.ACTION_COMPLETED,
            {
                "action_id": action.action_id,
                "result": envelope.model_dump(mode="json"),
                "result_blob": result_blob.model_dump(mode="json"),
                "patch_blob": patch_blob.model_dump(mode="json") if patch_blob else None,
                "evidence": [item.model_dump(mode="json") for item in evidence_items],
                "candidate_delta": candidate.model_dump(mode="json") if candidate else None,
                "probe": probe.model_dump(mode="json") if probe else None,
                "action_receipt": receipt.model_dump(mode="json"),
                "objective_measurement": (
                    objective.model_dump(mode="json") if objective else None
                ),
                "baseline_objective_measurement": (
                    baseline_objective.model_dump(mode="json") if baseline_objective else None
                ),
                "substrate_entries": [
                    item.model_dump(mode="json") for item in substrate_upserts
                ],
                "instrument": instrument.model_dump(mode="json") if instrument else None,
                "overlays": [item.model_dump(mode="json") for item in overlay_upserts],
                "lineages": [item.model_dump(mode="json") for item in lineage_upserts],
                "discovery_records": [
                    item.model_dump(mode="json") for item in discovery_upserts
                ],
                "lead_session": (
                    engine.state.lead_session.model_dump(mode="json") if use_lead else None
                ),
                "completed_at": utc_now(),
                "usage": result.usage.model_dump(mode="json"),
                "normalization_notes": normalization,
                "substrate_admission": asdict(substrate_notes),
                "overlay_admission": asdict(overlay_notes) if overlay_notes else {},
                "lineage_admission": lineage_notes,
                **trace.payload(),
            },
            actor="lead" if use_lead else "worker",
            action_id=action.action_id,
        )

    @staticmethod
    def _build_evidence(
        *,
        action: ActionSpec,
        round_state: RunState,
        envelope: WorkerEnvelope,
        result_blob: BlobRef,
        evidence_artifacts: list[BlobRef],
        candidate_artifact: ArtifactRef | None,
        baseline_objective: ObjectiveMeasurement | None,
        objective: ObjectiveMeasurement | None,
    ) -> tuple[EvidenceRecord, list[EvidenceRecord]]:
        evidence = EvidenceRecord(
            evidence_id=new_id("evd"),
            source_action_id=action.action_id,
            kind=f"{action.kind.value}_result",
            summary="; ".join(envelope.findings) or "Worker returned no concise finding.",
            scope=envelope.scope or action.stop_condition,
            artifact_scope=action.artifact_scope,
            independence_class=action.independence_class,
            references=unique_preserving_order(
                [*envelope.evidence_references, result_blob.digest]
            ),
            blob=result_blob,
            negative_result=envelope.negative_result,
            modalities=list(action.observation_modalities),
            establishes=(
                list(envelope.action_receipt.decisions_changed)
                if envelope.action_receipt
                else []
            ),
            cannot_establish=(
                ["independent external validity"]
                if action.independence_class
                in {
                    IndependenceClass.SAME_MODEL,
                    IndependenceClass.DIFFERENT_CONDITIONING,
                }
                else []
            ),
            artifact_digest=(
                candidate_artifact.blob.digest if candidate_artifact is not None else None
            ),
        )
        evidence_items = [evidence]
        for ref in evidence_artifacts:
            evidence_items.append(
                EvidenceRecord(
                    evidence_id=new_id("evd"),
                    source_action_id=action.action_id,
                    kind="retained_worker_artifact",
                    summary=(
                        "Preserved generated evidence before isolated workspace cleanup: "
                        f"{ref.original_name or ref.digest}"
                    ),
                    scope=(
                        "Durable byte retention only. Content validity requires an actual "
                        "inspection in the declared modality."
                    ),
                    artifact_scope=action.artifact_scope,
                    independence_class=IndependenceClass.DETERMINISTIC_TOOL,
                    references=[ref.digest],
                    blob=ref,
                    modalities=[],
                    cannot_establish=["quality, correctness, or successful playback"],
                    artifact_digest=(
                        candidate_artifact.blob.digest
                        if candidate_artifact is not None
                        else None
                    ),
                )
            )
        if baseline_objective is not None:
            baseline_blob = baseline_objective.evidence_blob
            evidence_items.append(
                EvidenceRecord(
                    evidence_id=new_id("evd"),
                    source_action_id=action.action_id,
                    kind="objective_baseline",
                    summary=(
                        f"Baseline {baseline_objective.primary_metric}="
                        f"{baseline_objective.metrics.get(baseline_objective.primary_metric)!r}"
                        if baseline_objective.valid
                        else f"Baseline measurement invalid: {baseline_objective.detail}"
                    ),
                    scope=f"Isolated parent evaluator: {baseline_objective.command}",
                    artifact_scope=action.artifact_scope,
                    independence_class=IndependenceClass.DETERMINISTIC_TOOL,
                    references=[baseline_blob.digest] if baseline_blob else [],
                    blob=baseline_blob,
                    negative_result=not baseline_objective.valid,
                    modalities=[
                        EvidenceModality.DETERMINISTIC_TEST,
                        EvidenceModality.STRUCTURED_DATA,
                    ],
                    establishes=[baseline_objective.primary_metric],
                    artifact_digest=(
                        round_state.current_artifact.blob.digest
                        if round_state.current_artifact
                        else None
                    ),
                )
            )
        if objective is not None:
            objective_blob = objective.evidence_blob
            evidence_items.append(
                EvidenceRecord(
                    evidence_id=new_id("evd"),
                    source_action_id=action.action_id,
                    kind="objective_measurement",
                    summary=(
                        f"Measured {objective.primary_metric}="
                        f"{objective.metrics.get(objective.primary_metric)!r}"
                        if objective.valid
                        else f"Objective measurement invalid: {objective.detail}"
                    ),
                    scope=f"Runtime-owned evaluator: {objective.command}",
                    artifact_scope=action.artifact_scope,
                    independence_class=IndependenceClass.DETERMINISTIC_TOOL,
                    references=[objective_blob.digest] if objective_blob else [],
                    blob=objective_blob,
                    negative_result=not objective.valid,
                    modalities=[
                        EvidenceModality.DETERMINISTIC_TEST,
                        EvidenceModality.STRUCTURED_DATA,
                    ],
                    establishes=[objective.primary_metric],
                )
            )

        return evidence, evidence_items

    @staticmethod
    def _classify_result(
        *,
        action: ActionSpec,
        envelope: WorkerEnvelope,
        evidence: EvidenceRecord,
        candidate_artifact: ArtifactRef | None,
        patch_blob: BlobRef | None,
        result_blob: BlobRef,
    ) -> tuple[CandidateDelta | None, Probe | None]:
        candidate: CandidateDelta | None = None
        probe: Probe | None = None
        candidate_kinds = {
            ActionKind.EXPLOIT,
            ActionKind.EXPLORE,
            ActionKind.REPAIR,
            ActionKind.INTEGRATE,
            ActionKind.REFRAME,
            ActionKind.MECHANISM_GRAFT,
        }
        if action.kind in candidate_kinds:
            candidate = CandidateDelta(
                delta_id=new_id("delta"),
                target=action.target,
                proposed_change="; ".join(envelope.findings)
                or "See the referenced candidate artifact.",
                expected_benefit=envelope.decision_effect or action.expected_decision_effect,
                dependencies=unique_preserving_order(
                    [*action.issue_ids, *action.obligation_ids, *action.crux_ids]
                ),
                risks=envelope.unresolved_risks,
                evidence_references=[evidence.evidence_id],
                source_action_id=action.action_id,
                artifact_blob=(
                    candidate_artifact.blob
                    if candidate_artifact is not None
                    else patch_blob or result_blob
                ),
            )
        else:
            probe = Probe(
                probe_id=new_id("probe"),
                target_issue_ids=action.issue_ids,
                method=action.assignment,
                predicted_outcomes=[
                    branch.decision_effect for branch in action.outcome_branches
                ]
                or [action.expected_decision_effect],
                scope=envelope.scope or action.stop_condition,
                blind_spots=envelope.unresolved_risks,
                independence_class=action.independence_class,
                cost=action.cost,
                source_action_id=action.action_id,
                status=(
                    ProbeStatus.INCONCLUSIVE
                    if envelope.materiality == "none" and not envelope.negative_result
                    else ProbeStatus.COMPLETE
                ),
                finding="; ".join(envelope.findings),
                evidence_references=[evidence.evidence_id],
            )

        return candidate, probe

    @staticmethod
    def _bind_receipt(
        *,
        action: ActionSpec,
        envelope: WorkerEnvelope,
        action_contract: ActionContract | None,
        result: ProviderCallResult[WorkerEnvelope],
        context_lens_digest: str | None,
        lineage_base: ArtifactRef | None,
        evidence: EvidenceRecord,
        objective: ObjectiveMeasurement | None,
    ) -> tuple[ActionReceipt, EvidenceRecord]:
        receipt = (
            ActionReceipt.model_validate(envelope.action_receipt.model_dump(mode="json"))
            if envelope.action_receipt is not None
            else derive_action_receipt(
                action_id=action.action_id,
                findings=envelope.findings,
                decision_effect=(envelope.decision_effect or action.expected_decision_effect),
                scope=envelope.scope or action.stop_condition,
            )
        )
        if receipt.action_id != action.action_id:
            receipt = receipt.model_copy(update={"action_id": action.action_id})
        receipt = finalize_action_receipt(
            receipt,
            contract=action_contract,
            trace=result.trace_summary,
            usage=result.usage,
        )
        receipt = receipt.model_copy(
            update={
                "context_lens_digest": context_lens_digest,
                "parent_artifact_digest": (
                    lineage_base.blob.digest if lineage_base is not None else None
                ),
            }
        )
        observed_modalities = observed_modalities_from_trace(
            action.observation_modalities,
            result.trace_summary,
        )
        missing_modalities = [
            item for item in action.observation_modalities if item not in observed_modalities
        ]
        evidence = evidence.model_copy(
            update={
                "modalities": observed_modalities,
                "establishes": (list(receipt.decisions_changed) if observed_modalities else []),
                "cannot_establish": unique_preserving_order(
                    [
                        *evidence.cannot_establish,
                        *(
                            [
                                "requested observation modalities were not seen in the "
                                "provider tool trace: "
                                + ", ".join(item.value for item in missing_modalities)
                            ]
                            if missing_modalities
                            else []
                        ),
                    ]
                ),
            }
        )
        if objective is not None and objective.valid:
            measured_channels = list(receipt.observed_evidence_channels)
            if IndependenceClass.DETERMINISTIC_TOOL not in measured_channels:
                measured_channels.append(IndependenceClass.DETERMINISTIC_TOOL)
            receipt.observed_evidence_channels = measured_channels
            receipt.evidence_channel_confirmed = True

        return receipt, evidence

    @staticmethod
    def _normalize_instrument(
        *,
        engine: FrontierEngine,
        action: ActionSpec,
        envelope: WorkerEnvelope,
        result_blob: BlobRef,
    ) -> InstrumentSpec | None:
        instrument = envelope.instrument or action.instrument
        if instrument is not None and engine.config.cognition.instruments_enabled:
            instrument = instrument.model_copy(deep=True)
            if not instrument.instrument_id:
                instrument.instrument_id = new_id("ins")
            instrument.artifact_references = unique_preserving_order(
                [*instrument.artifact_references, result_blob.digest]
            )
            if envelope.negative_result:
                instrument.status = InstrumentStatus.FAILED
            elif instrument.status in {InstrumentStatus.VALIDATED, InstrumentStatus.EXECUTED}:
                if not instrument.validation_evidence:
                    # Execution success is not inference validity. Do not
                    # let a model self-declare a validated instrument without
                    # an explicit validation record.
                    instrument.status = InstrumentStatus.BUILT
            elif instrument.observation_evidence and instrument.validation_evidence:
                instrument.status = InstrumentStatus.EXECUTED
            elif instrument.validation_evidence:
                instrument.status = InstrumentStatus.VALIDATED
            else:
                instrument.status = InstrumentStatus.BUILT

        return instrument
