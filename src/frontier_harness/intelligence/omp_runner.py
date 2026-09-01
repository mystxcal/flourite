"""OMP Codex execution for the phase-free intelligence kernel.

The provider remains a transport. The model sees the exact objective, a compact
workspace, direct artifact access, and raw evidence handles; it does not inherit
any second controller ontology.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from ..adapters.base import ArtifactAdapter, CallWorkspace
from ..core.types import (
    AssayStatus,
    ChallengeVerdict,
    ComputeUsage,
    CoreModel,
    FailureDomain,
    Move,
    MoveMode,
    ObservationKind,
    RunState,
)
from ..errors import ProviderCallError
from ..models import ArtifactRef, EvidenceRecord, Role, SandboxPolicy
from ..providers.base import ProviderCallRequest, ProviderCallResult
from ..providers.omp_codex import OmpCodexProvider
from ..runtime.sources import StagedInput
from ..util import atomic_write_text, sha256_text, utc_now
from .context import ContextFrame
from .contracts import (
    ArtifactDraft,
    BlockerDraft,
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
    kind: Literal[ObservationKind.CHALLENGE] = ObservationKind.CHALLENGE
    verdict: ChallengeVerdict
    material_to_claim: bool = True
    artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    covered_claims: Sequence[str] = Field(default_factory=list)


class ModelAssayReceipt(CoreModel):
    status: AssayStatus
    coverage: str = ""
    reason: str = ""
    missing_material: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def explain_status(self) -> ModelAssayReceipt:
        if self.status == AssayStatus.VALID and not self.coverage.strip():
            raise ValueError("a valid assay requires concrete coverage")
        if self.status == AssayStatus.INVALID and not (
            self.reason.strip() or self.missing_material
        ):
            raise ValueError("an invalid assay requires the missing material or reason")
        return self


class AssayInvalidError(ValueError):
    """The evaluator could not perceive the target, so no quality verdict exists."""


class ModelNextMove(CoreModel):
    mode: MoveMode
    intent: str
    instructions: str = ""
    fork_purpose: str | None = None


class ModelFinish(CoreModel):
    satisfaction_claims: list[str] = Field(min_length=1)
    residual_uncertainty: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_claims(self) -> ModelFinish:
        normalized = [item.strip() for item in self.satisfaction_claims]
        if any(not item for item in normalized):
            raise ValueError("finish response contains an empty satisfaction claim")
        if len(normalized) != len(set(normalized)):
            raise ValueError("finish response repeats a satisfaction claim")
        return self


class ModelBlocker(CoreModel):
    reason: str = Field(min_length=1)


class ModelOutputBase(CoreModel):
    artifact_changed: bool = False
    observations: Sequence[ModelObservation] = Field(default_factory=list)
    next_move: ModelNextMove | None = None
    branches: Sequence[ModelNextMove] = Field(default_factory=list)
    finish: ModelFinish | None = None
    blocker: ModelBlocker | None = None

    @model_validator(mode="after")
    def one_continuation(self) -> ModelOutputBase:
        outcomes = sum(
            (
                self.next_move is not None or bool(self.branches),
                self.finish is not None,
                self.blocker is not None,
            )
        )
        if outcomes > 1:
            raise ValueError("choose continuation moves, a finish claim, or a blocker")
        if any(item.fork_purpose is None for item in self.branches):
            raise ValueError("every branch requires a concrete fork_purpose")
        if self.blocker is not None and not any(
            (item.evidence_path or "").strip() for item in self.observations
        ):
            raise ValueError("a blocker requires an observation with durable evidence_path")
        return self


class LeadModelOutput(ModelOutputBase):
    workspace_summary: str
    decision_boundary: str = Field(min_length=1)
    consumed_observation_ids: Sequence[str] = Field(default_factory=list)
    integrated_trajectory_ids: Sequence[str] = Field(default_factory=list)


class NavigatorModelOutput(ModelOutputBase):
    observations: Sequence[ModelObservation] = Field(min_length=1)
    finish: None = None


class ChallengeModelOutput(ModelOutputBase):
    assay: ModelAssayReceipt
    observations: Sequence[ModelChallengeObservation] = Field(default_factory=list)
    quality_delta: Sequence[str] = Field(default_factory=list)
    next_move: None = None
    branches: Sequence[ModelNextMove] = Field(default_factory=list, max_length=0)
    finish: None = None
    blocker: None = None

    @model_validator(mode="after")
    def assay_controls_verdicts(self) -> ChallengeModelOutput:
        if self.assay.status == AssayStatus.VALID and not self.observations:
            raise ValueError("a valid assay requires at least one semantic observation")
        if self.assay.status == AssayStatus.INVALID and (
            self.observations or self.quality_delta
        ):
            raise ValueError("an invalid assay cannot emit semantic verdicts or quality changes")
        return self


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
        activity_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.provider = provider
        self.adapter = adapter
        self.run_dir = run_dir
        self.sources = list(sources or [])
        self.activity_callback = activity_callback
        self.executions_dir = run_dir / "kernel-executions"
        self.sessions_path = run_dir / "provider-sessions.json"
        self.executions_dir.mkdir(parents=True, exist_ok=True)

    async def preflight(self) -> str | None:
        """Return a concrete provider-readiness failure before any move is spent."""

        result = await self.provider.doctor()
        if result.ok:
            return None
        return "; ".join(result.details) or "provider preflight failed"

    def discard_cached_result(self, move_id: str) -> None:
        """Drop a boundary object rejected by the authoritative compiler."""

        (self.executions_dir / move_id / "committed-result.json").unlink(missing_ok=True)

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

        parent = self._current_artifact(state, move)
        workspace = self.adapter.open_call(
            call_id=self._workspace_key(move),
            call_kind=move.mode.value,
            current_artifact=parent,
        )
        retain_workspace = False
        usage = ComputeUsage()
        try:
            self._write_context(workspace, context)
            if move.mode == MoveMode.CHALLENGE:
                self._preflight_assay(workspace)
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
                    "recovering": recovering,
                },
                activity_callback=self.activity_callback,
            )
            failed_resume_usage = ComputeUsage()
            reconstructed_session = False
            try:
                provider_result = await self.provider.run(request)
            except ProviderCallError as exc:
                failed_resume_usage = self._failed_usage(exc)
                if thread_id and self.provider.config.resume_fallback_to_reconstruction:
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
                        retain_workspace = True
                        failed.observations.append(self._retained_workspace(workspace, retry_exc))
                        atomic_write_text(cache_path, failed.model_dump_json(indent=2))
                        return failed
                else:
                    failed = self._provider_failure(exc, usage=failed_resume_usage)
                    retain_workspace = True
                    failed.observations.append(self._retained_workspace(workspace, exc))
                    atomic_write_text(cache_path, failed.model_dump_json(indent=2))
                    return failed
            if move.mode == MoveMode.LEAD and provider_result.thread_id:
                sessions[move.trajectory_id] = provider_result.thread_id
                self._write_sessions(sessions)
            usage = failed_resume_usage
            previous_commit_error: str | None = None
            repair = 0
            while True:
                usage = usage.plus(self._provider_usage(provider_result))
                try:
                    result = self._convert(
                        move=move,
                        state=state,
                        context=context,
                        workspace=workspace,
                        parent=parent,
                        output=provider_result.response,
                        usage=usage,
                    )
                    break
                except Exception as exc:
                    commit_error = f"{type(exc).__name__}: {exc}"
                    if commit_error == previous_commit_error:
                        retain_workspace = True
                        failed = self._boundary_failure(exc, workspace, usage)
                        atomic_write_text(cache_path, failed.model_dump_json(indent=2))
                        return failed
                    if state.usage.plus(usage).exhausted(state.objective.envelope):
                        retain_workspace = True
                        failed = self._boundary_failure(exc, workspace, usage)
                        atomic_write_text(cache_path, failed.model_dump_json(indent=2))
                        return failed
                    previous_commit_error = commit_error
                    repair += 1
                    if isinstance(exc, AssayInvalidError):
                        # Rebuild the exact evaluation capsule from immutable state before
                        # asking the same evaluator to retry. This repairs vanished material
                        # without converting an execution fault into a quality judgment.
                        self._write_context(workspace, context)
                        self._preflight_assay(workspace)
                    repair_note = (
                        "The runtime could not commit your last result. Do not explain "
                        "or restart. Inspect the live workspace, fix the problem directly, "
                        "and return the corrected typed result.\n\n"
                        f"Exact error: {commit_error}"
                    )
                    # OMP may report an ephemeral thread id even for --no-session
                    # calls.  Such ids cannot be resumed.  A fresh Challenger or
                    # Navigator repair gets the full durable prompt again; only a
                    # deliberately persistent Lead session receives a delta prompt.
                    repair_thread = (
                        provider_result.thread_id or sessions.get(move.trajectory_id)
                        if request.preserve_session
                        else None
                    )
                    repair_request = request.model_copy(
                        update={
                            "call_id": f"{move.move_id}-repair-{repair}",
                            "prompt": (
                                repair_note
                                if repair_thread
                                else f"{request.prompt}\n\n{repair_note}"
                            ),
                            "resume_thread_id": repair_thread,
                            "preserve_session": request.preserve_session,
                            "output_path": runtime_dir / f"response-repair-{repair}.json",
                        }
                    )
                    try:
                        provider_result = await self.provider.run(repair_request)
                    except ProviderCallError as repair_error:
                        failed_repair_usage = self._failed_usage(repair_error)
                        if repair_thread and self.provider.config.resume_fallback_to_reconstruction:
                            usage = usage.plus(failed_repair_usage)
                            repair_request = repair_request.model_copy(
                                update={
                                    "prompt": f"{request.prompt}\n\n{repair_note}",
                                    "resume_thread_id": None,
                                    "preserve_session": request.preserve_session,
                                }
                            )
                            try:
                                provider_result = await self.provider.run(repair_request)
                            except ProviderCallError as reconstructed_error:
                                failed = self._provider_failure(
                                    reconstructed_error,
                                    usage=usage.plus(self._failed_usage(reconstructed_error)),
                                )
                                retain_workspace = True
                                failed.observations.append(
                                    self._retained_workspace(workspace, reconstructed_error)
                                )
                                atomic_write_text(cache_path, failed.model_dump_json(indent=2))
                                return failed
                        else:
                            failed = self._provider_failure(
                                repair_error,
                                usage=usage.plus(failed_repair_usage),
                            )
                            retain_workspace = True
                            failed.observations.append(
                                self._retained_workspace(workspace, repair_error)
                            )
                            atomic_write_text(cache_path, failed.model_dump_json(indent=2))
                            return failed
                    if move.mode == MoveMode.LEAD and provider_result.thread_id:
                        sessions[move.trajectory_id] = provider_result.thread_id
                        self._write_sessions(sessions)
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
        except Exception as exc:
            retain_workspace = True
            failed = self._runtime_failure(exc, workspace, usage)
            atomic_write_text(cache_path, failed.model_dump_json(indent=2))
            return failed
        finally:
            if not retain_workspace:
                self.adapter.close_call(workspace)

    @staticmethod
    def _workspace_key(move: Move) -> str:
        """Keep the Lead's actual adapter workspace stable across epochs.

        A software Lead owns one isolated worktree per trajectory.  Evaluators
        still receive disposable projections, but construction, ignored build
        data, and the provider's recorded cwd all survive ordinary move
        boundaries.  The semantic move id remains separate in the execution
        ledger and provider request.
        """

        if move.mode in {MoveMode.LEAD, MoveMode.ENVIRONMENT}:
            return f"trajectory_{move.trajectory_id}"
        return move.move_id

    @staticmethod
    def _provider_usage(result: ProviderCallResult[Any]) -> ComputeUsage:
        return ComputeUsage(
            wall_seconds=result.duration_seconds,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            model_turns=max(
                result.usage.model_requests,
                result.trace_summary.model_turns,
            ),
            tool_calls=len(result.trace_summary.tool_calls),
        )

    @staticmethod
    def _retained_workspace(workspace: CallWorkspace, error: Exception) -> ObservationDraft:
        return ObservationDraft(
            kind=ObservationKind.ERROR,
            summary=(
                f"The failed live workspace was retained at {workspace.cwd}. "
                f"Continue there or recover its useful changes. Error: {type(error).__name__}: "
                f"{error}"
            ),
            source="runtime",
            metadata={"retained_workspace": str(workspace.cwd)},
        )

    def _runtime_failure(
        self,
        error: Exception,
        workspace: CallWorkspace,
        usage: ComputeUsage,
    ) -> MoveExecutionResult:
        return MoveExecutionResult(
            success=False,
            error=f"{type(error).__name__}: {error}",
            failure_domain=(
                FailureDomain.ASSAY
                if isinstance(error, AssayInvalidError)
                else FailureDomain.COMPONENT
            ),
            usage=usage,
            observations=[self._retained_workspace(workspace, error)],
        )

    def _boundary_failure(
        self,
        error: Exception,
        workspace: CallWorkspace,
        usage: ComputeUsage,
    ) -> MoveExecutionResult:
        """Classify a repeatedly invalid external result without blaming component code."""

        return MoveExecutionResult(
            success=False,
            error=f"{type(error).__name__}: {error}",
            failure_domain=(
                FailureDomain.ASSAY
                if isinstance(error, AssayInvalidError)
                else FailureDomain.PROVIDER
            ),
            usage=usage,
            observations=[self._retained_workspace(workspace, error)],
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
            failure_domain=FailureDomain.PROVIDER,
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

    def _current_artifact(self, state: RunState, move: Move) -> ArtifactRef | None:
        trajectory = state.trajectories[move.trajectory_id]
        if trajectory.artifact_head_id is not None:
            return self._adapter_artifact(state, state.artifacts[trajectory.artifact_head_id])

        # A new branch starts from the exact parent head visible when it forked,
        # not from an empty seed and not from whatever the parent becomes later.
        # base_workspace_id is the durable fork point.
        if trajectory.parent_trajectory_id is None:
            return None
        base = state.workspaces.get(trajectory.base_workspace_id or "")
        if base is None:
            return None
        parent_heads = [
            state.artifacts[artifact_id]
            for artifact_id in base.artifact_head_ids
            if artifact_id in state.artifacts
            and state.artifacts[artifact_id].trajectory_id == trajectory.parent_trajectory_id
        ]
        if not parent_heads:
            return None
        return self._adapter_artifact(state, parent_heads[-1])

    def _adapter_artifact(self, state: RunState, artifact: Any) -> ArtifactRef:
        workspace = state.current_workspace
        return ArtifactRef(
            artifact_id=artifact.artifact_id,
            version=int(artifact.metadata.get("adapter_version", len(state.artifacts))),
            blob=artifact.content_ref,
            kind=str(artifact.metadata.get("kind", self.adapter.artifact_kind)),
            summary=workspace.summary if workspace is not None else "current artifact",
            parent_artifact_id=(
                artifact.parent_artifact_ids[0] if artifact.parent_artifact_ids else None
            ),
            source_action_ids=[artifact.created_by_move_id],
            deliverables=artifact.deliverables,
            created_at=artifact.created_at,
        )

    def _write_context(self, workspace: CallWorkspace, context: ContextFrame) -> None:
        context_dir = workspace.context_dir
        context_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(context_dir / "objective.md", self._objective_text(context))
        if context.workspace_text is not None:
            atomic_write_text(context_dir / "workspace.md", context.workspace_text)
            atomic_write_text(context_dir / "frontier.md", context.workspace_text)
        if context.quality_text is not None:
            atomic_write_text(context_dir / "quality.md", context.quality_text)
        sources = self._write_sources(workspace, context_dir)
        self._write_observations(workspace, context)
        artifacts = self._write_artifacts(workspace, context)
        atomic_write_text(
            context_dir / "index.json",
            json.dumps(
                self._context_index(context, sources=sources, artifacts=artifacts),
                indent=2,
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _objective_text(context: ContextFrame) -> str:
        if not context.amendments:
            return context.objective_text
        return (
            context.objective_text
            + "\n\n# Explicit amendments\n\n"
            + "\n\n".join(context.amendments)
        )

    def _write_sources(
        self,
        workspace: CallWorkspace,
        context_dir: Path,
    ) -> list[dict[str, str]]:
        index: list[dict[str, str]] = []
        source_dir = context_dir / "sources"
        source_root = source_dir.resolve()
        for source in self.sources:
            destination = (source_dir / source.relative_path).resolve()
            try:
                destination.relative_to(source_root)
            except ValueError as exc:
                raise ValueError(f"source path escapes context: {source.relative_path}") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.adapter.blobs.materialize(source.content_ref, destination)
            index.append(
                {
                    "display_name": source.display_name,
                    "local_path": destination.relative_to(workspace.cwd).as_posix(),
                    "digest": source.content_ref.digest,
                }
            )
        return index

    def _write_observations(
        self,
        workspace: CallWorkspace,
        context: ContextFrame,
    ) -> list[dict[str, Any]]:
        index: list[dict[str, Any]] = []
        evidence_dir = workspace.context_dir / "evidence"
        for observation in context.observations:
            item = observation.model_dump(mode="json")
            if observation.raw_ref is not None:
                suffix = Path(observation.raw_ref.original_name or "evidence.bin").suffix
                destination = evidence_dir / f"{observation.observation_id}{suffix}"
                self.adapter.blobs.materialize(observation.raw_ref, destination)
                item["local_evidence_path"] = destination.relative_to(workspace.cwd).as_posix()
            index.append(item)
        atomic_write_text(
            workspace.context_dir / "observations.json",
            json.dumps(index, indent=2, ensure_ascii=False),
        )
        return index

    def _write_artifacts(
        self,
        workspace: CallWorkspace,
        context: ContextFrame,
    ) -> list[dict[str, Any]]:
        index: list[dict[str, Any]] = []
        artifacts_dir = workspace.context_dir / "artifacts"
        for artifact in context.artifact_heads:
            suffix = Path(artifact.content_ref.original_name or "artifact.bin").suffix or ".bin"
            destination = artifacts_dir / artifact.trajectory_id / f"{artifact.artifact_id}{suffix}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.adapter.blobs.materialize(artifact.content_ref, destination)
            item = artifact.model_dump(mode="json")
            item["local_path"] = destination.relative_to(workspace.cwd).as_posix()
            item["local_deliverables"] = self._write_deliverables(
                workspace,
                artifact.artifact_id,
                destination.parent,
                artifact.deliverables,
            )
            index.append(item)
        return index

    def _write_deliverables(
        self,
        workspace: CallWorkspace,
        artifact_id: str,
        directory: Path,
        deliverables: Sequence[Any],
    ) -> list[str]:
        paths: list[str] = []
        for index, deliverable in enumerate(deliverables):
            suffix = Path(deliverable.original_name or "deliverable.bin").suffix or ".bin"
            path = directory / f"{artifact_id}-{index}{suffix}"
            self.adapter.blobs.materialize(deliverable, path)
            paths.append(path.relative_to(workspace.cwd).as_posix())
        return paths

    @staticmethod
    def _context_index(
        context: ContextFrame,
        *,
        sources: list[dict[str, str]],
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "run_id": context.run_id,
            "mode": context.mode,
            "current_workspace_id": context.current_workspace_id,
            "workspace_summary": context.workspace_summary,
            "decision_boundary": context.decision_boundary,
            "quality_digest": (
                sha256_text(context.quality_text) if context.quality_text is not None else None
            ),
            "artifact_heads": artifacts,
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
            "sources": sources,
            "generated_at": utc_now(),
        }

    def _prompt(self, move: Move, workspace: CallWorkspace, context: ContextFrame) -> str:
        objective_path = (workspace.context_dir / "objective.md").relative_to(workspace.cwd)
        index_path = (workspace.context_dir / "index.json").relative_to(workspace.cwd)
        observations_path = (workspace.context_dir / "observations.json").relative_to(workspace.cwd)
        frontier_path = (workspace.context_dir / "frontier.md").relative_to(workspace.cwd)
        quality_path = (workspace.context_dir / "quality.md").relative_to(workspace.cwd)
        workspace_output = (workspace.output_dir / "workspace.md").relative_to(workspace.cwd)
        quality_output = (workspace.output_dir / "quality.md").relative_to(workspace.cwd)
        expected_artifact = workspace.expected_artifact_path.relative_to(workspace.cwd)
        common = f"""You are executing one meaningful Flourite move for an exact user task.

The original objective is authoritative: `{objective_path}`.
The compact run index is `{index_path}` and new evidence is `{observations_path}`.
The current compressed frontier, when present, is `{frontier_path}`.
The current task-native quality lens, when present, is `{quality_path}`.
Open the actual artifact, source, and raw evidence whenever the decision depends on them.

Move intent: {move.intent}
Move instructions: {move.instructions or "Use your judgment."}
Domain lens: {self.adapter.guidance or "Use the exact objective and direct evidence."}

Use your tools and do real work. Do not spend the move narrating a process, manufacturing
ceremony, or merely proposing work you can perform now. Preserve inconvenient evidence.
Prefer eliminating bad ideas in thought space when direct reasoning settles them; use tools
when the resulting observation can change a decision. Calls and activity are costs, not proof.
The typed final response is a concise durable boundary, not the work itself.
"""
        if move.mode in {MoveMode.LEAD, MoveMode.ENVIRONMENT}:
            return (
                common
                + f"""

Act as the persistent Lead. Improve the live result directly. For a document artifact,
write the current best artifact to `{expected_artifact}`. For a software artifact, modify
the isolated repository itself. Before returning, write the shortest reconstructible frontier
to `{workspace_output}`: causal model, load-bearing invariants, established facts,
failed bets and why, unresolved decision-changing uncertainty, shared assumptions, and the
few best discriminators. Also write `{quality_output}`: the evolving task-native success
and failure signatures, observable discriminators, proxy traps, coverage gaps, and blind spots.
Return `decision_boundary` as the single live decision or uncertainty whose resolution would
most change the result. Keep its wording stable while it is genuinely the same boundary;
change it only when reasoning or evidence has moved the frontier.
Integrate only quality distinctions grounded in the objective or direct evidence. Choose one
next move or make an evidenced
finish claim. If no move is obvious, broaden or reframe rather than inferring completion.
Return `consumed_observation_ids` only for evidence you actually integrated into the artifact,
frontier, or quality lens; unread or unresolved evidence must remain live. When branch evidence
has been integrated into the root artifact and frontier, return those exact child trajectory IDs
in `integrated_trajectory_ids`. A finish claim is legal only after every child trajectory is
integrated into one root artifact.
Use `blocker` only for a concrete external dependency that tools and further reasoning
cannot resolve; difficulty, uncertainty, and failed attempts are not blockers.
Open `branches` only when competing hypotheses or solution families make genuinely
different predictions; give each a concrete `fork_purpose`. Branching is optional, not
a default display of effort.
"""
            )
        if move.mode == MoveMode.NAVIGATE:
            return (
                common
                + """

Act as a fresh-context Navigator. Do not edit the artifact and do not declare completion.
Reconstruct the global shape: detect drift, repeated bets, missing solution families,
misallocated compute, brittle assumptions, and the strongest next move. Return at least
one concrete observation and normally a next move for the Lead.
"""
            )
        return (
            common
            + f"""

Act as an independent Challenger. Do not judge a description of the work: inspect the
actual current artifact and decision-relevant evidence. Test the requested live artifact
or finish claim against the exact objective. Every observation must say whether it
supports, challenges, or remains uncertain, with concrete scope. `artifact_digest` means
the canonical artifact-head digest from `{index_path}`, not the hash of a file
inside that artifact; when there is one target it may be omitted because the runtime binds
it exactly. If you attach raw evidence, give one existing workspace-local file in
`evidence_path`; put additional locators in the summary. Every claimed artifact head needs
direct support. Mark
`material_to_claim=false` when a finding is real but cannot change whether the exact
objective is satisfied. If such a finding is the only criticism, also state direct
support for the claim. For a formal finish claim, `covered_claims` must copy the exact
satisfaction-claim strings actually tested; together, material support observations must
cover every claim. Before supporting, determine whether those claims collectively span the
original objective and every amendment; if they omit anything material, challenge the
finish claim instead of validating its narrower wording. Do not edit the artifact or
prescribe a ritual.

First preflight what you can actually perceive. All paths in `{index_path}` are
relative to the current working directory; use them directly and never retype the absolute
workspace path. Return `assay.status=invalid` with the exact missing material only when the
objective, target, reference, or required viewer is genuinely inaccessible. An invalid assay
must emit no semantic verdict. When valid, summarize whole-artifact coverage in
`assay.coverage`. Treat the quality lens as a fallible hypothesis: use `quality_delta` for a
new material distinction or proxy trap revealed by direct inspection, not generic advice.
"""
        )

    def _convert(
        self,
        *,
        move: Move,
        state: RunState,
        context: ContextFrame,
        workspace: CallWorkspace,
        parent: ArtifactRef | None,
        output: ModelOutput,
        usage: ComputeUsage,
    ) -> MoveExecutionResult:
        if isinstance(output, ChallengeModelOutput) and output.assay.status == AssayStatus.INVALID:
            missing = ", ".join(output.assay.missing_material) or "unspecified material"
            reason = output.assay.reason or "the evaluator could not inspect the exact target"
            raise AssayInvalidError(f"{reason}; missing: {missing}")
        artifact, candidate = self._capture_artifact(move, workspace, parent, output)
        challenge_artifacts = self._challenge_artifacts(state, move)
        observations = self._model_observations(
            move,
            workspace,
            output,
            challenge_artifacts=challenge_artifacts,
        )
        observations.extend(
            self._artifact_observations(
                move,
                state,
                candidate,
                claim_artifacts=challenge_artifacts,
            )
        )
        workspace_result = self._workspace_result(
            move,
            state,
            workspace,
            output,
            visible_observation_ids={
                item.observation_id for item in context.observations
            },
        )
        self._validate_semantic_result(
            move=move,
            state=state,
            output=output,
            artifact=artifact,
            parent=parent,
            observations=observations,
            workspace=workspace_result,
        )
        return MoveExecutionResult(
            observations=observations,
            artifact=artifact,
            workspace=workspace_result,
            next_moves=self._directives(move, output),
            finish=self._finish_result(output),
            blocker=self._blocker_result(output),
            usage=usage,
        )

    @staticmethod
    def _validate_semantic_result(
        *,
        move: Move,
        state: RunState,
        output: ModelOutput,
        artifact: ArtifactDraft | None,
        parent: ArtifactRef | None,
        observations: Sequence[ObservationDraft],
        workspace: WorkspaceDraft | None,
    ) -> None:
        if output.blocker is not None and not any(item.raw_ref is not None for item in observations):
            raise ValueError("blocker evidence was not durably captured")
        if (
            isinstance(output, LeadModelOutput)
            and output.integrated_trajectory_ids
            and move.trajectory_id != state.root_trajectory_id
        ):
            raise ValueError("only the root Lead can integrate child trajectories")
        if state.finish_claim is not None:
            allowed_claims = set(state.finish_claim.satisfaction_claims)
            for observation in observations:
                unknown = set(observation.covered_claims) - allowed_claims
                if unknown:
                    raise ValueError(
                        "challenge returned unknown covered_claims: "
                        + ", ".join(sorted(unknown))
                    )
        if output.finish is None:
            return
        if move.mode != MoveMode.LEAD or move.trajectory_id != state.root_trajectory_id:
            raise ValueError("only the root Lead can make a finish claim")
        if artifact is None and parent is None:
            raise ValueError("finish claim requires an actual artifact")
        if workspace is None or workspace.active_trajectory_ids != [state.root_trajectory_id]:
            raise ValueError("finish claim requires every child trajectory integrated")

    def _capture_artifact(
        self,
        move: Move,
        workspace: CallWorkspace,
        parent: ArtifactRef | None,
        output: ModelOutput,
    ) -> tuple[ArtifactDraft | None, ArtifactRef | None]:
        if move.mode not in {MoveMode.LEAD, MoveMode.ENVIRONMENT}:
            return None, None
        candidate = self.adapter.capture_candidate_artifact(
            workspace,
            summary=cast(LeadModelOutput, output).workspace_summary,
            parent=parent,
            source_action_ids=[move.move_id],
        )
        if candidate is None or self._same_artifact(candidate, parent):
            return None, candidate
        return (
            ArtifactDraft(
                content_ref=candidate.blob,
                parent_artifact_ids=[parent.artifact_id] if parent else [],
                deliverables=candidate.deliverables,
                metadata={"kind": candidate.kind, "adapter_version": candidate.version},
            ),
            candidate,
        )

    @staticmethod
    def _same_artifact(candidate: ArtifactRef, parent: ArtifactRef | None) -> bool:
        return parent is not None and (
            candidate.blob.digest == parent.blob.digest
            and [item.digest for item in candidate.deliverables]
            == [item.digest for item in parent.deliverables]
        )

    @staticmethod
    def _challenge_artifacts(state: RunState, move: Move) -> list[Any]:
        """Return the exact artifact heads visible to a Challenger.

        A Challenger can be requested either by the kernel for a formal finish
        claim or explicitly by the Lead while the work is still evolving.  In
        the latter case there is deliberately no finish claim yet; the move's
        frozen workspace is the authority.  Treating "no claim" as "no
        artifact" made valid exploratory challenges impossible to commit.
        """

        if state.finish_claim is not None:
            artifact_ids = state.finish_claim.artifact_head_ids
        else:
            workspace = state.workspaces.get(move.based_on_workspace_id or "")
            artifact_ids = workspace.artifact_head_ids if workspace is not None else []
        return [state.artifacts[item] for item in artifact_ids if item in state.artifacts]

    def _model_observations(
        self,
        move: Move,
        workspace: CallWorkspace,
        output: ModelOutput,
        *,
        challenge_artifacts: Sequence[Any],
    ) -> list[ObservationDraft]:
        digest_owners = self._artifact_digest_owners(challenge_artifacts)
        target_digests = {item.digest for item in challenge_artifacts}
        assay_coverage = (
            output.assay.coverage if isinstance(output, ChallengeModelOutput) else None
        )
        observations = [
            self._model_observation(
                move,
                workspace,
                item,
                digest_owners=digest_owners,
                target_digests=target_digests,
                assay_coverage=assay_coverage,
            )
            for item in output.observations
        ]
        if isinstance(output, ChallengeModelOutput):
            observations.extend(
                ObservationDraft(
                    kind=ObservationKind.MODEL,
                    summary=item,
                    source="fresh-challenger",
                    assay_status=AssayStatus.VALID,
                    assay_coverage=output.assay.coverage,
                    direct_inspection=True,
                    quality_delta=True,
                )
                for item in output.quality_delta
            )
        return observations

    def _model_observation(
        self,
        move: Move,
        workspace: CallWorkspace,
        model_observation: ModelObservation,
        *,
        digest_owners: dict[str, str],
        target_digests: set[str],
        assay_coverage: str | None,
    ) -> ObservationDraft:
        raw_ref, evidence_metadata = self._capture_model_evidence(
            move,
            workspace,
            model_observation.evidence_path,
        )
        challenge = (
            model_observation if isinstance(model_observation, ModelChallengeObservation) else None
        )
        bound_digest, binding_metadata = self._challenge_digest(
            challenge,
            digest_owners=digest_owners,
            target_digests=target_digests,
        )
        metadata: dict[str, object] = {**evidence_metadata, **binding_metadata}
        return ObservationDraft(
            kind=model_observation.kind,
            summary=model_observation.summary,
            source={
                MoveMode.LEAD: "lead",
                MoveMode.NAVIGATE: "navigator",
                MoveMode.CHALLENGE: "fresh-challenger",
                MoveMode.ENVIRONMENT: "environment",
            }[move.mode],
            raw_ref=raw_ref,
            artifact_digest=bound_digest,
            confidence=model_observation.confidence,
            challenge_verdict=challenge.verdict if challenge is not None else None,
            assay_status=AssayStatus.VALID if challenge is not None else None,
            assay_coverage=assay_coverage if challenge is not None else None,
            covered_claims=list(challenge.covered_claims) if challenge is not None else [],
            material_to_claim=challenge.material_to_claim if challenge is not None else True,
            direct_inspection=challenge is not None,
            metadata=metadata,
        )

    @staticmethod
    def _artifact_digest_owners(artifacts: Sequence[Any]) -> dict[str, str]:
        owners: dict[str, str] = {}
        for artifact in artifacts:
            owners[artifact.digest] = artifact.digest
            for deliverable in artifact.deliverables:
                owners[deliverable.digest] = artifact.digest
        return owners

    def _capture_model_evidence(
        self,
        move: Move,
        workspace: CallWorkspace,
        declared_path: str | None,
    ) -> tuple[Any | None, dict[str, object]]:
        if not declared_path:
            return None, {}
        try:
            evidence_path = self.adapter.resolve_declared_path(workspace, declared_path)
        except (OSError, ValueError) as exc:
            if move.mode == MoveMode.CHALLENGE:
                raise AssayInvalidError(
                    f"declared challenge evidence is not accessible: {declared_path}"
                ) from exc
            raise
        if not evidence_path.is_file():
            error = f"evidence_path is not an existing workspace file: {declared_path}"
            if move.mode == MoveMode.CHALLENGE:
                raise AssayInvalidError(error)
            raise ValueError(error)
        return (
            self.adapter.blobs.put_file(evidence_path, original_name=evidence_path.name),
            {"evidence_capture": "durable"},
        )

    @staticmethod
    def _challenge_digest(
        challenge: ModelChallengeObservation | None,
        *,
        digest_owners: dict[str, str],
        target_digests: set[str],
    ) -> tuple[str | None, dict[str, object]]:
        if challenge is None:
            return None, {}
        reported = challenge.artifact_digest
        if reported is not None and reported in digest_owners:
            owner = digest_owners[reported]
            resolved_metadata: dict[str, object] = (
                {"inspected_content_digest": reported} if reported != owner else {}
            )
            return owner, resolved_metadata
        if reported is None and len(target_digests) == 1:
            owner = next(iter(target_digests))
            return owner, {"artifact_binding": "single_frozen_target"}
        if reported is not None:
            raise AssayInvalidError(
                "challenge artifact_digest does not identify a materialized artifact or "
                "deliverable; reread the capsule context index and return the canonical digest"
            )
        raise AssayInvalidError(
            "challenge observation is ambiguous across multiple artifact heads; return the "
            "canonical artifact_digest for the inspected target"
        )

    def _artifact_observations(
        self,
        move: Move,
        state: RunState,
        candidate: ArtifactRef | None,
        *,
        claim_artifacts: Sequence[Any],
    ) -> list[ObservationDraft]:
        if move.mode == MoveMode.CHALLENGE:
            artifacts = [self._adapter_artifact(state, item) for item in claim_artifacts]
        else:
            artifacts = [candidate] if candidate is not None else []
        observations: list[ObservationDraft] = []
        for artifact in artifacts:
            try:
                checks = (
                    self.adapter.deterministic_checks(artifact)
                    if move.mode == MoveMode.CHALLENGE
                    else self.adapter.staged_checks(artifact, stage="candidate")
                )
            except Exception as exc:
                if move.mode == MoveMode.CHALLENGE:
                    raise AssayInvalidError(
                        "artifact-native verification could not run for "
                        f"{artifact.blob.digest}: {type(exc).__name__}: {exc}"
                    ) from exc
                observations.append(self._check_failure(artifact, exc))
                continue
            observations.extend(
                self._check_observation(
                    check,
                    challenge=move.mode == MoveMode.CHALLENGE,
                )
                for check in checks
            )
        return observations

    @staticmethod
    def _check_failure(
        artifact: ArtifactRef,
        error: Exception,
    ) -> ObservationDraft:
        return ObservationDraft(
            kind=ObservationKind.ERROR,
            summary=(
                f"Artifact-native verification could not run: {type(error).__name__}: {error}"
            ),
            source="artifact-check",
            artifact_digest=artifact.blob.digest,
            confidence=1.0,
        )

    def _workspace_result(
        self,
        move: Move,
        state: RunState,
        workspace: CallWorkspace,
        output: ModelOutput,
        *,
        visible_observation_ids: set[str],
    ) -> WorkspaceDraft | None:
        if not isinstance(output, LeadModelOutput):
            return None
        path = workspace.output_dir / "workspace.md"
        if not path.is_file():
            relative = path.relative_to(workspace.cwd)
            raise ValueError(f"lead did not write the required {relative}")
        quality_path = workspace.output_dir / "quality.md"
        quality_document = (
            quality_path.read_text(encoding="utf-8")
            if quality_path.is_file()
            else self._bootstrap_quality(state)
        )
        grounded_deltas = [
            item
            for item in state.observations.values()
            if item.quality_delta and item.summary not in quality_document
        ]
        if grounded_deltas:
            quality_document = (
                quality_document.rstrip()
                + "\n\n## Evidence-driven updates\n\n"
                + "\n".join(f"- {item.summary}" for item in grounded_deltas)
                + "\n"
            )
        active = list(
            state.workspaces[move.based_on_workspace_id].active_trajectory_ids
            if move.based_on_workspace_id in state.workspaces
            else [state.root_trajectory_id]
        )
        integrated = set(output.integrated_trajectory_ids)
        if state.root_trajectory_id in integrated:
            raise ValueError("the root trajectory cannot be integrated away")
        unknown = integrated - set(active)
        if unknown:
            raise ValueError(
                "integrated_trajectory_ids are not live in this workspace: "
                + ", ".join(sorted(unknown))
            )
        active = [item for item in active if item not in integrated]
        inherited_consumed = list(
            state.workspaces[move.based_on_workspace_id].consumed_observation_ids
            if move.based_on_workspace_id in state.workspaces
            else []
        )
        newly_consumed = list(output.consumed_observation_ids)
        unseen = set(newly_consumed) - visible_observation_ids
        if unseen:
            raise ValueError(
                "consumed_observation_ids were not present in the live evidence context: "
                + ", ".join(sorted(unseen))
            )
        consumed = list(
            dict.fromkeys(
                inherited_consumed
                + newly_consumed
                + [item.observation_id for item in grounded_deltas]
            )
        )
        return WorkspaceDraft(
            document=path.read_text(encoding="utf-8"),
            quality_document=quality_document,
            summary=output.workspace_summary,
            decision_boundary=output.decision_boundary,
            consumed_observation_ids=consumed,
            active_trajectory_ids=active,
            activate=move.trajectory_id == state.root_trajectory_id,
        )

    def _bootstrap_quality(self, state: RunState) -> str:
        current = state.current_workspace
        if current is not None and current.quality_ref is not None:
            return self.adapter.blobs.read_text(current.quality_ref)
        return (
            "# Quality lens\n\n"
            "- Satisfy the exact objective in its decision-relevant form.\n"
            "- Judge the actual artifact rather than descriptions or proxy checks.\n"
            "- Treat this bootstrap as incomplete; replace it with task-native, observable "
            "success and failure signatures.\n"
        )

    @staticmethod
    def _preflight_assay(workspace: CallWorkspace) -> None:
        required = [
            workspace.context_dir / "objective.md",
            workspace.context_dir / "index.json",
            workspace.context_dir / "observations.json",
        ]
        missing = [path.name for path in required if not path.is_file()]
        index_path = workspace.context_dir / "index.json"
        if index_path.is_file():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            for artifact in index.get("artifact_heads", []):
                for relative in [
                    artifact.get("local_path"),
                    *artifact.get("local_deliverables", []),
                ]:
                    if relative and not (workspace.cwd / relative).is_file():
                        missing.append(str(relative))
        if missing:
            raise AssayInvalidError(
                "evaluation capsule preflight failed; missing: " + ", ".join(missing)
            )

    @staticmethod
    def _directives(move: Move, output: ModelOutput) -> list[MoveDirective]:
        items = [*output.branches]
        if output.next_move is not None:
            items.append(output.next_move)
        return [
            MoveDirective(
                mode=item.mode,
                intent=item.intent,
                instructions=item.instructions,
                trajectory_id=move.trajectory_id,
                fork_purpose=item.fork_purpose,
            )
            for item in items
        ]

    @staticmethod
    def _finish_result(output: ModelOutput) -> FinishDraft | None:
        if output.finish is None:
            return None
        return FinishDraft(
            satisfaction_claims=output.finish.satisfaction_claims,
            residual_uncertainty=output.finish.residual_uncertainty,
        )

    @staticmethod
    def _blocker_result(output: ModelOutput) -> BlockerDraft | None:
        if output.blocker is None:
            return None
        return BlockerDraft(reason=output.blocker.reason)

    @staticmethod
    def _check_observation(
        evidence: EvidenceRecord,
        *,
        challenge: bool,
    ) -> ObservationDraft:
        verdict = (
            ChallengeVerdict.CHALLENGES if evidence.negative_result else ChallengeVerdict.SUPPORTS
        )
        return ObservationDraft(
            kind=ObservationKind.CHALLENGE if challenge else ObservationKind.TEST,
            summary=evidence.summary,
            source="artifact-check",
            raw_ref=evidence.blob,
            artifact_digest=evidence.artifact_digest,
            confidence=1.0,
            challenge_verdict=verdict if challenge else None,
            assay_status=AssayStatus.VALID if challenge else None,
            assay_coverage=evidence.scope if challenge else None,
            material_to_claim=evidence.negative_result,
            direct_inspection=challenge,
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
            },
        )
