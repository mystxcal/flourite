"""Provider execution and durable provenance for one bounded cognitive call."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, cast

from pydantic import BaseModel

from .. import events as et
from ..adapters.base import CallWorkspace
from ..blobs import BlobStore
from ..cognition import validate_lead_ack
from ..config import HarnessConfig
from ..errors import ProviderCallError, ProviderError
from ..models import (
    BlobRef,
    LeadContinuityStatus,
    Role,
    RunState,
    SandboxPolicy,
    Usage,
)
from ..observability import LiveObserver
from ..providers import ModelProvider, ProviderCallRequest, ProviderCallResult
from ..util import atomic_write_text

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class EventAppender(Protocol):
    def __call__(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str = "runtime",
        action_id: str | None = None,
    ) -> object: ...


@dataclass(slots=True)
class CallSpec(Generic[ResponseT]):
    call_kind: str
    role: Role
    prompt: str
    response_model: type[ResponseT]
    sandbox: SandboxPolicy
    network_access: bool
    image_paths: list[Path] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    use_lead: bool = False
    max_provider_calls: int | None = None
    resume_thread_id: str | None = None


@dataclass(slots=True)
class CallTrace:
    prompt_blob: BlobRef | None = None
    context_lens_blob: BlobRef | None = None
    schema_blob: BlobRef | None = None
    boundary_blob: BlobRef | None = None
    raw_events_blob: BlobRef | None = None
    stderr_blob: BlobRef | None = None
    command: list[str] | None = None
    thread_id: str | None = None
    resumed: bool = False
    continuity_mode: str = "ephemeral"
    provider_trace_summary: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "prompt_blob": self.prompt_blob.model_dump(mode="json") if self.prompt_blob else None,
            "context_lens_blob": (
                self.context_lens_blob.model_dump(mode="json") if self.context_lens_blob else None
            ),
            "schema_blob": self.schema_blob.model_dump(mode="json") if self.schema_blob else None,
            "boundary_blob": (
                self.boundary_blob.model_dump(mode="json") if self.boundary_blob else None
            ),
            "raw_events_blob": (
                self.raw_events_blob.model_dump(mode="json") if self.raw_events_blob else None
            ),
            "stderr_blob": self.stderr_blob.model_dump(mode="json") if self.stderr_blob else None,
            "provider_command": self.command or [],
            "provider_thread_id": self.thread_id,
            "provider_resumed": self.resumed,
            "continuity_mode": self.continuity_mode,
            "provider_trace_summary": self.provider_trace_summary or {},
        }


class ProviderCallExecutor:
    """Own provider mechanics without owning run semantics."""

    def __init__(
        self,
        *,
        config: HarnessConfig,
        provider: ModelProvider,
        blobs: BlobStore,
        observer: LiveObserver,
        run_dir: Path,
        state: Callable[[], RunState],
        calls_remaining: Callable[[], int],
        append: EventAppender,
    ) -> None:
        self.config = config
        self.provider = provider
        self.blobs = blobs
        self.observer = observer
        self.run_dir = run_dir
        self._state = state
        self._calls_remaining = calls_remaining
        self._append = append

    async def invoke(
        self,
        workspace: CallWorkspace,
        spec: CallSpec[ResponseT],
    ) -> tuple[ProviderCallResult[ResponseT], CallTrace]:
        prompt_path = workspace.context_dir / "ROLE_PROMPT.md"
        boundary_path = workspace.output_dir / "boundary.json"
        schema_path = workspace.output_dir / "boundary.schema.json"
        atomic_write_text(prompt_path, spec.prompt)
        request = self._request(workspace, spec, boundary_path, schema_path)
        state = self._state()
        action_id_value = spec.metadata.get("action_id")
        action_id = str(action_id_value) if action_id_value else None
        self.observer.begin_call(
            phase=state.phase.value,
            call_id=workspace.call_id,
            call_kind=spec.call_kind,
            action_id=action_id,
        )
        try:
            result, reconstructed, resume_trace = await self._run_with_recovery(
                request=request,
                prompt_path=prompt_path,
                schema_path=schema_path,
                boundary_path=boundary_path,
                use_lead=spec.use_lead,
            )
        except BaseException as error:
            self._attach_failure(
                error,
                prompt_path=prompt_path,
                schema_path=schema_path,
                boundary_path=boundary_path,
            )
            self.observer.finish_call(
                workspace.call_id,
                phase=self._state().phase.value,
                failed=True,
            )
            raise
        trace = self._trace_from_result(
            prompt_path=prompt_path,
            schema_path=schema_path,
            boundary_path=boundary_path,
            result=result,
        )
        self.observer.finish_call(workspace.call_id, phase=self._state().phase.value)
        if spec.use_lead and self.config.cognition.persistent_lead:
            self._update_continuity(
                workspace=workspace,
                result=result,
                trace=trace,
                reconstructed=reconstructed,
                resume_failure_trace=resume_trace,
            )
        return result, trace

    def _request(
        self,
        workspace: CallWorkspace,
        spec: CallSpec[ResponseT],
        boundary_path: Path,
        schema_path: Path,
    ) -> ProviderCallRequest[ResponseT]:
        provider_call_limit = min(
            spec.max_provider_calls or self.config.provider.schema_attempts,
            self._calls_remaining(),
        )
        if provider_call_limit < 1:
            raise ProviderError("No provider-call budget remains for this harness turn")
        state = self._state()
        action_id_value = spec.metadata.get("action_id")
        action_id = str(action_id_value) if action_id_value else None
        resume_thread_id = spec.resume_thread_id
        if (
            resume_thread_id is None
            and spec.use_lead
            and self.config.cognition.persistent_lead
            and self.config.provider.resume_lead_sessions
        ):
            resume_thread_id = state.lead_session.thread_id
        return ProviderCallRequest[ResponseT](
            call_id=workspace.call_id,
            call_kind=spec.call_kind,
            role=spec.role,
            prompt=spec.prompt,
            cwd=workspace.cwd,
            response_model=spec.response_model,
            output_path=boundary_path,
            schema_path=schema_path,
            sandbox=spec.sandbox,
            network_access=spec.network_access,
            image_paths=list(spec.image_paths),
            expected_artifact_path=workspace.expected_artifact_path,
            resume_thread_id=resume_thread_id,
            preserve_session=(
                spec.use_lead
                and self.config.cognition.persistent_lead
                and self.config.provider.persist_lead_sessions
            ),
            lead_call=spec.use_lead,
            max_provider_calls=provider_call_limit,
            activity_callback=self.observer.provider_callback(
                call_id=workspace.call_id,
                call_kind=spec.call_kind,
                action_id=action_id,
            ),
            metadata={
                **spec.metadata,
                "provider_session_dir": str(self.run_dir / "provider-sessions"),
                "provider_lead_cwd": str(self.run_dir / "provider-sessions" / "lead-workspace"),
            },
        )

    async def _run_with_recovery(
        self,
        *,
        request: ProviderCallRequest[ResponseT],
        prompt_path: Path,
        schema_path: Path,
        boundary_path: Path,
        use_lead: bool,
    ) -> tuple[ProviderCallResult[ResponseT], bool, CallTrace]:
        try:
            return await self.provider.run(request), False, CallTrace()
        except (ProviderCallError, ProviderError) as resume_error:
            if not self._can_reconstruct(use_lead=use_lead, request=request):
                self._attach_failure(
                    resume_error,
                    prompt_path=prompt_path,
                    schema_path=schema_path,
                    boundary_path=boundary_path,
                )
                raise
            resume_usage, resume_trace = self._trace_from_error(
                prompt_path=prompt_path,
                schema_path=schema_path,
                boundary_path=boundary_path,
                error=resume_error,
            )
            remaining = request.max_provider_calls - getattr(
                resume_error,
                "boundary_attempts",
                1,
            )
            if remaining < 1:
                resume_error.frontier_usage = resume_usage  # type: ignore[attr-defined]
                resume_error.frontier_trace = resume_trace  # type: ignore[attr-defined]
                raise
            recovery_prompt = (
                "CONTINUITY RECOVERY: the prior Lead session could not be resumed. "
                "Reconstruct the exact current task model from the explicit capsule, preserve "
                "the artifact and all accepted evidence, and include a strict continuity "
                "acknowledgement.\n\n" + request.prompt
            )
            atomic_write_text(prompt_path, recovery_prompt)
            recovery = request.model_copy(
                update={
                    "prompt": recovery_prompt,
                    "resume_thread_id": None,
                    "preserve_session": True,
                    "max_provider_calls": remaining,
                    "metadata": {**request.metadata, "continuity_recovery": True},
                }
            )
            try:
                result = await self.provider.run(recovery)
            except BaseException as recovery_error:
                recovery_usage, recovery_trace = self._trace_from_error(
                    prompt_path=prompt_path,
                    schema_path=schema_path,
                    boundary_path=boundary_path,
                    error=recovery_error,
                )
                recovery_trace.continuity_mode = "reconstruction-failed"
                recovery_error.resume_error = resume_error  # type: ignore[attr-defined]
                recovery_error.frontier_usage = resume_usage.plus(  # type: ignore[attr-defined]
                    recovery_usage
                )
                recovery_error.frontier_trace = recovery_trace  # type: ignore[attr-defined]
                raise
            if resume_usage.calls or resume_usage.wall_seconds:
                result = result.model_copy(
                    update={
                        "usage": resume_usage.plus(result.usage),
                        "duration_seconds": resume_usage.wall_seconds + result.duration_seconds,
                    }
                )
            return result, True, resume_trace

    def _can_reconstruct(
        self,
        *,
        use_lead: bool,
        request: ProviderCallRequest[Any],
    ) -> bool:
        return bool(
            use_lead
            and request.resume_thread_id
            and self.config.provider.resume_fallback_to_reconstruction
            and self.config.cognition.fallback_to_sparse
        )

    def _update_continuity(
        self,
        *,
        workspace: CallWorkspace,
        result: ProviderCallResult[Any],
        trace: CallTrace,
        reconstructed: bool,
        resume_failure_trace: CallTrace,
    ) -> None:
        state = self._state()
        acknowledgement = getattr(result.response, "continuity_ack", None)
        ack_status, problems = validate_lead_ack(
            acknowledgement,
            state=state,
            artifact=state.current_artifact,
        )
        status = ack_status
        if reconstructed:
            status = (
                LeadContinuityStatus.RECONSTRUCTED_VERIFIED
                if ack_status == LeadContinuityStatus.CONTINUOUS
                else LeadContinuityStatus.DEGRADED
            )
            trace.continuity_mode = "reconstructed"
        else:
            trace.continuity_mode = "resumed" if result.resumed else "new-lead"
        lead = state.lead_session.model_copy(deep=True)
        lead.thread_id = result.thread_id or lead.thread_id
        lead.status = status
        lead.turns += 1
        lead.last_call_id = workspace.call_id
        lead.last_ack = acknowledgement
        if reconstructed and status != LeadContinuityStatus.RECONSTRUCTED_VERIFIED:
            lead.reconstruction_failures += 1
        lead.degraded_reason = "; ".join(problems) if problems else None
        self._append(
            et.LEAD_RECONSTRUCTION if reconstructed else et.LEAD_SESSION_UPDATED,
            {
                "lead_session": lead.model_dump(mode="json"),
                "ack_problems": problems,
                "call_id": workspace.call_id,
                "resume_failure_trace": (resume_failure_trace.payload() if reconstructed else None),
            },
            actor="continuity",
        )

    def _attach_failure(
        self,
        error: BaseException,
        *,
        prompt_path: Path,
        schema_path: Path,
        boundary_path: Path,
    ) -> None:
        if hasattr(error, "frontier_trace"):
            return
        usage, trace = self._trace_from_error(
            prompt_path=prompt_path,
            schema_path=schema_path,
            boundary_path=boundary_path,
            error=error,
        )
        error.frontier_usage = usage  # type: ignore[attr-defined]
        error.frontier_trace = trace  # type: ignore[attr-defined]

    @staticmethod
    def failure_parts(error: BaseException) -> tuple[Usage, CallTrace]:
        return (
            cast(Usage, getattr(error, "frontier_usage", Usage())),
            cast(CallTrace, getattr(error, "frontier_trace", CallTrace())),
        )

    def _trace_from_result(
        self,
        *,
        prompt_path: Path,
        schema_path: Path,
        boundary_path: Path,
        result: ProviderCallResult[Any],
    ) -> CallTrace:
        return CallTrace(
            prompt_blob=self._capture(
                prompt_path,
                media_type="text/markdown; charset=utf-8",
                original_name="role-prompt.md",
            ),
            context_lens_blob=self._capture(
                prompt_path.parent / "CONTEXT_LENS.json",
                media_type="application/json",
                original_name="context-lens.json",
            ),
            schema_blob=self._capture(
                schema_path,
                media_type="application/schema+json",
                original_name="boundary.schema.json",
            ),
            boundary_blob=self._capture(
                boundary_path,
                media_type="application/json",
                original_name="boundary.json",
            ),
            raw_events_blob=(
                self._capture(
                    result.raw_events_path,
                    media_type="application/x-ndjson",
                    original_name="codex-events.jsonl",
                )
                if self.config.runtime.retain_raw_codex_events
                else None
            ),
            stderr_blob=self._capture(
                result.stderr_path,
                media_type="text/plain; charset=utf-8",
                original_name="codex-stderr.log",
            ),
            command=result.command,
            thread_id=result.thread_id,
            resumed=result.resumed,
            continuity_mode="resumed" if result.resumed else "new-session",
            provider_trace_summary=result.trace_summary.model_dump(mode="json"),
        )

    def _trace_from_error(
        self,
        *,
        prompt_path: Path,
        schema_path: Path,
        boundary_path: Path,
        error: BaseException,
    ) -> tuple[Usage, CallTrace]:
        if isinstance(error, ProviderCallError):
            usage = error.usage
            raw_events_path = error.raw_events_path
            stderr_path = error.stderr_path
            command = error.command
            trace_summary = error.trace_summary
            thread_id = error.thread_id
        else:
            usage = Usage()
            raw_events_path = None
            stderr_path = None
            command = []
            trace_summary = {}
            thread_id = None
        return usage, CallTrace(
            prompt_blob=self._capture(
                prompt_path,
                media_type="text/markdown; charset=utf-8",
                original_name="role-prompt.md",
            ),
            context_lens_blob=self._capture(
                prompt_path.parent / "CONTEXT_LENS.json",
                media_type="application/json",
                original_name="context-lens.json",
            ),
            schema_blob=self._capture(
                schema_path,
                media_type="application/schema+json",
                original_name="boundary.schema.json",
            ),
            boundary_blob=self._capture(
                boundary_path,
                media_type="application/json",
                original_name="boundary.json",
            ),
            raw_events_blob=(
                self._capture(
                    raw_events_path,
                    media_type="application/x-ndjson",
                    original_name="codex-events.jsonl",
                )
                if self.config.runtime.retain_raw_codex_events
                else None
            ),
            stderr_blob=self._capture(
                stderr_path,
                media_type="text/plain; charset=utf-8",
                original_name="codex-stderr.log",
            ),
            command=command,
            thread_id=thread_id,
            provider_trace_summary=trace_summary,
        )

    def _capture(
        self,
        path: Path | None,
        *,
        media_type: str,
        original_name: str,
    ) -> BlobRef | None:
        if path is None or not path.is_file():
            return None
        return self.blobs.put_file(
            path,
            media_type=media_type,
            original_name=original_name,
        )
