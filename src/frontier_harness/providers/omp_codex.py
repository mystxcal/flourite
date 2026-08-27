"""Auditable ChatGPT-subscription provider built on OMP's Codex transport.

The Codex CLI is a coding agent, not a neutral model transport: it injects its
own prompt, project rules, and tools. This adapter runs OMP with an empty
provider-facing system prompt and disables ambient discovery. The harness
supplies the complete user message and the configured tool set explicitly. Every
call emits a redacted context manifest, making the client-visible request
reproducible without retaining OAuth material.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, TypeVar, cast

from pydantic import BaseModel, ValidationError

from ..config import ProviderConfig
from ..errors import ProviderCallError, ProviderError
from ..models import SandboxPolicy, Usage
from ..util import atomic_write_text, canonical_json, sha256_bytes, sha256_text, utc_now
from .base import (
    ModelProvider,
    ProviderCallRequest,
    ProviderCallResult,
    ProviderDoctorResult,
    ProviderTraceSummary,
)
from .safe_events import safe_event as _safe_event
from .trace import trace_summary as _trace_summary

ResponseT = TypeVar("ResponseT", bound=BaseModel)

_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
_PROVIDER_READ_CHUNK_BYTES = 64 * 1024
_PROVIDER_MAX_EVENT_BYTES = 32 * 1024 * 1024


def _jwt_expiry(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload))
        expiry = value.get("exp")
        return int(expiry) if expiry is not None else None
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None


class CodexAuth:
    """Read and, only when needed, refresh the user's existing Codex login."""

    def __init__(self, path: Path, *, refresh_margin_seconds: int = 300) -> None:
        self.path = path.expanduser()
        self.refresh_margin_seconds = refresh_margin_seconds

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProviderError(
                f"Codex ChatGPT login was not found at {self.path}. Run `codex login`."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Could not read Codex authentication: {exc}") from exc
        if value.get("auth_mode") != "chatgpt":
            raise ProviderError("The Codex login is not a ChatGPT subscription login")
        tokens = value.get("tokens")
        if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str):
            raise ProviderError("The Codex login has no usable access token")
        return cast(dict[str, Any], value)

    def access_token(self, *, force_refresh: bool = False) -> str:
        auth = self._read()
        tokens = cast(dict[str, Any], auth["tokens"])
        access = str(tokens["access_token"])
        expiry = _jwt_expiry(access)
        if not force_refresh and (
            expiry is None or expiry > int(time.time()) + self.refresh_margin_seconds
        ):
            return access
        refresh = tokens.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise ProviderError("The Codex access token expired and no refresh token is available")
        encoded = urllib.parse.urlencode(
            {
                "client_id": _CODEX_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            }
        ).encode()
        request = urllib.request.Request(
            _CODEX_TOKEN_URL,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                refreshed = json.loads(response.read())
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Codex OAuth refresh failed: {exc}") from exc
        new_access = refreshed.get("access_token")
        if not isinstance(new_access, str) or not new_access:
            raise ProviderError("Codex OAuth refresh returned no access token")
        tokens["access_token"] = new_access
        if isinstance(refreshed.get("refresh_token"), str):
            tokens["refresh_token"] = refreshed["refresh_token"]
        if isinstance(refreshed.get("id_token"), str):
            tokens["id_token"] = refreshed["id_token"]
        auth["last_refresh"] = utc_now()
        atomic_write_text(self.path, json.dumps(auth, indent=2), mode=0o600)
        return new_access


def _context_inventory(workspace: Path) -> dict[str, dict[str, int | str]]:
    """Hash the explicit model-facing context, never ambient host files."""

    context = workspace / ".sfh_context"
    if not context.is_dir():
        return {}
    inventory: dict[str, dict[str, int | str]] = {}
    for path in sorted(item for item in context.rglob("*") if item.is_file()):
        data = path.read_bytes()
        inventory[path.relative_to(workspace).as_posix()] = {
            "sha256": sha256_bytes(data),
            "bytes": len(data),
        }
    return inventory


def _context_delta(
    current: dict[str, dict[str, int | str]],
    previous: dict[str, dict[str, int | str]],
) -> dict[str, list[str]]:
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    changed = sorted(
        path
        for path in set(current).intersection(previous)
        if current[path].get("sha256") != previous[path].get("sha256")
    )
    unchanged = sorted(
        path
        for path in set(current).intersection(previous)
        if current[path].get("sha256") == previous[path].get("sha256")
    )
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": unchanged,
    }


@dataclass(slots=True)
class PreparedOmpCall:
    request: ProviderCallRequest[Any]
    effective_prompt: str
    session_dir: Path
    omp_cwd: Path
    agent_dir: Path
    overlay_path: Path
    raw_events_path: Path
    stderr_path: Path
    manifest_path: Path
    context_state_path: Path
    context_inventory: dict[str, dict[str, int | str]]
    manifest: dict[str, Any]
    started: float
    attempt_limit: int
    session_resumable: bool

    def duration(self) -> float:
        return time.monotonic() - self.started


@dataclass(slots=True)
class OmpAttempt:
    number: int
    return_code: int | None
    safe_command: list[str]
    stderr: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    final_message: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    thread_id: str | None = None
    timed_out: bool = False

    @property
    def trace(self) -> ProviderTraceSummary:
        return _trace_summary(self.events)

    @property
    def raw_final(self) -> str:
        if self.final_message is None:
            return ""
        blocks = self.final_message.get("content", [])
        return "\n".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()


@dataclass(slots=True)
class OmpCallState:
    thread_id: str | None
    usage: Usage = field(default_factory=Usage)
    last_safe_command: list[str] = field(default_factory=list)
    validation_error: str | None = None
    continuation_note: str | None = None
    resumed_within_call: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)

    def absorb(self, attempt: OmpAttempt) -> None:
        self.last_safe_command = attempt.safe_command
        self.thread_id = attempt.thread_id or self.thread_id
        self.events.extend({"attempt": attempt.number, "event": event} for event in attempt.events)
        self.stderr.append(f"--- attempt {attempt.number} ---\n{attempt.stderr}")
        if not attempt.timed_out:
            self.usage = self.usage.plus(attempt.usage)
            self.usage.calls += 1

    def trace(self) -> ProviderTraceSummary:
        return _trace_summary([cast(dict[str, Any], item["event"]) for item in self.events])


@dataclass(frozen=True, slots=True)
class AttemptDecision:
    status: str
    diagnostic_status: str
    record: dict[str, Any]
    retry: bool = False
    response: BaseModel | None = None
    error: str | None = None
    continuation_note: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorProbe:
    """One immutable compatibility observation of the installed OMP transport."""

    version: str | None
    version_code: int
    models_code: int
    available_models: frozenset[str]
    missing_models: tuple[str, ...]
    contained: bool
    sandbox_ready: bool

    @property
    def ok(self) -> bool:
        return all(
            (
                self.version_code == 0,
                self.models_code == 0,
                bool(self.available_models),
                not self.missing_models,
                self.sandbox_ready,
            )
        )

    def details(self) -> list[str]:
        details = [
            "ChatGPT OAuth is readable through the explicit OMP Codex transport.",
            f"Codex catalog exposed {len(self.available_models)} compatible model(s).",
            (
                "Contained capability mode is active with a reduced tool and filesystem surface."
                if self.contained
                else "Trusted-host capability mode is active with yolo approval and the VM/VPS as the execution boundary."
            ),
            "The provider-facing system prompt remains empty; harness context is explicit per call.",
        ]
        if self.version_code != 0 or self.models_code != 0 or not self.available_models:
            details.append("OMP could not resolve a usable openai-codex model catalog.")
        if self.missing_models:
            details.append(f"Configured model(s) are unavailable: {', '.join(self.missing_models)}")
        if not self.sandbox_ready:
            details.append(
                "Bubblewrap is required while provider.capabilities.mode is 'contained'."
            )
        return details


class OmpCodexProvider(ModelProvider):
    """OMP Codex transport with explicit context and a traceable capability plane."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.auth = CodexAuth(config.codex_auth_path)
        self._doctor_result: ProviderDoctorResult | None = None

    def _environment(self, *, token: str, agent_dir: Path) -> dict[str, str]:
        capabilities = self.config.capabilities
        trusted = capabilities.mode == "trusted"
        if trusted and capabilities.inherit_environment:
            env = dict(os.environ)
        else:
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "PATH",
                    "LANG",
                    "LC_ALL",
                    "LC_CTYPE",
                    "SSL_CERT_FILE",
                    "SSL_CERT_DIR",
                    "NODE_EXTRA_CA_CERTS",
                    "TZ",
                }
                or key.startswith("FAKE_OMP_")
            }
        env.update(
            {
                "OPENAI_CODEX_OAUTH_TOKEN": token,
                "NULL_PROMPT": "true",
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "NO_COLOR": "1",
            }
        )
        if not trusted:
            env.update(
                {
                    "HOME": str(agent_dir),
                    "XDG_CACHE_HOME": str(agent_dir / "cache"),
                    "XDG_CONFIG_HOME": str(agent_dir / "config"),
                    "XDG_DATA_HOME": str(agent_dir / "data"),
                }
            )
        return env

    async def _run_short(self, args: list[str], *, token: str) -> tuple[int, str]:
        agent_dir = self.config.provider_state_root.expanduser() / "doctor"
        agent_dir.mkdir(parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                self.config.command,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._environment(token=token, agent_dir=agent_dir),
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"OMP executable not found: {self.config.command!r}") from exc
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProviderError("OMP compatibility probe timed out") from exc
        return process.returncode or 0, stdout.decode(errors="replace").strip()

    async def doctor(self) -> ProviderDoctorResult:
        if self._doctor_result is not None:
            return self._doctor_result
        if shutil.which(self.config.command) is None:
            self._doctor_result = self._doctor_failure(
                "OMP is not installed. Install it, then rerun flourite doctor."
            )
            return self._doctor_result
        try:
            probe = await self._doctor_probe()
        except (ProviderError, json.JSONDecodeError) as exc:
            self._doctor_result = self._doctor_failure(str(exc))
            return self._doctor_result
        self._doctor_result = ProviderDoctorResult(
            ok=probe.ok,
            provider="omp-codex",
            version=probe.version,
            auth_mode="chatgpt",
            details=probe.details(),
        )
        return self._doctor_result

    @staticmethod
    def _doctor_failure(message: str) -> ProviderDoctorResult:
        return ProviderDoctorResult(
            ok=False,
            provider="omp-codex",
            auth_mode="chatgpt",
            details=[message],
        )

    async def _doctor_probe(self) -> DoctorProbe:
        token = self.auth.access_token()
        version_code, version = await self._run_short(["--version"], token=token)
        models_code, raw_models = await self._run_short(
            ["models", "openai-codex", "--json", "--no-extensions"],
            token=token,
        )
        models = json.loads(raw_models).get("models", []) if models_code == 0 else []
        available = frozenset(
            item["id"]
            for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
        configured = {
            (route.model or self.config.default_model).removeprefix("openai-codex/")
            for route in (self.config.strong, self.config.worker, self.config.cheap)
        }
        contained = self.config.capabilities.mode == "contained"
        return DoctorProbe(
            version=version if isinstance(version, str) else None,
            version_code=version_code,
            models_code=models_code,
            available_models=available,
            missing_models=tuple(sorted(configured - available)),
            contained=contained,
            sandbox_ready=not contained or shutil.which("bwrap") is not None,
        )

    def _tool_names(self, request: ProviderCallRequest[Any]) -> list[str]:
        if self.config.capabilities.mode == "trusted":
            return list(self.config.capabilities.tools)
        tools = ["read", "grep", "glob"]
        if request.sandbox == SandboxPolicy.WORKSPACE_WRITE:
            tools.extend(["edit", "write"])
        if request.network_access:
            # Public research access stays explicit and narrow. Network
            # permission never implies a shell or interactive browser.
            tools.append("web_search")
        return tools

    def _runtime_overlay_for_tools(
        self,
        tools: set[str],
        *,
        network_effective: bool,
    ) -> dict[str, Any]:
        capabilities = self.config.capabilities
        flat = {
            "tools.approvalMode": "yolo",
            "tools.intentTracing": True,
            "tools.abortOnFabricatedResult": True,
            "tools.xdev": True,
            "tools.xdevDocs": "builtins",
            "retry.enabled": True,
            "retry.maxRetries": capabilities.retry_max_retries,
            "retry.modelFallback": False,
            "compaction.enabled": True,
            "compaction.midTurnEnabled": True,
            "compaction.autoContinue": True,
            "compaction.supersedeReads": True,
            "compaction.dropUseless": True,
            "lsp.enabled": "lsp" in tools,
            "bash.enabled": "bash" in tools,
            "fetch.enabled": network_effective,
            "web_search.enabled": network_effective and "web_search" in tools,
            "browser.enabled": network_effective and "browser" in tools,
            "github.enabled": "github" in tools,
            "checkpoint.enabled": "checkpoint" in tools or "rewind" in tools,
            "inspect_image.mode": "on" if "inspect_image" in tools else "off",
            "computer.enabled": "computer" in tools,
            "async.enabled": True,
            "task.batch": True,
            "task.enableEffort": True,
            "task.enableLsp": "lsp" in tools,
            "task.maxConcurrency": capabilities.task_max_concurrency,
            "task.maxRecursionDepth": capabilities.task_max_recursion_depth,
            "task.softRequestBudget": capabilities.task_soft_request_budget,
            "task.maxRuntimeMs": capabilities.task_max_runtime_ms,
            "task.isolation.mode": "none",
            "task.isolation.apply": True,
        }
        nested: dict[str, Any] = {}
        for path, value in flat.items():
            cursor = nested
            segments = path.split(".")
            for segment in segments[:-1]:
                child = cursor.get(segment)
                if not isinstance(child, dict):
                    child = {}
                    cursor[segment] = child
                cursor = child
            cursor[segments[-1]] = value
        return nested

    def _runtime_overlay(self, request: ProviderCallRequest[Any]) -> dict[str, Any]:
        return self._runtime_overlay_for_tools(
            set(self._tool_names(request)),
            network_effective=(
                self.config.capabilities.mode == "trusted" or request.network_access
            ),
        )

    def _omp_args(
        self,
        request: ProviderCallRequest[Any],
        *,
        effective_prompt: str,
        session_dir: Path,
        omp_cwd: Path,
        overlay_path: Path,
    ) -> list[str]:
        route = self.config.route(request.role)
        model = route.model or self.config.default_model
        if "/" not in model:
            model = f"openai-codex/{model}"
        args = [
            self.config.command,
            "--print",
            "--mode",
            "json",
            "--model",
            model,
            "--smol",
            model,
            "--slow",
            model,
            "--plan",
            model,
            "--thinking",
            route.reasoning_effort,
            "--cwd",
            str(omp_cwd),
            "--tools",
            ",".join(self._tool_names(request)),
            "--config",
            str(overlay_path),
            "--no-title",
            "--no-prewalk",
            "--auto-approve",
            "--approval-mode",
            "yolo",
            "--max-time",
            f"{self.config.timeout_seconds}s",
        ]
        capabilities = self.config.capabilities
        if capabilities.mode == "contained":
            args.extend(["--no-lsp", "--no-pty"])
        if not capabilities.discover_extensions:
            args.append("--no-extensions")
        if not capabilities.discover_skills:
            args.append("--no-skills")
        if not capabilities.discover_rules:
            args.append("--no-rules")
        if omp_cwd != request.cwd.resolve():
            args.extend(["--add-dir", str(request.cwd.resolve())])
        if request.resume_thread_id:
            args.extend(["--resume", request.resume_thread_id, "--session-dir", str(session_dir)])
        elif request.preserve_session:
            args.extend(["--session-dir", str(session_dir)])
        else:
            args.append("--no-session")
        for image in request.image_paths:
            args.append(f"@{image}")
        args.append(effective_prompt)
        return args

    @staticmethod
    def _sandbox_prefix(
        request: ProviderCallRequest[Any], session_dir: Path, omp_cwd: Path
    ) -> list[str]:
        """Expose only runtime files, the call workspace, and provider session state."""

        workspace = request.cwd.resolve()
        session_dir = session_dir.resolve()
        prefix = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--dir",
            "/etc",
        ]
        for host_path in ("/etc/ssl", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
            if Path(host_path).exists():
                prefix.extend(["--ro-bind", host_path, host_path])
        made: set[str] = {"/"}
        for target in (workspace, session_dir, omp_cwd):
            for parent in reversed(list(target.parents)[:-1]):
                value = str(parent)
                if value not in made:
                    prefix.extend(["--dir", value])
                    made.add(value)
        prefix.extend(
            [
                "--bind",
                str(workspace),
                str(workspace),
                "--bind",
                str(session_dir),
                str(session_dir),
                "--chdir",
                str(omp_cwd),
            ]
        )
        return prefix

    def _command(
        self,
        request: ProviderCallRequest[Any],
        *,
        effective_prompt: str,
        session_dir: Path,
        omp_cwd: Path,
        overlay_path: Path,
    ) -> tuple[list[str], list[str]]:
        omp_args = self._omp_args(
            request,
            effective_prompt=effective_prompt,
            session_dir=session_dir,
            omp_cwd=omp_cwd,
            overlay_path=overlay_path,
        )
        safe = [*omp_args[:-1], "<explicit-prompt>"]
        if self.config.capabilities.mode != "contained":
            return omp_args, safe
        executable = shutil.which(self.config.command)
        if executable is None or shutil.which("bwrap") is None:
            raise ProviderError("OS isolation requires both OMP and bubblewrap")
        omp_args[0] = "/frontier-omp"
        prefix = self._sandbox_prefix(request, session_dir, omp_cwd)
        prefix.extend(["--ro-bind", executable, "/frontier-omp", "--"])
        return [*prefix, *omp_args], [*prefix, *safe]

    @staticmethod
    def _usage(message: dict[str, Any], *, wall_seconds: float) -> Usage:
        value = message.get("usage")
        raw = cast(dict[str, Any], value) if isinstance(value, dict) else {}
        return Usage(
            model_requests=1,
            input_tokens=int(raw.get("input", 0) or 0),
            cached_input_tokens=int(raw.get("cacheRead", 0) or 0),
            output_tokens=int(raw.get("output", 0) or 0),
            reasoning_output_tokens=int(raw.get("reasoningTokens", 0) or 0),
            wall_seconds=wall_seconds,
        )

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=5)
        except (ProcessLookupError, TimeoutError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            await process.wait()

    async def _stream_process(
        self,
        process: asyncio.subprocess.Process,
        activity_callback: Callable[[dict[str, Any]], None] | None,
    ) -> tuple[bytes, bytes]:
        """Drain a JSONL provider process while forwarding sanitized live events.

        ``StreamReader.readline`` inherits asyncio's small separator limit and
        rejects otherwise-valid tool events once a single JSON record grows
        beyond it. OMP may legitimately emit such records. Frame stdout from
        fixed-size chunks instead, while retaining a deliberate upper bound on
        one unterminated event.
        """

        async def collect(
            stream: asyncio.StreamReader | None,
            *,
            provider_events: bool,
        ) -> bytes:
            if stream is None:
                return b""
            chunks: list[bytes] = []
            pending = bytearray()

            def forward(raw_event: bytes) -> None:
                if not raw_event or activity_callback is None:
                    return
                try:
                    event = json.loads(raw_event)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return
                safe_event = _safe_event(event)
                if safe_event is None:
                    return
                try:
                    activity_callback(safe_event)
                except Exception:
                    # Observability must never be able to fail a model call.
                    return

            while True:
                chunk = await stream.read(_PROVIDER_READ_CHUNK_BYTES)
                if not chunk:
                    break
                chunks.append(chunk)
                if not provider_events or activity_callback is None:
                    continue
                pending.extend(chunk)
                while True:
                    separator = pending.find(b"\n")
                    if separator < 0:
                        if len(pending) > _PROVIDER_MAX_EVENT_BYTES:
                            raise ProviderError(
                                "OMP emitted a JSONL event larger than the 32 MiB safety limit"
                            )
                        break
                    if separator > _PROVIDER_MAX_EVENT_BYTES:
                        raise ProviderError(
                            "OMP emitted a JSONL event larger than the 32 MiB safety limit"
                        )
                    forward(bytes(pending[:separator]))
                    del pending[: separator + 1]
            if provider_events and activity_callback is not None:
                forward(bytes(pending))
            return b"".join(chunks)

        _, stdout, stderr = await asyncio.gather(
            process.wait(),
            collect(process.stdout, provider_events=True),
            collect(process.stderr, provider_events=False),
        )
        return stdout, stderr

    async def run(self, request: ProviderCallRequest[ResponseT]) -> ProviderCallResult[ResponseT]:
        call = await self._prepare_call(request)
        state = OmpCallState(thread_id=request.resume_thread_id)
        for number in range(1, call.attempt_limit + 1):
            attempt = await self._execute_attempt(call, state, number)
            state.absorb(attempt)
            decision = self._decide_attempt(call, attempt)
            state.records.append(decision.record)
            state.validation_error = decision.error
            state.continuation_note = decision.continuation_note
            self._write_call_diagnostics(
                call,
                state,
                status=decision.diagnostic_status,
                error=decision.error,
            )
            if decision.response is not None:
                return cast(
                    ProviderCallResult[ResponseT],
                    self._successful_call(call, state, attempt, decision.response),
                )
            if decision.retry:
                continue
            self._raise_attempt_failure(call, state, attempt)
        self._raise_schema_failure(call, state)

    async def _prepare_call(
        self,
        request: ProviderCallRequest[Any],
    ) -> PreparedOmpCall:
        doctor = await self.doctor()
        if not doctor.ok:
            raise ProviderError("; ".join(doctor.details))
        request.cwd.mkdir(parents=True, exist_ok=True)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        schema = request.response_model.model_json_schema()
        atomic_write_text(request.schema_path, canonical_json(schema))

        session_dir = (
            Path(
                request.metadata.get(
                    "provider_session_dir", request.output_path.parent / "sessions"
                )
            )
            .expanduser()
            .resolve()
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        context_inventory = _context_inventory(request.cwd.resolve())
        context_state_path = session_dir / "lead-context-state.json"
        context_delta = _context_delta(
            context_inventory,
            self._prior_context(request, context_state_path),
        )
        effective_prompt = self._effective_prompt(
            request,
            schema=schema,
            context_delta=context_delta,
        )
        # A persistent conversation does not imply a persistent filesystem.
        # Every move's adapter workspace is the sole writable artifact state.
        # Keeping a second "lead cwd" made successful work invisible to the
        # adapter and could discard an entire long-running call at capture.
        omp_cwd = request.cwd.resolve()
        omp_cwd.mkdir(parents=True, exist_ok=True)
        agent_dir = session_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)

        overlay_path = request.output_path.parent / "omp-runtime.json"
        runtime_overlay = self._runtime_overlay(request)
        atomic_write_text(
            overlay_path,
            json.dumps(runtime_overlay, indent=2, sort_keys=True),
        )
        manifest_path = request.output_path.parent / "context-manifest.json"
        manifest = self._call_manifest(
            request,
            doctor_version=doctor.version,
            context_inventory=context_inventory,
            context_delta=context_delta,
            effective_prompt=effective_prompt,
            omp_cwd=omp_cwd,
            runtime_overlay=runtime_overlay,
        )
        atomic_write_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))
        return PreparedOmpCall(
            request=request,
            effective_prompt=effective_prompt,
            session_dir=session_dir,
            omp_cwd=omp_cwd,
            agent_dir=agent_dir,
            overlay_path=overlay_path,
            raw_events_path=request.output_path.parent / "provider-events.jsonl",
            stderr_path=request.output_path.parent / "provider-stderr.log",
            manifest_path=manifest_path,
            context_state_path=context_state_path,
            context_inventory=context_inventory,
            manifest=manifest,
            started=time.monotonic(),
            attempt_limit=min(self.config.schema_attempts, request.max_provider_calls),
            session_resumable=bool(request.preserve_session or request.resume_thread_id),
        )

    @staticmethod
    def _prior_context(
        request: ProviderCallRequest[Any],
        context_state_path: Path,
    ) -> dict[str, dict[str, int | str]]:
        if not request.resume_thread_id or not context_state_path.exists():
            return {}
        try:
            value = json.loads(context_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(path): cast(dict[str, int | str], item)
            for path, item in value.items()
            if isinstance(item, dict)
        }

    @staticmethod
    def _effective_prompt(
        request: ProviderCallRequest[Any],
        *,
        schema: dict[str, Any],
        context_delta: dict[str, list[str]],
    ) -> str:
        delta_note = OmpCodexProvider._context_delta_note(request, context_delta)
        workspace_prefix = (
            f"<frontier_workspace>{request.cwd.resolve()}</frontier_workspace>\n"
            "Treat that explicit directory as the sole task workspace for this call. "
            "Use the absolute paths in the prompt; do not infer files from the provider "
            "session directory.\n\n" + delta_note
        )
        boundary = (
            "\n\n<frontier_boundary>\nReturn exactly one JSON object and no Markdown. "
            "It must validate against this JSON Schema:\n"
            f"{canonical_json(schema)}\n</frontier_boundary>"
        )
        return workspace_prefix + request.prompt + boundary

    @staticmethod
    def _context_delta_note(
        request: ProviderCallRequest[Any],
        context_delta: dict[str, list[str]],
    ) -> str:
        if not request.resume_thread_id:
            return ""
        changed = [*context_delta["added"], *context_delta["changed"]]
        return (
            "<frontier_context_delta>\n"
            "Changed or added since the last successful Lead epoch: "
            f"{', '.join(changed) if changed else '(none)'}.\n"
            f"Removed: {', '.join(context_delta['removed']) if context_delta['removed'] else '(none)'}.\n"
            f"Unchanged explicit context files: {len(context_delta['unchanged'])}. "
            "Their hashes match the prior epoch; do not reread them reflexively. "
            "Use session memory, but reread any unchanged file when its exact content "
            "is load-bearing.\n"
            "</frontier_context_delta>\n\n"
        )

    def _call_manifest(
        self,
        request: ProviderCallRequest[Any],
        *,
        doctor_version: str | None,
        context_inventory: dict[str, dict[str, int | str]],
        context_delta: dict[str, list[str]],
        effective_prompt: str,
        omp_cwd: Path,
        runtime_overlay: dict[str, Any],
    ) -> dict[str, Any]:
        route = self.config.route(request.role)
        tools = self._tool_names(request)
        return {
            "version": 2,
            "provider": "openai-codex",
            "transport": "omp-openai-codex-responses",
            "transport_version": doctor_version,
            "system_messages": [],
            "developer_messages": [],
            "tools": tools,
            "context_files": context_inventory,
            "context_delta": context_delta,
            "capabilities": {
                "mode": self.config.capabilities.mode,
                "inherit_environment": (
                    self.config.capabilities.mode == "trusted"
                    and self.config.capabilities.inherit_environment
                ),
                "auto_approve": True,
                "network_effective": (
                    self.config.capabilities.mode == "trusted" or request.network_access
                ),
                "runtime_overlay": runtime_overlay,
                "contract_sha256": sha256_text(
                    canonical_json(
                        {
                            "tools": tools,
                            "runtime_overlay": runtime_overlay,
                            "transport_version": doctor_version,
                        }
                    )
                ),
            },
            "model": route.model or self.config.default_model,
            "reasoning_effort": route.reasoning_effort,
            "original_prompt": {
                "sha256": sha256_text(request.prompt),
                "bytes": len(request.prompt.encode()),
            },
            "effective_prompt": {
                "sha256": sha256_text(effective_prompt),
                "bytes": len(effective_prompt.encode()),
            },
            "schema_sha256": sha256_text(
                canonical_json(request.response_model.model_json_schema())
            ),
            "images": [
                {
                    "name": path.name,
                    "sha256": sha256_bytes(path.read_bytes()),
                    "bytes": path.stat().st_size,
                }
                for path in request.image_paths
            ],
            "ambient_discovery": {
                "system_prompt": False,
                "rules": self.config.capabilities.discover_rules,
                "skills": self.config.capabilities.discover_skills,
                "extensions": self.config.capabilities.discover_extensions,
                "lsp": "lsp" in tools,
            },
            "sandbox": request.sandbox.value,
            "network_requested": request.network_access,
            "continuation": request.resume_thread_id,
            "omp_cwd": str(omp_cwd),
            "call_workspace": str(request.cwd.resolve()),
            "max_provider_calls": request.max_provider_calls,
            "outcome": {"status": "running", "attempts": []},
        }

    async def _execute_attempt(
        self,
        call: PreparedOmpCall,
        state: OmpCallState,
        number: int,
    ) -> OmpAttempt:
        prompt = self._attempt_prompt(call.effective_prompt, state)
        request = self._attempt_request(
            call.request,
            state,
            session_resumable=call.session_resumable,
        )
        command, safe_command = self._command(
            request,
            effective_prompt=prompt,
            session_dir=call.session_dir,
            omp_cwd=call.omp_cwd,
            overlay_path=call.overlay_path,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment(
                token=self.auth.access_token(),
                agent_dir=call.agent_dir,
            ),
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                self._stream_process(process, call.request.activity_callback),
                timeout=self.config.timeout_seconds + 30,
            )
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        except TimeoutError:
            await self._terminate(process)
            return OmpAttempt(
                number=number,
                return_code=process.returncode,
                safe_command=safe_command,
                thread_id=state.thread_id,
                timed_out=True,
            )
        return self._decode_attempt(
            number,
            return_code=process.returncode,
            safe_command=safe_command,
            stdout=stdout,
            stderr=stderr,
            prior_thread_id=state.thread_id,
        )

    @staticmethod
    def _attempt_prompt(base: str, state: OmpCallState) -> str:
        prompt = base
        if state.continuation_note:
            prompt += f"\n\n{state.continuation_note}"
        if state.validation_error:
            prompt += (
                "\n\nYour previous boundary object was invalid. Return a corrected "
                "JSON object only. Validation error: "
                f"{state.validation_error[:2000]}"
            )
        return prompt

    @staticmethod
    def _attempt_request(
        request: ProviderCallRequest[Any],
        state: OmpCallState,
        *,
        session_resumable: bool,
    ) -> ProviderCallRequest[Any]:
        if session_resumable and state.thread_id and state.thread_id != request.resume_thread_id:
            state.resumed_within_call = True
            return request.model_copy(
                update={"resume_thread_id": state.thread_id, "preserve_session": True}
            )
        return request

    def _decode_attempt(
        self,
        number: int,
        *,
        return_code: int | None,
        safe_command: list[str],
        stdout: bytes,
        stderr: bytes,
        prior_thread_id: str | None,
    ) -> OmpAttempt:
        events: list[dict[str, Any]] = []
        final_message: dict[str, Any] | None = None
        usage = Usage()
        thread_id = prior_thread_id
        for raw_line in stdout.splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "session" and isinstance(event.get("id"), str):
                thread_id = event["id"]
            safe_event = _safe_event(event)
            if safe_event is None:
                continue
            events.append(safe_event)
            if safe_event.get("type") == "message_end":
                message = cast(dict[str, Any], safe_event["message"])
                usage = usage.plus(self._usage(message, wall_seconds=0.0))
                if self._has_text(message):
                    final_message = message
        trace = _trace_summary(events)
        return OmpAttempt(
            number=number,
            return_code=return_code,
            safe_command=safe_command,
            stderr=stderr.decode(errors="replace"),
            events=events,
            final_message=final_message,
            usage=usage.plus(trace.nested_usage),
            thread_id=thread_id,
        )

    @staticmethod
    def _has_text(message: dict[str, Any]) -> bool:
        return any(
            isinstance(block, dict) and block.get("type") == "text"
            for block in message.get("content", [])
        )

    def _decide_attempt(
        self,
        call: PreparedOmpCall,
        attempt: OmpAttempt,
    ) -> AttemptDecision:
        trace = attempt.trace
        base = self._attempt_record(attempt)
        if attempt.timed_out:
            return AttemptDecision(
                status="timeout",
                diagnostic_status="error",
                error="provider timeout",
                record={**base, "status": "timeout"},
            )
        if (
            attempt.return_code == 0
            and attempt.final_message is None
            and call.session_resumable
            and attempt.thread_id
            and attempt.number < call.attempt_limit
        ):
            return AttemptDecision(
                status="execution_slice_exhausted",
                diagnostic_status="continuing",
                retry=True,
                continuation_note=self._slice_continuation(),
                record={
                    **base,
                    "status": "execution_slice_exhausted",
                    "thread_id": attempt.thread_id,
                },
            )
        if attempt.return_code != 0 or attempt.final_message is None:
            return AttemptDecision(
                status="provider_error",
                diagnostic_status="error",
                error="provider process failed",
                record={**base, "status": "provider_error"},
            )

        raw_final = attempt.raw_final
        self._write_attempt_output(call.request, attempt.number, raw_final)
        stop_reason = (trace.stop_reason or "").casefold()
        if stop_reason in {"length", "max_tokens", "token_limit", "output_limit"}:
            error = (
                f"provider stopped at its output limit ({trace.stop_reason}); "
                "the boundary is not accepted even if defaults make it schema-valid"
            )
            return AttemptDecision(
                status="output_limit",
                diagnostic_status="retrying",
                retry=True,
                error=error,
                continuation_note=self._output_continuation(),
                record={
                    **base,
                    "status": "output_limit",
                    "output_sha256": sha256_text(raw_final),
                    "stop_reason": trace.stop_reason,
                },
            )
        try:
            response = call.request.response_model.model_validate_json(raw_final)
        except ValidationError as exc:
            error = str(exc)
            return AttemptDecision(
                status="schema_invalid",
                diagnostic_status="retrying",
                retry=True,
                error=error,
                record={
                    **base,
                    "status": "schema_invalid",
                    "output_sha256": sha256_text(raw_final),
                    "validation_error": error[:2000],
                },
            )
        return AttemptDecision(
            status="valid",
            diagnostic_status="success",
            response=response,
            record={
                **base,
                "status": "valid",
                "output_sha256": sha256_text(raw_final),
            },
        )

    @staticmethod
    def _attempt_record(attempt: OmpAttempt) -> dict[str, Any]:
        trace = attempt.trace
        return {
            "attempt": attempt.number,
            "return_code": attempt.return_code,
            "model_turns": trace.model_turns,
            "tool_calls": len(trace.tool_calls),
            "tool_errors": trace.tool_errors,
        }

    @staticmethod
    def _slice_continuation() -> str:
        return (
            "The previous execution slice ended before you returned the required "
            "boundary object. Continue the same session and preserve all work already "
            "present in the explicit workspace. Do not restart research or open new "
            "delegations. Close the current small unit, verify the workspace, write the "
            "required artifact summary, and return the required JSON boundary now. "
            "Unfinished improvements belong in actions for later harness rounds."
        )

    @staticmethod
    def _output_continuation() -> str:
        return (
            "The previous response hit its output limit and is not an accepted boundary. "
            "Preserve the completed workspace work, compress commentary, and return the "
            "complete required JSON object now. Do not restart the task."
        )

    @staticmethod
    def _write_attempt_output(
        request: ProviderCallRequest[Any],
        number: int,
        raw_final: str,
    ) -> None:
        atomic_write_text(request.output_path, raw_final)
        atomic_write_text(
            request.output_path.with_name(
                f"{request.output_path.stem}.attempt-{number}{request.output_path.suffix}"
            ),
            raw_final,
        )

    @staticmethod
    def _write_call_diagnostics(
        call: PreparedOmpCall,
        state: OmpCallState,
        *,
        status: str,
        error: str | None,
    ) -> None:
        call.manifest["outcome"] = {
            "status": status,
            "attempts": state.records,
            "usage": state.usage.model_dump(mode="json"),
            "wall_seconds": call.duration(),
            "error": error,
        }
        atomic_write_text(
            call.manifest_path,
            json.dumps(call.manifest, indent=2, sort_keys=True),
        )
        atomic_write_text(
            call.raw_events_path,
            "".join(canonical_json(event) + "\n" for event in state.events),
        )
        atomic_write_text(call.stderr_path, "\n".join(state.stderr))

    def _successful_call(
        self,
        call: PreparedOmpCall,
        state: OmpCallState,
        attempt: OmpAttempt,
        response: BaseModel,
    ) -> ProviderCallResult[Any]:
        duration = call.duration()
        state.usage.wall_seconds = duration
        self._write_call_diagnostics(call, state, status="success", error=None)
        if call.session_resumable:
            atomic_write_text(
                call.context_state_path,
                json.dumps(call.context_inventory, indent=2, sort_keys=True),
            )
        return ProviderCallResult(
            call_id=call.request.call_id,
            response=response,
            usage=state.usage,
            duration_seconds=duration,
            return_code=0,
            boundary_attempts=len(state.records),
            thread_id=state.thread_id,
            resumed=bool(call.request.resume_thread_id or state.resumed_within_call),
            raw_events_path=call.raw_events_path,
            stderr_path=call.stderr_path,
            command=attempt.safe_command,
            trace_summary=state.trace(),
        )

    def _raise_attempt_failure(
        self,
        call: PreparedOmpCall,
        state: OmpCallState,
        attempt: OmpAttempt,
    ) -> NoReturn:
        duration = call.duration()
        state.usage.wall_seconds = duration
        if attempt.timed_out:
            message = f"OMP Codex call {call.request.call_id} timed out after {duration:.1f}s"
        else:
            tail = attempt.stderr[-3000:].strip()
            failure = (
                f"exited with {attempt.return_code}"
                if attempt.return_code != 0
                else "ended without a boundary response"
            )
            message = f"OMP Codex call {call.request.call_id} {failure}"
            if tail:
                message += f": {tail}"
        raise ProviderCallError(
            message,
            usage=state.usage,
            raw_events_path=call.raw_events_path,
            stderr_path=call.stderr_path,
            command=attempt.safe_command,
            trace_summary=state.trace().model_dump(mode="json"),
            boundary_attempts=len(state.records),
            thread_id=state.thread_id,
        )

    def _raise_schema_failure(
        self,
        call: PreparedOmpCall,
        state: OmpCallState,
    ) -> NoReturn:
        state.usage.wall_seconds = call.duration()
        self._write_call_diagnostics(
            call,
            state,
            status="error",
            error=state.validation_error,
        )
        raise ProviderCallError(
            "OMP Codex output failed the boundary schema after "
            f"{call.attempt_limit} attempt(s): {state.validation_error}",
            usage=state.usage,
            raw_events_path=call.raw_events_path,
            stderr_path=call.stderr_path,
            command=state.last_safe_command,
            trace_summary=state.trace().model_dump(mode="json"),
            boundary_attempts=len(state.records),
            thread_id=state.thread_id,
        )
