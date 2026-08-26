"""Durable provenance captured around one provider call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import BlobRef


@dataclass(slots=True)
class CallTrace:
    prompt_blob: BlobRef | None = None
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
