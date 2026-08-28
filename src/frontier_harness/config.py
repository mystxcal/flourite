"""Configuration for the one executable Flourite architecture."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, field_validator

from .errors import ConfigurationError
from .models import Role, StrictModel
from .util import deep_merge


class RoleRouting(StrictModel):
    model: str | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"


DEFAULT_TRUSTED_TOOLS = [
    "read",
    "bash",
    "edit",
    "write",
    "grep",
    "glob",
    "lsp",
    "ast_edit",
    "debug",
    "eval",
    "browser",
    "task",
    "web_search",
]


class CapabilityPolicy(StrictModel):
    mode: Literal["trusted", "contained"] = "trusted"
    tools: list[str] = Field(default_factory=lambda: list(DEFAULT_TRUSTED_TOOLS))
    inherit_environment: bool = True
    discover_rules: bool = False
    discover_skills: bool = False
    discover_extensions: bool = False
    task_max_concurrency: int = Field(default=4, ge=1, le=32)
    task_max_recursion_depth: int = Field(default=1, ge=0, le=4)
    task_soft_request_budget: int = Field(default=80, ge=10, le=500)
    task_max_runtime_ms: int = Field(default=900_000, ge=0)
    retry_max_retries: int = Field(default=6, ge=0, le=20)

    @field_validator("tools")
    @classmethod
    def normalize_tools(cls, value: list[str]) -> list[str]:
        tools = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not tools:
            raise ValueError("capabilities.tools cannot be empty")
        return tools


class ProviderConfig(StrictModel):
    kind: Literal["omp-codex", "fake"] = "omp-codex"
    command: str = "omp"
    codex_auth_path: Path = Path("~/.codex/auth.json")
    provider_state_root: Path = Path("~/.cache/flourite/provider")
    default_model: str = "gpt-5.6-sol"
    capabilities: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    schema_attempts: int = Field(default=2, ge=1, le=3)
    timeout_seconds: int = Field(default=1800, ge=30)
    resume_fallback_to_reconstruction: bool = True
    default_network_access: bool = True
    strong: RoleRouting = Field(
        default_factory=lambda: RoleRouting(model="gpt-5.6-sol", reasoning_effort="xhigh")
    )
    worker: RoleRouting = Field(
        default_factory=lambda: RoleRouting(model="gpt-5.6-sol", reasoning_effort="high")
    )
    cheap: RoleRouting = Field(
        default_factory=lambda: RoleRouting(model="gpt-5.6-sol", reasoning_effort="medium")
    )

    def route(self, role: Role) -> RoleRouting:
        return cast(RoleRouting, getattr(self, role.value))


class RunPolicy(StrictModel):
    run_root: Path = Path(".flourite/runs")
    max_attachment_bytes: int = Field(default=100_000_000, ge=1)
    max_attachment_files: int = Field(default=2_000, ge=1)
    excluded_source_globs: list[str] = Field(
        default_factory=lambda: [
            ".git/**",
            ".flourite/**",
            ".frontier/**",
            "node_modules/**",
            ".venv/**",
            "venv/**",
            "__pycache__/**",
            ".pytest_cache/**",
            ".mypy_cache/**",
            ".ruff_cache/**",
            "dist/**",
            "build/**",
        ]
    )
    export_redacts_secrets: bool = True

    @field_validator("run_root", mode="after")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(str(value))))


class SoftwarePolicy(StrictModel):
    preflight_checks: list[str] = Field(default_factory=list)
    candidate_checks: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    check_timeout_seconds: int = Field(default=900, ge=10)
    include_untracked: bool = True
    release_artifacts: list[str] = Field(default_factory=list)
    max_release_artifact_bytes: int = Field(default=250_000_000, ge=1)
    allow_dirty_source: bool = True
    excluded_untracked_globs: list[str] = Field(
        default_factory=lambda: [
            ".flourite/**",
            ".frontier/**",
            ".git/**",
            "node_modules/**",
            ".venv/**",
            "venv/**",
            "__pycache__/**",
            ".pytest_cache/**",
            ".mypy_cache/**",
            ".ruff_cache/**",
            "dist/**",
            "build/**",
        ]
    )


class RuntimePolicy(StrictModel):
    sqlite_busy_timeout_ms: int = Field(default=10_000, ge=100)
    auto_repair: bool = True
    repair_command: str = "codex"
    repair_model: str = "gpt-5.6-sol"
    repair_reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "xhigh"
    repair_timeout_seconds: int = Field(default=1800, ge=30)
    repair_no_progress_limit: int = Field(default=2, ge=1, le=4)


class KernelPolicy(StrictModel):
    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_model_turns: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_parallel: int = Field(default=1, ge=1, le=32)
    max_event_payload_bytes: int = Field(default=256_000, ge=10_000)


class HarnessConfig(StrictModel):
    run: RunPolicy = Field(default_factory=RunPolicy)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    software: SoftwarePolicy = Field(default_factory=SoftwarePolicy)
    runtime: RuntimePolicy = Field(default_factory=RuntimePolicy)
    kernel: KernelPolicy = Field(default_factory=KernelPolicy)


DEFAULT_CONFIG: dict[str, Any] = HarnessConfig().model_dump(mode="json")


def load_config(
    path: Path | None = None, *, overrides: dict[str, Any] | None = None
) -> HarnessConfig:
    data: dict[str, Any] = dict(DEFAULT_CONFIG)
    if path is not None:
        try:
            with path.open("rb") as handle:
                data = deep_merge(data, tomllib.load(handle))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"Configuration file not found: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from exc
    if overrides:
        data = deep_merge(data, overrides)
    try:
        return HarnessConfig.model_validate(data)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


EXAMPLE_CONFIG = """# Flourite configuration

[run]
run_root = ".flourite/runs"

[kernel]
max_parallel = 1
# Optional operator-owned hard envelopes; unset means no hidden harness limit.
# max_wall_seconds = 7200
# max_input_tokens = 1000000
# max_output_tokens = 100000
# max_model_turns = 1000
# max_cost_usd = 100

[provider]
kind = "omp-codex"
command = "omp"
codex_auth_path = "~/.codex/auth.json"
provider_state_root = "~/.cache/flourite/provider"
default_model = "gpt-5.6-sol"
schema_attempts = 2
timeout_seconds = 1800
resume_fallback_to_reconstruction = true
default_network_access = true

[provider.capabilities]
mode = "trusted"
inherit_environment = true
discover_rules = false
discover_skills = false
discover_extensions = false
tools = ["read", "bash", "edit", "write", "grep", "glob", "lsp", "ast_edit", "debug", "eval", "browser", "task", "web_search"]
task_max_concurrency = 4
task_max_recursion_depth = 1
task_soft_request_budget = 80
task_max_runtime_ms = 900000
retry_max_retries = 6

[provider.strong]
model = "gpt-5.6-sol"
reasoning_effort = "xhigh"

[software]
preflight_checks = []
candidate_checks = []
checks = []
check_timeout_seconds = 900
include_untracked = true
release_artifacts = []
max_release_artifact_bytes = 250000000
"""
