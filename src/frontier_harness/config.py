"""Configuration loading and validation."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from .errors import ConfigurationError
from .models import BudgetContract, Role, StrictModel
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
    """Model-facing execution substrate.

    Trusted mode assumes the harness itself runs inside an operator-controlled
    VM, VPS, or disposable host. It deliberately favors capability and tight
    feedback over an inner permission maze. Contained mode preserves the old
    reduced surface for deployments that need it.
    """

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
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in value:
            name = raw.strip()
            if not name or name in seen:
                continue
            normalized.append(name)
            seen.add(name)
        if not normalized:
            raise ValueError("capabilities.tools cannot be empty")
        return normalized


class ProviderConfig(StrictModel):
    kind: Literal["omp-codex", "fake"] = "omp-codex"
    command: str = "omp"
    codex_auth_path: Path = Path("~/.codex/auth.json")
    provider_state_root: Path = Path("~/.cache/flourite/provider")
    default_model: str = "gpt-5.6-sol"
    capabilities: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    schema_attempts: int = Field(default=2, ge=1, le=3)
    timeout_seconds: int = Field(default=1800, ge=30)
    persist_lead_sessions: bool = True
    resume_lead_sessions: bool = True
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

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_codex_cli(cls, value: Any) -> Any:
        # The Codex CLI injects its own project context and therefore cannot
        # satisfy the harness's explicit-context contract. Preserve old config
        # files, but route them through the audited OMP subscription transport.
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if migrated.get("kind") == "codex-cli":
            migrated["kind"] = "omp-codex"
            if migrated.get("command", "codex") == "codex":
                migrated["command"] = "omp"
        legacy_sandbox = migrated.pop("use_os_sandbox", None)
        if legacy_sandbox is not None:
            capabilities = dict(migrated.get("capabilities") or {})
            capabilities.setdefault("mode", "contained" if legacy_sandbox else "trusted")
            migrated["capabilities"] = capabilities
        for obsolete in (
            "require_chatgpt_auth",
            "force_chatgpt_login_method",
            "strip_api_key_environment",
            "ephemeral",
            "profile",
            "ignore_user_config",
            "ignore_rules",
            "honor_software_rules",
            "approval_policy",
        ):
            migrated.pop(obsolete, None)
        return migrated

    def route(self, role: Role) -> RoleRouting:
        return cast(RoleRouting, getattr(self, role.value))


class FrontierPolicy(StrictModel):
    max_open_issues: int = Field(default=8, ge=1, le=30)
    max_candidate_deltas: int = Field(default=4, ge=1, le=20)
    max_actions_per_batch: int = Field(default=3, ge=1, le=12)
    max_actions_per_target: int = Field(default=2, ge=1, le=5)
    max_stalled_actions_per_target: int = Field(default=2, ge=1, le=8)
    correlation_discount: bool = True
    require_decision_relevance: bool = True
    prefer_cheapest_sufficient: bool = True
    # A periodic rewrite is process, not evidence.  Set an interval only for a
    # domain that has demonstrated time-based coherence decay; adaptive runs
    # otherwise rebuild when the artifact spine or controller says they must.
    clean_synthesis_every_rounds: int | None = Field(default=None, ge=1)
    minimum_action_impact: Literal["low", "medium", "high"] = "medium"
    expensive_probe_minimum_impact: Literal["medium", "high", "fatal"] = "high"


class RunPolicy(StrictModel):
    adapter: str = "generic"
    semantic_profiles: list[
        Literal["generic", "research", "formal", "decision", "creative", "media"]
    ] = Field(default_factory=list)
    run_root: Path = Path(".flourite/runs")
    keep_capsules: bool = True
    release_gate: Literal["auto", "always", "never"] = "auto"
    final_output: Path | None = None
    budget: BudgetContract = Field(default_factory=BudgetContract)
    fail_fast_on_provider_error: bool = False
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

    @field_validator("run_root", "final_output", mode="after")
    @classmethod
    def expand_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return Path(os.path.expandvars(os.path.expanduser(str(value))))


class ObjectivePolicy(StrictModel):
    command: str | None = None
    primary_metric: str = "score"
    direction: Literal["maximize", "minimize"] = "maximize"
    timeout_seconds: int = Field(default=900, ge=1)


class SoftwarePolicy(StrictModel):
    preflight_checks: list[str] = Field(default_factory=list)
    candidate_checks: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    check_timeout_seconds: int = Field(default=900, ge=10)
    include_untracked: bool = True
    apply_final_patch: bool = False
    release_artifacts: list[str] = Field(default_factory=list)
    max_release_artifact_bytes: int = Field(default=250_000_000, ge=1)
    allow_dirty_source: bool = True
    objective: ObjectivePolicy = Field(default_factory=ObjectivePolicy)
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
    retain_raw_codex_events: bool = True
    max_event_payload_bytes: int = Field(default=2_000_000, ge=10_000)
    capsule_artifact_char_limit: int = Field(default=200_000, ge=10_000)
    evidence_per_capsule: int = Field(default=12, ge=1, le=100)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class ResourcePolicy(StrictModel):
    """Adaptive compute metabolism inside the operator's hard envelope."""

    mode: Literal["static", "adaptive"] = "adaptive"
    initial_call_grant: int | None = Field(default=None, ge=4)
    grant_step_calls: int | None = Field(default=None, ge=2)
    max_stagnant_grants: int | None = Field(default=None, ge=0, le=8)


class CognitionPolicy(StrictModel):
    mode: Literal["adaptive", "legacy"] = "adaptive"
    persistent_lead: bool = True
    max_active_cruxes: int = Field(default=3, ge=1, le=8)
    normal_overlay_limit: int = Field(default=2, ge=0, le=8)
    hard_overlay_limit: int = Field(default=4, ge=1, le=16)
    specialist_reuse_threshold: int = Field(default=2, ge=1, le=10)
    semantic_regression: bool = True
    completion_case: bool = True
    instruments_enabled: bool = True
    ceiling_scan: bool = True
    action_contracts: bool = True
    fallback_to_sparse: bool = True
    lead_reconstruction_check_after_turns: int = Field(default=8, ge=2, le=50)
    max_material_repairs: int | None = Field(default=None, ge=0, le=8)
    max_control_call_fraction: float = Field(default=0.30, ge=0.0, le=0.75)
    require_behavioral_overlay_difference: bool = True
    require_reframe_witness: bool = True
    require_completion_coverage: bool = True
    inline_control_updates: bool = True
    human_evidence_available: bool = False
    thought_first: bool = True
    # The model keeps the full trusted tool plane.  This optional strict gate is
    # off by default because epistemic mode is guidance, not a permission
    # system; enable it only for deliberately cost-constrained experiments.
    require_execution_trigger: bool = False
    frontier_keeper: Literal["auto", "fresh", "continuous"] = "auto"

    @model_validator(mode="after")
    def overlay_limits(self) -> CognitionPolicy:
        if self.normal_overlay_limit > self.hard_overlay_limit:
            raise ValueError("normal_overlay_limit cannot exceed hard_overlay_limit")
        return self


class SummitPolicy(StrictModel):
    mode: Literal["off", "auto", "on"] = "auto"
    profile: Literal["lean", "deep", "max", "frontier"] = "deep"
    max_archive_lineages: int = Field(default=8, ge=1, le=32)
    max_active_lineages: int = Field(default=4, ge=1, le=12)
    max_per_niche: int = Field(default=2, ge=1, le=6)
    stepping_stone_probe_allowance: int = Field(default=1, ge=0, le=5)
    stepping_stone_development_allowance: int = Field(default=1, ge=0, le=5)
    require_concrete_auto_trigger: bool = True
    enable_reconstruction: bool = True
    enable_ceiling_audit: bool = True
    enable_mechanism_grafting: bool = True
    preserve_falsification_residue: bool = True
    experimental_frontier: bool = True
    max_discovery_actions_per_round: int = Field(default=1, ge=0, le=3)
    stagnation_before_mutation: int = Field(default=2, ge=1, le=8)
    enable_semantic_mutation: bool = True
    enable_semantic_crossover: bool = True

    @model_validator(mode="after")
    def archive_limits(self) -> SummitPolicy:
        if self.max_active_lineages > self.max_archive_lineages:
            raise ValueError("max_active_lineages cannot exceed max_archive_lineages")
        return self


class KernelPolicy(StrictModel):
    """Hard envelope and compact-state settings for the phase-free kernel."""

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
    frontier: FrontierPolicy = Field(default_factory=FrontierPolicy)
    software: SoftwarePolicy = Field(default_factory=SoftwarePolicy)
    runtime: RuntimePolicy = Field(default_factory=RuntimePolicy)
    resource: ResourcePolicy = Field(default_factory=ResourcePolicy)
    cognition: CognitionPolicy = Field(default_factory=CognitionPolicy)
    summit: SummitPolicy = Field(default_factory=SummitPolicy)
    kernel: KernelPolicy = Field(default_factory=KernelPolicy)

    @model_validator(mode="after")
    def validate_reserve_and_batch(self) -> HarnessConfig:
        if (
            self.resource.mode == "static"
            and self.run.budget.synthesis_reserve_calls >= self.run.budget.max_calls
        ):
            raise ValueError("The static call budget leaves no room before synthesis reserve")
        if self.frontier.max_actions_per_batch > self.run.budget.max_parallel * 2:
            raise ValueError(
                "max_actions_per_batch is implausibly larger than max_parallel; "
                "reduce it to avoid oversized queued batches"
            )
        if self.cognition.mode == "legacy" and self.summit.mode == "on":
            raise ValueError("summit.mode='on' requires cognition.mode='adaptive'")
        if (
            self.resource.mode == "static"
            and self.run.budget.synthesis_reserve_calls < 2
            and self.cognition.semantic_regression
        ):
            raise ValueError("semantic regression requires at least two reserved calls")
        if (
            self.resource.initial_call_grant is not None
            and self.resource.initial_call_grant > self.run.budget.max_calls
        ):
            raise ValueError("resource.initial_call_grant cannot exceed the hard call envelope")
        if (
            self.resource.grant_step_calls is not None
            and self.resource.grant_step_calls > self.run.budget.max_calls
        ):
            raise ValueError("resource.grant_step_calls cannot exceed the hard call envelope")
        return self


DEFAULT_CONFIG: dict[str, Any] = HarnessConfig().model_dump(mode="json")


def load_config(
    path: Path | None = None, *, overrides: dict[str, Any] | None = None
) -> HarnessConfig:
    data: dict[str, Any] = dict(DEFAULT_CONFIG)
    if path is not None:
        try:
            with path.open("rb") as handle:
                loaded = tomllib.load(handle)
        except FileNotFoundError as exc:
            raise ConfigurationError(f"Configuration file not found: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from exc
        data = deep_merge(data, loaded)
    if overrides:
        data = deep_merge(data, overrides)
    try:
        return HarnessConfig.model_validate(data)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


EXAMPLE_CONFIG = """# Flourite configuration

[run]
adapter = "generic"
semantic_profiles = []          # e.g. ["creative", "media"] with any artifact adapter
run_root = ".flourite/runs"
keep_capsules = true
release_gate = "auto"          # auto | always | never

[run.budget]
max_calls = 48                    # hard operator envelope; adaptive runs stop earlier
max_parallel = 3
synthesis_reserve_calls = 4       # legacy/static mode only
# max_rounds = 8                   # optional independent safety ceiling
# max_input_tokens = 500000
# max_output_tokens = 60000
# max_wall_seconds = 7200

[kernel]
# max_wall_seconds = 43200          # optional operator-owned hard envelope
max_parallel = 1                    # search widens only when the task needs it
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
persist_lead_sessions = true
resume_lead_sessions = true
resume_fallback_to_reconstruction = true
default_network_access = true

[provider.capabilities]
mode = "trusted"                    # trusted | contained
inherit_environment = true          # trusted mode uses the VM/VPS as its boundary
discover_rules = false              # the harness supplies exact explicit context
discover_skills = false
discover_extensions = false
tools = ["read", "bash", "edit", "write", "grep", "glob", "lsp", "ast_edit", "debug", "eval", "browser", "task", "web_search"]
task_max_concurrency = 4
task_max_recursion_depth = 1
task_soft_request_budget = 80
task_max_runtime_ms = 900000
retry_max_retries = 6
# The harness owns durable planning, so it omits OMP's redundant todo plane.

[resource]
mode = "adaptive"                 # adaptive | static
# initial_call_grant = 9           # normally derived from topology + finish reserve
# grant_step_calls = 4             # normally derived from the feasible worker wave
# max_stagnant_grants = 2          # normally derived from unresolved material debt

[provider.strong]
model = "gpt-5.6-sol"
reasoning_effort = "xhigh"

[provider.worker]
model = "gpt-5.6-sol"
reasoning_effort = "high"

[provider.cheap]
model = "gpt-5.6-sol"
reasoning_effort = "medium"

[frontier]
max_open_issues = 8
max_candidate_deltas = 4
max_actions_per_batch = 3
max_actions_per_target = 2
max_stalled_actions_per_target = 2
correlation_discount = true
minimum_action_impact = "medium"
expensive_probe_minimum_impact = "high"
# clean_synthesis_every_rounds = 3  # opt in only when a domain proves time-based decay

[cognition]
mode = "adaptive"                   # adaptive | legacy
persistent_lead = true
max_active_cruxes = 3
normal_overlay_limit = 2
hard_overlay_limit = 4
specialist_reuse_threshold = 2
semantic_regression = true
completion_case = true
instruments_enabled = true
ceiling_scan = true
action_contracts = true
fallback_to_sparse = true
lead_reconstruction_check_after_turns = 8
# max_material_repairs = 3         # optional hard safety ceiling; normally evidence-bounded
max_control_call_fraction = 0.30
require_behavioral_overlay_difference = true
require_reframe_witness = true
require_completion_coverage = true
inline_control_updates = true
human_evidence_available = false
thought_first = true
require_execution_trigger = false        # mode guides attention; it never removes tools
frontier_keeper = "auto"             # auto | fresh | continuous

[summit]
mode = "auto"                       # off | auto | on
profile = "deep"                    # lean | deep | max | frontier
max_archive_lineages = 8
max_active_lineages = 4
max_per_niche = 2
stepping_stone_probe_allowance = 1
stepping_stone_development_allowance = 1
require_concrete_auto_trigger = true
enable_reconstruction = true
enable_ceiling_audit = true
enable_mechanism_grafting = true
preserve_falsification_residue = true
experimental_frontier = true
max_discovery_actions_per_round = 1
stagnation_before_mutation = 2
enable_semantic_mutation = true
enable_semantic_crossover = true

[software]
preflight_checks = []           # cheap contract/schema checks after the first candidate
candidate_checks = []           # cheap regression checks after each integration checkpoint
checks = []                     # e.g. ["python -m pytest -q", "ruff check ."]
check_timeout_seconds = 900
include_untracked = true
apply_final_patch = false       # never mutate the source repo unless explicit
release_artifacts = []          # e.g. ["dist/*.mp4", "dist/report.pdf"]
max_release_artifact_bytes = 250000000

[software.objective]
# command = "python evaluate.py --json"  # final non-empty stdout line must be JSON
primary_metric = "score"
direction = "maximize"          # maximize | minimize
timeout_seconds = 900
"""
