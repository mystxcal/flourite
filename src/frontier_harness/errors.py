"""Package-specific exceptions.

Expected failures carry enough structured context for the runtime to preserve
evidence and continue safely.  They intentionally remain small and dependency
free so provider and adapter layers can raise them without importing the
engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from .models import Usage


class FrontierError(Exception):
    """Base exception for expected Sparse Frontier failures."""


class ConfigurationError(FrontierError):
    """Raised when configuration is invalid or internally inconsistent."""


class ProviderError(FrontierError):
    """Raised when the model provider cannot complete a call."""


class ProviderCallError(ProviderError):
    """A provider call started but did not yield a valid boundary object.

    Partial token accounting and raw traces are retained when available.  This
    lets the event ledger represent failed work honestly instead of making it
    disappear from cost and debugging views.
    """

    def __init__(
        self,
        message: str,
        *,
        usage: Usage | None = None,
        raw_events_path: Path | None = None,
        stderr_path: Path | None = None,
        command: list[str] | None = None,
        trace_summary: dict[str, Any] | None = None,
        boundary_attempts: int = 1,
        thread_id: str | None = None,
        failure_kind: Literal["boundary", "process", "transport", "unknown"] = "unknown",
    ) -> None:
        super().__init__(message)
        self.usage = usage or Usage(calls=1)
        self.raw_events_path = raw_events_path
        self.stderr_path = stderr_path
        self.command = command or []
        self.trace_summary = trace_summary or {}
        self.boundary_attempts = max(1, boundary_attempts)
        self.thread_id = thread_id
        self.failure_kind = failure_kind


class LedgerIntegrityError(FrontierError):
    """Raised when the event ledger or content-addressed blobs fail verification."""


class RunNotFoundError(FrontierError):
    """Raised when a requested run cannot be found."""


class WorkspaceError(FrontierError):
    """Raised for unsafe or invalid workspace operations."""


class OperatorStop(FrontierError):
    """Raised after a requested safe stop leaves the run resumable."""
