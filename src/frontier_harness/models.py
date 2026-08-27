"""Small boundary types shared by adapters and the model transport.

Authoritative run state lives in :mod:`frontier_harness.core.types`.  This
module contains only the compatibility-shaped values needed at I/O boundaries.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .core.types import ContentRef

BlobRef = ContentRef
ArtifactScope = Literal["targeted", "sequence", "whole_artifact", "release"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Role(StrEnum):
    STRONG = "strong"
    WORKER = "worker"
    CHEAP = "cheap"


class SandboxPolicy(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class IndependenceClass(StrEnum):
    SAME_MODEL = "same_model"
    DIFFERENT_CONDITIONING = "different_conditioning"
    DETERMINISTIC_TOOL = "deterministic_tool"
    EXTERNAL_EVIDENCE = "external_evidence"
    HUMAN = "human"
    REAL_WORLD = "real_world"


class EvidenceModality(StrEnum):
    SOURCE = "source"
    STRUCTURED_DATA = "structured_data"
    DETERMINISTIC_TEST = "deterministic_test"
    STATIC_VISUAL = "static_visual"
    TEMPORAL_VISUAL = "temporal_visual"
    AUDIO = "audio"
    INTERACTIVE = "interactive"
    EXTERNAL_OBSERVATION = "external_observation"
    HUMAN_OBSERVATION = "human_observation"


class ArtifactRef(StrictModel):
    artifact_id: str
    version: int = Field(ge=1)
    blob: BlobRef
    kind: str = "markdown"
    summary: str = ""
    parent_artifact_id: str | None = None
    source_action_ids: list[str] = Field(default_factory=list)
    deliverables: list[BlobRef] = Field(default_factory=list)
    created_at: str


class EvidenceRecord(StrictModel):
    evidence_id: str
    source_action_id: str | None = None
    kind: str
    summary: str
    scope: str
    artifact_scope: ArtifactScope = "targeted"
    independence_class: IndependenceClass
    references: list[str] = Field(default_factory=list)
    blob: BlobRef | None = None
    negative_result: bool = False
    modalities: list[EvidenceModality] = Field(default_factory=list)
    establishes: list[str] = Field(default_factory=list)
    cannot_establish: list[str] = Field(default_factory=list)
    artifact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Usage(StrictModel):
    calls: int = 0
    model_requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    wall_seconds: float = 0.0

    def plus(self, other: Usage) -> Usage:
        return Usage(
            calls=self.calls + other.calls,
            model_requests=self.model_requests + other.model_requests,
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens + other.reasoning_output_tokens,
            wall_seconds=self.wall_seconds + other.wall_seconds,
        )
