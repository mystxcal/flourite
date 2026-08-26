"""OMP Codex execution for the phase-free intelligence kernel.

The provider remains a transport. The model sees the exact objective, a compact
workspace, direct artifact access, and raw evidence handles; it does not inherit
the legacy controller ontology.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import Field, model_validator

from ..adapters.base import ArtifactAdapter, CallWorkspace
from ..core.types import (
    ChallengeVerdict,
    ComputeUsage,
    CoreModel,
    Move,
    MoveMode,
    ObservationKind,
    RunState,
)
from ..errors import ProviderCallError
from ..models import ArtifactRef, EvidenceRecord, Role, SandboxPolicy
from ..providers.base import ProviderCallRequest
from ..providers.omp_codex import OmpCodexProvider
from ..runtime.sources import StagedInput
from ..util import atomic_write_text, utc_now
from .context import ContextFrame
from .contracts import (
    ArtifactDraft,
    FinishDraft,
    MoveDirective,
    MoveExecutionResult,
    ObservationDraft,
    WorkspaceDraft,
)


class ModelObservation(CoreModel):
    kind: ObservationKind
    summary: str
    evidence_path: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ModelChallengeObservation(ModelObservation):
    kind: ObservationKind = ObservationKind.CHALLENGE
    verdict: ChallengeVerdict
    material_to_claim: bool = True


class ModelNextMove(CoreModel):
    mode: MoveMode
    intent: str
    instructions: str = ""
    fork_purpose: str | None = None


class ModelFinish(CoreModel):
    satisfaction_claims: list[str] = Field(min_length=1)
    residual_uncertainty: list[str] = Field(default_factory=list)


class ModelOutputBase(CoreModel):
    artifact_changed: bool = False
    observations: Sequence[ModelObservation] = Field(default_factory=list)
    next_move: ModelNextMove | None = None
    branches: Sequence[ModelNextMove] = Field(default_factory=list)
    finish: ModelFinish | None = None

    @model_validator(mode="after")
    def one_continuation(self) -> ModelOutputBase:
        if (self.next_move is not None or self.branches) and self.finish is not None:
            raise ValueError("choose continuation moves or a finish claim, not both")
        if any(item.fork_purpose is None for item in self.branches):
            raise ValueError("every branch requires a concrete fork_purpose")
        return self


class LeadModelOutput(ModelOutputBase):
    workspace_path: str
    workspace_summary: str


class NavigatorModelOutput(ModelOutputBase):
    observations: Sequence[ModelObservation] = Field(min_length=1)
    finish: None = None


class ChallengeModelOutput(ModelOutputBase):
    observations: Sequence[ModelChallengeObservation] = Field(min_length=1)
    next_move: None = None
    finish: None = None


ModelOutput = LeadModelOutput | NavigatorModelOutput | ChallengeModelOutput


class OmpMoveRunner:
    """Execute one Lead/Navigator/Challenger move in an isolated adapter capsule."""

    def __init__(
        self,
        *,
        provider: OmpCodexProvider,
        adapter: ArtifactAdapter,
        run_dir: Path,
        sources: list[StagedInput] | None = None,
    ) -> None:
        self.provider = provider
        self.adapter = adapter
        self.run_dir = run_dir
        self.sources = list(sources or [])
        self.executions_dir = run_dir / "kernel-executions"
        self.sessions_path = run_dir / "provider-sessions.json"
        self.lead_cwd = run_dir / "lead-session"
        self.executions_dir.mkdir(parents=True, exist_ok=True)
        self.lead_cwd.mkdir(parents=True, exist_ok=True)
        self.adapter.prepare()

    async def run(
        self,
        *,
        move: Move,
        state: RunState,
        context: ContextFrame,
        recovering: bool,
    ) -> MoveExecutionResult:
        execution_dir = self.executions_dir / move.move_id
        cache_path = execution_dir / "committed-result.json"
        if cache_path.is_file():
            return MoveExecutionResult.model_validate_json(cache_path.read_text(encoding="utf-8"))
        execution_dir.mkdir(parents=True, exist_ok=True)

        parent = self._current_legacy_artifact(state, move)
        workspace = self.adapter.open_call(
            call_id=move.move_id,
            call_kind=move.mode.value,
            current_artifact=parent,
        )
        try:
            self._write_context(workspace, context)
            output_type = self._output_type(move.mode)
            runtime_dir = execution_dir / "provider"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            sessions = self._sessions()
            thread_id = sessions.get(move.trajectory_id) if move.mode == MoveMode.LEAD else None
            request = ProviderCallRequest(
                call_id=move.move_id,
                call_kind=move.mode.value,
                role=Role.STRONG,
                prompt=self._prompt(move, workspace, context),
                cwd=workspace.cwd,
                response_model=output_type,
                output_path=runtime_dir / "response.json",
                schema_path=runtime_dir / "response.schema.json",
                sandbox=(
                    SandboxPolicy.WORKSPACE_WRITE
                    if move.mode in {MoveMode.LEAD, MoveMode.ENVIRONMENT}
                    else SandboxPolicy.READ_ONLY
                ),
                network_access=self.provider.config.default_network_access,
                expected_artifact_path=workspace.expected_artifact_path,
                resume_thread_id=thread_id,
                preserve_session=move.mode == MoveMode.LEAD,
                lead_call=move.mode == MoveMode.LEAD,
                max_provider_calls=self.provider.config.schema_attempts,
                metadata={
                    "provider_session_dir": self.run_dir / "provider-sessions",
                    "provider_lead_cwd": self.lead_cwd / move.trajectory_id,
                    "recovering": recovering,
                },
            )
            failed_resume_usage = ComputeUsage()
            reconstructed_session = False
            try:
                provider_result = await self.provider.run(request)
            except ProviderCallError as exc:
                failed_resume_usage = self._failed_usage(exc)
                if (
                    thread_id
                    and self.provider.config.resume_fallback_to_reconstruction
                    and self._session_is_unavailable(str(exc))
                ):
                    sessions.pop(move.trajectory_id, None)
                    self._write_sessions(sessions)
                    reconstructed_session = True
                    request = request.model_copy(
                        update={"resume_thread_id": None, "preserve_session": True}
                    )
                    try:
                        provider_result = await self.provider.run(request)
                    except ProviderCallError as retry_exc:
                        failed = self._provider_failure(
                            retry_exc,
                            usage=failed_resume_usage.plus(self._failed_usage(retry_exc)),
                        )
                        atomic_write_text(cache_path, failed.model_dump_json(indent=2))
                        return failed
                else:
                    failed = self._provider_failure(exc, usage=failed_resume_usage)
                    atomic_write_text(cache_path, failed.model_dump_json(indent=2))
                    return failed
            if move.mode == MoveMode.LEAD and provider_result.thread_id:
                sessions[move.trajectory_id] = provider_result.thread_id
                self._write_sessions(sessions)
            successful_usage = ComputeUsage(
                wall_seconds=provider_result.duration_seconds,
                input_tokens=provider_result.usage.input_tokens,
                output_tokens=provider_result.usage.output_tokens,
                model_turns=max(
                    provider_result.usage.model_requests,
                    provider_result.trace_summary.model_turns,
                ),
                tool_calls=len(provider_result.trace_summary.tool_calls),
            )
            result = self._convert(
                move=move,
                state=state,
                workspace=workspace,
                parent=parent,
                output=provider_result.response,
                usage=failed_resume_usage.plus(successful_usage),
            )
            if reconstructed_session:
                result.observations.append(
                    ObservationDraft(
                        kind=ObservationKind.RESOURCE,
                        summary=(
                            "The persistent Lead session disappeared; it was reconstructed from "
                            "the durable objective, workspace, artifacts, and evidence."
                        ),
                        source="runtime",
                        metadata={"session_reconstructed": True},
                    )
                )
            atomic_write_text(
                cache_path,
                result.model_dump_json(indent=2),
            )
            return result
        finally:
            self.adapter.close_call(workspace)

    @staticmethod
    def _session_is_unavailable(message: str) -> bool:
        return bool(
            re.search(
                r"(?:(?:session|thread).{0,100}(?:not found|does not exist|unknown|missing|invalid)"
                r"|no (?:such )?(?:session|thread)|resume.{0,100}(?:not found|unknown session))",
                message,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )

    @staticmethod
    def _failed_usage(error: ProviderCallError) -> ComputeUsage:
        trace = error.trace_summary
        model_turns = int(trace.get("model_turns", 0)) if isinstance(trace, dict) else 0
        tool_calls = len(trace.get("tool_calls", [])) if isinstance(trace, dict) else 0
        return ComputeUsage(
            wall_seconds=error.usage.wall_seconds,
            input_tokens=error.usage.input_tokens,
            output_tokens=error.usage.output_tokens,
            model_turns=max(error.usage.model_requests, model_turns),
            tool_calls=tool_calls,
        )

    def _provider_failure(
        self,
        error: ProviderCallError,
        *,
        usage: ComputeUsage,
    ) -> MoveExecutionResult:
        raw_ref = None
        for path in (error.raw_events_path, error.stderr_path):
            if path is not None and path.is_file():
                raw_ref = self.adapter.blobs.put_file(path, original_name=path.name)
                break
        return MoveExecutionResult(
            success=False,
            error=f"{type(error).__name__}: {error}",
            usage=usage,
            observations=[
                ObservationDraft(
                    kind=ObservationKind.ERROR,
                    summary=f"Provider move failed: {error}",
                    source="provider",
                    raw_ref=raw_ref,
                )
            ],
        )

    @staticmethod
    def _output_type(
        mode: MoveMode,
    ) -> type[LeadModelOutput] | type[NavigatorModelOutput] | type[ChallengeModelOutput]:
        if mode in {MoveMode.LEAD, MoveMode.ENVIRONMENT}:
            return LeadModelOutput
        if mode == MoveMode.NAVIGATE:
            return NavigatorModelOutput
        return ChallengeModelOutput

    def _sessions(self) -> dict[str, str]:
        if not self.sessions_path.is_file():
            return {}
        value = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()
        ):
            raise ValueError("provider session map is malformed")
        return cast(dict[str, str], value)

    def _write_sessions(self, sessions: dict[str, str]) -> None:
        atomic_write_text(self.sessions_path, json.dumps(sessions, indent=2, sort_keys=True))

    def _current_legacy_artifact(self, state: RunState, move: Move) -> ArtifactRef | None:
        workspace = state.workspaces.get(move.based_on_workspace_id or "")
        if workspace is None:
            return None
        candidates = [
            state.artifacts[item]
            for item in workspace.artifact_head_ids
            if item in state.artifacts
            and state.artifacts[item].trajectory_id == move.trajectory_id
        ]
        if not candidates:
            return None
        artifact = candidates[-1]
        return ArtifactRef(
            artifact_id=artifact.artifact_id,
            version=int(artifact.metadata.get("legacy_version", len(state.artifacts))),
            blob=artifact.content_ref,
            kind=self.adapter.artifact_kind,
            summary=workspace.summary,
            parent_artifact_id=(artifact.parent_artifact_ids[0] if artifact.parent_artifact_ids else None),
            source_action_ids=[artifact.created_by_move_id],
            deliverables=artifact.deliverables,
            created_at=artifact.created_at,
        )

    def _write_context(self, workspace: CallWorkspace, context: ContextFrame) -> None:
        context_dir = workspace.context_dir
        context_dir.mkdir(parents=True, exist_ok=True)
        objective = context.objective_text
        if context.amendments:
            objective += "\n\n# Explicit amendments\n\n" + "\n\n".join(context.amendments)
        atomic_write_text(context_dir / "objective.md", objective)
        if context.workspace_text is not None:
            atomic_write_text(context_dir / "workspace.md", context.workspace_text)

        source_index: list[dict[str, str]] = []
        source_dir = context_dir / "sources"
        for source in self.sources:
            destination = (source_dir / source.relative_path).resolve()
            try:
                destination.relative_to(source_dir.resolve())
            except ValueError as exc:
                raise ValueError(f"source path escapes context: {source.relative_path}") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.adapter.blobs.materialize(source.content_ref, destination)
            source_index.append(
                {
                    "display_name": source.display_name,
                    "local_path": destination.relative_to(workspace.cwd).as_posix(),
                    "digest": source.content_ref.digest,
                }
            )

        observations: list[dict[str, Any]] = []
        evidence_dir = context_dir / "evidence"
        for observation in context.observations:
            item = observation.model_dump(mode="json")
            if observation.raw_ref is not None:
                suffix = Path(observation.raw_ref.original_name or "evidence.bin").suffix
                destination = evidence_dir / f"{observation.observation_id}{suffix}"
                self.adapter.blobs.materialize(observation.raw_ref, destination)
                item["local_evidence_path"] = destination.relative_to(workspace.cwd).as_posix()
            observations.append(item)
        atomic_write_text(
            context_dir / "observations.json",
            json.dumps(observations, indent=2, ensure_ascii=False),
        )
        artifact_index: list[dict[str, Any]] = []
        artifacts_dir = context_dir / "artifacts"
        for artifact in context.artifact_heads:
            suffix = Path(artifact.content_ref.original_name or "artifact.bin").suffix or ".bin"
            destination = (
                artifacts_dir
                / artifact.trajectory_id
                / f"{artifact.artifact_id}{suffix}"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.adapter.blobs.materialize(artifact.content_ref, destination)
            deliverables: list[str] = []
            for deliverable_index, deliverable in enumerate(artifact.deliverables):
                item_suffix = Path(deliverable.original_name or "deliverable.bin").suffix or ".bin"
                item_path = (
                    destination.parent
                    / f"{artifact.artifact_id}-{deliverable_index}{item_suffix}"
                )
                self.adapter.blobs.materialize(deliverable, item_path)
                deliverables.append(item_path.relative_to(workspace.cwd).as_posix())
            item = artifact.model_dump(mode="json")
            item["local_path"] = destination.relative_to(workspace.cwd).as_posix()
            item["local_deliverables"] = deliverables
            artifact_index.append(item)
        index = {
            "run_id": context.run_id,
            "mode": context.mode,
            "current_workspace_id": context.current_workspace_id,
            "workspace_summary": context.workspace_summary,
            "artifact_heads": artifact_index,
            "trajectories": [item.model_dump(mode="json") for item in context.trajectories],
            "recent_moves": [
                {
                    "move_id": item.move_id,
                    "mode": item.mode,
                    "intent": item.intent,
                    "status": item.status,
                    "error": item.error,
                }
                for item in context.recent_moves
            ],
            "finish_claim": (
                context.finish_claim.model_dump(mode="json") if context.finish_claim else None
            ),
            "usage": context.usage.model_dump(mode="json"),
            "hard_envelope": context.envelope.model_dump(mode="json"),
            "capabilities": context.capabilities,
            "sources": source_index,
            "generated_at": utc_now(),
        }
        atomic_write_text(
            context_dir / "index.json",
            json.dumps(index, indent=2, ensure_ascii=False),
        )

    def _prompt(self, move: Move, workspace: CallWorkspace, context: ContextFrame) -> str:
        objective_path = (workspace.context_dir / "objective.md").relative_to(workspace.cwd)
        index_path = (workspace.context_dir / "index.json").relative_to(workspace.cwd)
        observations_path = (workspace.context_dir / "observations.json").relative_to(
            workspace.cwd
        )
        expected_artifact = workspace.expected_artifact_path.relative_to(workspace.cwd)
        common = f"""You are executing one meaningful Flourite move for an exact user task.

The original objective is authoritative: `{objective_path}`.
The compact run index is `{index_path}` and new evidence is `{observations_path}`.
Open the actual artifact, source, and raw evidence whenever the decision depends on them.

Move intent: {move.intent}
Move instructions: {move.instructions or "Use your judgment."}

Use your tools and do real work. Do not spend the move narrating a process, manufacturing
ceremony, or merely proposing work you can perform now. Preserve inconvenient evidence.
The typed final response is a concise durable boundary, not the work itself.
"""
        if move.mode in {MoveMode.LEAD, MoveMode.ENVIRONMENT}:
            return common + f"""

Act as the persistent Lead. Improve the live result directly. For a document artifact,
write the current best artifact to `{expected_artifact}`. For a software artifact, modify
the isolated repository itself. Before returning, write a compact decision map to
`.sfh_output/workspace.md`; it should capture the current best, strategy, causal evidence,
failed approaches worth remembering, unresolved load-bearing uncertainty, and the next
frontier. Set `workspace_path` to that file. Choose one next move or make an evidenced
finish claim. If no move is obvious, broaden or reframe rather than inferring completion.
Open `branches` only when competing hypotheses or solution families make genuinely
different predictions; give each a concrete `fork_purpose`. Branching is optional, not
a default display of effort.
"""
        if move.mode == MoveMode.NAVIGATE:
            return common + """

Act as a fresh-context Navigator. Do not edit the artifact and do not declare completion.
Reconstruct the global shape: detect drift, repeated bets, missing solution families,
misallocated compute, brittle assumptions, and the strongest next move. Return at least
one concrete observation and normally a next move for the Lead.
"""
        return common + """

Act as an independent Challenger. Do not judge a description of the work: inspect the
actual current artifact and decision-relevant evidence. Try to falsify the finish claim
against the exact objective. Every observation must say whether it supports, challenges,
or remains uncertain, with concrete scope. Mark `material_to_claim=false` when a finding
is real but cannot change whether the exact objective is satisfied. Do not edit the
artifact or prescribe a ritual.
"""

    def _convert(
        self,
        *,
        move: Move,
        state: RunState,
        workspace: CallWorkspace,
        parent: ArtifactRef | None,
        output: ModelOutput,
        usage: ComputeUsage,
    ) -> MoveExecutionResult:
        artifact_draft: ArtifactDraft | None = None
        candidate: ArtifactRef | None = None
        if move.mode in {MoveMode.LEAD, MoveMode.ENVIRONMENT}:
            candidate = self.adapter.capture_candidate_artifact(
                workspace,
                summary=cast(LeadModelOutput, output).workspace_summary,
                parent=parent,
                source_action_ids=[move.move_id],
            )
            if candidate is not None and (
                parent is None
                or candidate.blob.digest != parent.blob.digest
                or [item.digest for item in candidate.deliverables]
                != [item.digest for item in parent.deliverables]
            ):
                artifact_draft = ArtifactDraft(
                    content_ref=candidate.blob,
                    parent_artifact_ids=[parent.artifact_id] if parent else [],
                    deliverables=candidate.deliverables,
                    metadata={
                        "kind": candidate.kind,
                        "legacy_version": candidate.version,
                    },
                )

        source = {
            MoveMode.LEAD: "lead",
            MoveMode.NAVIGATE: "navigator",
            MoveMode.CHALLENGE: "fresh-challenger",
            MoveMode.ENVIRONMENT: "environment",
        }[move.mode]
        current_digest = parent.blob.digest if parent is not None else None
        observations: list[ObservationDraft] = []
        for model_observation in output.observations:
            raw_ref = None
            if model_observation.evidence_path:
                evidence_path = self.adapter.resolve_declared_path(
                    workspace, model_observation.evidence_path
                )
                raw_ref = self.adapter.blobs.put_file(
                    evidence_path,
                    original_name=evidence_path.name,
                )
            verdict = (
                model_observation.verdict
                if isinstance(model_observation, ModelChallengeObservation)
                else None
            )
            observations.append(
                ObservationDraft(
                    kind=model_observation.kind,
                    summary=model_observation.summary,
                    source=source,
                    raw_ref=raw_ref,
                    artifact_digest=(current_digest if move.mode == MoveMode.CHALLENGE else None),
                    confidence=model_observation.confidence,
                    challenge_verdict=verdict,
                    metadata=(
                        {"material_to_claim": model_observation.material_to_claim}
                        if isinstance(model_observation, ModelChallengeObservation)
                        else {}
                    ),
                )
            )

        checked_artifact = candidate if candidate is not None else parent
        if checked_artifact is not None:
            try:
                checks = (
                    self.adapter.deterministic_checks(checked_artifact)
                    if move.mode == MoveMode.CHALLENGE
                    else self.adapter.staged_checks(checked_artifact, stage="candidate")
                    if candidate is not None
                    else []
                )
            except Exception as exc:
                checks = []
                observations.append(
                    ObservationDraft(
                        kind=(
                            ObservationKind.CHALLENGE
                            if move.mode == MoveMode.CHALLENGE
                            else ObservationKind.ERROR
                        ),
                        summary=f"Artifact-native verification could not run: {type(exc).__name__}: {exc}",
                        source="artifact-check",
                        artifact_digest=checked_artifact.blob.digest,
                        confidence=1.0,
                        challenge_verdict=(
                            ChallengeVerdict.UNCERTAIN
                            if move.mode == MoveMode.CHALLENGE
                            else None
                        ),
                    )
                )
            observations.extend(
                self._check_observation(
                    check,
                    challenge=move.mode == MoveMode.CHALLENGE,
                )
                for check in checks
            )

        workspace_draft: WorkspaceDraft | None = None
        if isinstance(output, LeadModelOutput):
            path = self.adapter.resolve_declared_path(workspace, output.workspace_path)
            workspace_document = path.read_text(encoding="utf-8")
            workspace_draft = WorkspaceDraft(
                document=workspace_document,
                summary=output.workspace_summary,
                activate=move.trajectory_id == state.root_trajectory_id,
            )

        directives: list[MoveDirective] = []
        for branch in output.branches:
            directives.append(
                MoveDirective(
                    mode=branch.mode,
                    intent=branch.intent,
                    instructions=branch.instructions,
                    trajectory_id=move.trajectory_id,
                    fork_purpose=branch.fork_purpose,
                )
            )
        if output.next_move is not None:
            directives.append(
                MoveDirective(
                    mode=output.next_move.mode,
                    intent=output.next_move.intent,
                    instructions=output.next_move.instructions,
                    trajectory_id=move.trajectory_id,
                    fork_purpose=output.next_move.fork_purpose,
                )
            )
        finish = None
        if output.finish is not None:
            finish = FinishDraft(
                satisfaction_claims=output.finish.satisfaction_claims,
                residual_uncertainty=output.finish.residual_uncertainty,
            )
        return MoveExecutionResult(
            observations=observations,
            artifact=artifact_draft,
            workspace=workspace_draft,
            next_moves=directives,
            finish=finish,
            usage=usage,
        )

    @staticmethod
    def _check_observation(
        evidence: EvidenceRecord,
        *,
        challenge: bool,
    ) -> ObservationDraft:
        verdict = (
            ChallengeVerdict.CHALLENGES
            if evidence.negative_result
            else ChallengeVerdict.SUPPORTS
        )
        return ObservationDraft(
            kind=ObservationKind.CHALLENGE if challenge else ObservationKind.TEST,
            summary=evidence.summary,
            source="artifact-check",
            raw_ref=evidence.blob,
            artifact_digest=evidence.artifact_digest,
            confidence=1.0,
            challenge_verdict=verdict if challenge else None,
            metadata={
                "evidence_id": evidence.evidence_id,
                "scope": evidence.scope,
                "artifact_scope": evidence.artifact_scope,
                "independence_class": evidence.independence_class,
                "references": evidence.references,
                "modalities": evidence.modalities,
                "establishes": evidence.establishes,
                "cannot_establish": evidence.cannot_establish,
                "negative_result": evidence.negative_result,
                "material_to_claim": evidence.negative_result,
            },
        )
