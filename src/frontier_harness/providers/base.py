"""Provider-neutral call contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ..models import Role, SandboxPolicy, Usage

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class ToolCallSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    intent: str = ""
    success: bool | None = None
    duration_ms: float | None = None
    arguments_sha256: str | None = None
    result_sha256: str | None = None
    nested_usage: Usage = Field(default_factory=Usage)


class ProviderTraceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_turns: int = 0
    parent_model_turns: int = 0
    nested_model_turns: int = 0
    nested_usage: Usage = Field(default_factory=Usage)
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    tool_errors: int = 0
    stop_reason: str | None = None


class ProviderCallRequest(BaseModel, Generic[ResponseT]):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    call_id: str
    call_kind: str
    role: Role
    prompt: str
    cwd: Path
    response_model: type[ResponseT]
    output_path: Path
    schema_path: Path
    sandbox: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE
    network_access: bool = False
    image_paths: list[Path] = Field(default_factory=list)
    expected_artifact_path: Path | None = None
    resume_thread_id: str | None = None
    preserve_session: bool = False
    lead_call: bool = False
    max_provider_calls: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    activity_callback: Callable[[dict[str, Any]], None] | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )


class ProviderCallResult(BaseModel, Generic[ResponseT]):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    call_id: str
    response: ResponseT
    usage: Usage
    duration_seconds: float
    return_code: int = 0
    boundary_attempts: int = Field(default=1, ge=1)
    thread_id: str | None = None
    resumed: bool = False
    raw_events_path: Path | None = None
    stderr_path: Path | None = None
    command: list[str] = Field(default_factory=list)
    trace_summary: ProviderTraceSummary = Field(default_factory=ProviderTraceSummary)


class ProviderDoctorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    provider: str
    version: str | None = None
    auth_mode: str | None = None
    details: list[str] = Field(default_factory=list)


class ModelProvider(ABC):
    @abstractmethod
    async def doctor(self) -> ProviderDoctorResult:
        raise NotImplementedError

    @abstractmethod
    async def run(self, request: ProviderCallRequest[ResponseT]) -> ProviderCallResult[ResponseT]:
        raise NotImplementedError
