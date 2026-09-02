from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from frontier_harness.config import DEFAULT_TRUSTED_TOOLS, ProviderConfig
from frontier_harness.models import Role, SandboxPolicy
from frontier_harness.providers.base import ProviderCallRequest
from frontier_harness.providers.omp_codex import (
    OmpCodexProvider,
    _safe_event,
    _trace_summary,
)


class ProbeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


def _token() -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{payload}.signature"


def _auth(path: Path, *, mode: str = "chatgpt") -> Path:
    path.write_text(
        json.dumps(
            {
                "auth_mode": mode,
                "tokens": {
                    "access_token": _token(),
                    "refresh_token": "refresh-test",
                    "id_token": "id-test",
                    "account_id": "account-test",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _fake_omp(path: Path) -> Path:
    executable = path / "omp"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("omp/test")
    raise SystemExit(0)
if args[:2] == ["models", "openai-codex"]:
    print(json.dumps({"models": [{"id": "gpt-5.6-terra"}, {"id": "gpt-5.6-sol"}]}))
    raise SystemExit(0)
if "__frontier_tool_probe__" in " ".join(args):
    configured = args[args.index("--tools") + 1].split(",")
    valid = [item for item in configured if item != "__frontier_tool_probe__"]
    print("CliUsageError: Unknown tool in --tools: __frontier_tool_probe__. Valid tools: " + ", ".join(valid) + ".")
    raise SystemExit(1)

log = os.environ.get("FAKE_OMP_LOG")
if log:
    overlay = {}
    if "--config" in args:
        overlay = json.loads(Path(args[args.index("--config") + 1]).read_text())
    Path(log).write_text(json.dumps({
        "args": args,
        "overlay": overlay,
        "null_prompt": os.environ.get("NULL_PROMPT"),
        "has_oauth": bool(os.environ.get("OPENAI_CODEX_OAUTH_TOKEN")),
        "has_api_key": bool(os.environ.get("OPENAI_API_KEY")),
        "has_openrouter_key": bool(os.environ.get("OPENROUTER_API_KEY")),
    }), encoding="utf-8")

counter_path = os.environ.get("FAKE_OMP_RETRY_COUNTER")
attempt = 1
if counter_path:
    counter = Path(counter_path)
    attempt = int(counter.read_text() or "0") + 1 if counter.exists() else 1
    counter.write_text(str(attempt), encoding="utf-8")
length_counter_path = os.environ.get("FAKE_OMP_LENGTH_COUNTER")
length_attempt = 0
if length_counter_path:
    length_counter = Path(length_counter_path)
    length_attempt = int(length_counter.read_text() or "0") + 1 if length_counter.exists() else 1
    length_counter.write_text(str(length_attempt), encoding="utf-8")
secret_path = os.environ.get("FAKE_OMP_SECRET")
secret_visible = bool(secret_path and Path(secret_path).exists())
payload = {"wrong": True} if counter_path and attempt == 1 else {"ok": not secret_visible}
print(json.dumps({"type": "session", "version": 3, "id": "session-test", "cwd": os.getcwd()}))
slice_counter_path = os.environ.get("FAKE_OMP_SLICE_COUNTER")
if slice_counter_path:
    slice_counter = Path(slice_counter_path)
    slice_attempt = int(slice_counter.read_text() or "0") + 1 if slice_counter.exists() else 1
    slice_counter.write_text(str(slice_attempt), encoding="utf-8")
    if slice_attempt == 1:
        print(json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "tool-slice", "name": "write", "arguments": {"path": "draft"}}],
                "usage": {"input": 13, "cacheRead": 5, "output": 3, "reasoningTokens": 1},
                "stopReason": "toolUse",
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
            },
        }))
        raise SystemExit(0)
if os.environ.get("FAKE_OMP_TOOL_TRACE"):
    print(json.dumps({
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "tool-1", "name": "bash", "arguments": {"command": "true"}, "intent": "potency"}],
            "usage": {"input": 7, "cacheRead": 1, "output": 2, "reasoningTokens": 1},
            "stopReason": "toolUse",
            "model": "gpt-5.6-sol",
            "provider": "openai-codex",
        },
    }))
    print(json.dumps({"type": "tool_execution_start", "toolCallId": "tool-1", "toolName": "bash", "args": {"command": "true"}, "intent": "potency"}))
    print(json.dumps({"type": "tool_execution_end", "toolCallId": "tool-1", "toolName": "bash", "result": {"content": [{"type": "text", "text": "ok"}], "details": {"wallTimeMs": 4}}, "isError": False}))
    if os.environ.get("FAKE_OMP_FAIL_AFTER_TRACE"):
        print("synthetic provider failure", file=sys.stderr)
        raise SystemExit(9)
large_event_bytes = int(os.environ.get("FAKE_OMP_LARGE_EVENT_BYTES", "0"))
if large_event_bytes:
    print(json.dumps({"type": "diagnostic", "padding": "x" * large_event_bytes}))
print(json.dumps({
    "type": "message_end",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "brief", "thinkingSignature": "secret-replay"},
            {"type": "text", "text": json.dumps(payload)},
        ],
        "usage": {"input": 11, "cacheRead": 3, "output": 5, "reasoningTokens": 2},
        "stopReason": "length" if length_attempt == 1 else "stop",
        "model": "gpt-5.6-terra",
        "provider": "openai-codex",
    },
}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _request(tmp_path: Path) -> ProviderCallRequest[ProbeResponse]:
    cwd = tmp_path / "workspace"
    output = cwd / "output"
    output.mkdir(parents=True)
    return ProviderCallRequest[ProbeResponse](
        call_id="call_test",
        call_kind="probe",
        role=Role.WORKER,
        prompt="Return the boundary object.",
        cwd=cwd,
        response_model=ProbeResponse,
        output_path=output / "boundary.json",
        schema_path=output / "boundary.schema.json",
        sandbox=SandboxPolicy.READ_ONLY,
        metadata={"provider_session_dir": str(tmp_path / "sessions")},
    )


def _provider(tmp_path: Path, *, attempts: int = 2, mode: str = "trusted") -> OmpCodexProvider:
    return OmpCodexProvider(
        ProviderConfig(
            command=str(_fake_omp(tmp_path)),
            codex_auth_path=_auth(tmp_path / "auth.json"),
            provider_state_root=tmp_path / "provider-state",
            capabilities={"mode": mode},
            schema_attempts=attempts,
            timeout_seconds=30,
        )
    )


def test_omp_provider_has_explicit_context_and_sanitized_trace(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_OMP_LOG", str(log))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-provider")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-provider")
    provider = _provider(tmp_path)
    doctor = asyncio.run(provider.doctor())
    assert doctor.ok
    assert doctor.provider == "omp-codex"

    request = _request(tmp_path)
    live_events: list[dict[str, object]] = []
    request = request.model_copy(update={"activity_callback": live_events.append})
    result = asyncio.run(provider.run(request))
    assert result.response.ok is True
    assert result.thread_id == "session-test"
    assert [item["type"] for item in live_events] == ["session", "message_end"]
    assert result.usage.model_dump() == {
        "calls": 1,
        "model_requests": 1,
        "input_tokens": 11,
        "cached_input_tokens": 3,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
        "wall_seconds": result.usage.wall_seconds,
    }
    invocation = json.loads(log.read_text())
    assert invocation["null_prompt"] == "true"
    assert invocation["has_oauth"] is True
    assert invocation["has_api_key"] is True
    assert invocation["has_openrouter_key"] is True
    assert "--no-rules" in invocation["args"]
    assert "--no-skills" in invocation["args"]
    assert "--no-extensions" in invocation["args"]
    assert "--system-prompt" not in invocation["args"]
    assert "--auto-approve" in invocation["args"]
    assert "--no-lsp" not in invocation["args"]
    assert "--no-pty" not in invocation["args"]
    assert invocation["overlay"]["tools"]["approvalMode"] == "yolo"
    assert invocation["overlay"]["compaction"]["midTurnEnabled"] is True
    assert invocation["overlay"]["task"]["maxConcurrency"] == 4
    assert invocation["overlay"]["async"]["enabled"] is True
    assert result.command[-1] == "<explicit-prompt>"

    manifest = json.loads((request.output_path.parent / "context-manifest.json").read_text())
    assert manifest["system_messages"] == []
    assert manifest["developer_messages"] == []
    assert manifest["tools"] == DEFAULT_TRUSTED_TOOLS
    assert manifest["capabilities"]["mode"] == "trusted"
    assert manifest["capabilities"]["inherit_environment"] is True
    assert manifest["transport_version"] == "omp/test"
    assert len(manifest["capabilities"]["contract_sha256"]) == 64
    assert manifest["outcome"]["status"] == "success"
    assert manifest["outcome"]["attempts"][0]["status"] == "valid"
    assert manifest["ambient_discovery"] == {
        "extensions": False,
        "lsp": True,
        "rules": False,
        "skills": False,
        "system_prompt": False,
    }
    trace = (request.output_path.parent / "provider-events.jsonl").read_text()
    assert "secret-replay" not in trace
    assert "thinkingSignature" not in trace


def test_schema_valid_output_limit_is_retried_not_accepted(tmp_path: Path, monkeypatch) -> None:
    counter = tmp_path / "length-counter"
    monkeypatch.setenv("FAKE_OMP_LENGTH_COUNTER", str(counter))
    provider = _provider(tmp_path, attempts=2)
    request = _request(tmp_path).model_copy(update={"max_provider_calls": 2})

    result = asyncio.run(provider.run(request))

    assert result.response.ok is True
    assert counter.read_text(encoding="utf-8") == "2"
    manifest = json.loads((request.output_path.parent / "context-manifest.json").read_text())
    assert [item["status"] for item in manifest["outcome"]["attempts"]] == [
        "output_limit",
        "valid",
    ]


def test_resumed_lead_receives_hashed_context_delta(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_OMP_LOG", str(log))
    provider = _provider(tmp_path)
    request = _request(tmp_path).model_copy(update={"preserve_session": True})
    context = request.cwd / ".sfh_context"
    context.mkdir()
    (context / "STATE.json").write_text('{"round": 0}\n', encoding="utf-8")
    (context / "TASK_SOURCE.json").write_text('{"task": "stable"}\n', encoding="utf-8")

    first = asyncio.run(provider.run(request))
    first_manifest = json.loads((request.output_path.parent / "context-manifest.json").read_text())
    assert first_manifest["omp_cwd"] == str(request.cwd.resolve())
    assert first_manifest["call_workspace"] == str(request.cwd.resolve())
    (context / "STATE.json").write_text('{"round": 1}\n', encoding="utf-8")
    (context / "EVIDENCE.md").write_text("new evidence\n", encoding="utf-8")
    second_output = request.output_path.parent / "boundary-second.json"
    second = request.model_copy(
        update={
            "call_id": "call_second",
            "output_path": second_output,
            "schema_path": second_output.with_suffix(".schema.json"),
            "resume_thread_id": first.thread_id,
        }
    )
    asyncio.run(provider.run(second))

    manifest = json.loads((second_output.parent / "context-manifest.json").read_text())
    assert manifest["context_delta"] == {
        "added": [".sfh_context/EVIDENCE.md"],
        "changed": [".sfh_context/STATE.json"],
        "removed": [],
        "unchanged": [".sfh_context/TASK_SOURCE.json"],
    }
    invocation = json.loads(log.read_text())
    prompt = invocation["args"][-1]
    assert "Changed or added since the last successful Lead epoch" in prompt
    assert ".sfh_context/STATE.json" in prompt
    assert "Unchanged explicit context files: 1" in prompt


def test_omp_provider_retries_an_invalid_boundary_and_accounts_for_both_calls(
    tmp_path: Path, monkeypatch
) -> None:
    counter = tmp_path / "attempts"
    log = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_OMP_RETRY_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_OMP_LOG", str(log))
    request = _request(tmp_path).model_copy(update={"max_provider_calls": 2})
    result = asyncio.run(_provider(tmp_path).run(request))
    assert result.response.ok is True
    assert counter.read_text() == "2"
    assert result.usage.calls == 2
    assert result.usage.model_requests == 2
    assert result.boundary_attempts == 2
    assert result.usage.input_tokens == 22
    assert result.raw_events_path is not None
    trace = result.raw_events_path.read_text()
    assert '"attempt":1' in trace
    assert '"attempt":2' in trace
    manifest = json.loads(result.raw_events_path.with_name("context-manifest.json").read_text())
    assert [item["status"] for item in manifest["outcome"]["attempts"]] == [
        "schema_invalid",
        "valid",
    ]
    assert result.raw_events_path.with_name("boundary.attempt-1.json").is_file()
    assert "--resume" not in json.loads(log.read_text())["args"]


def test_omp_provider_continues_a_completed_slice_without_a_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    counter = tmp_path / "slices"
    log = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_OMP_SLICE_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_OMP_LOG", str(log))
    request = _request(tmp_path).model_copy(
        update={"max_provider_calls": 2, "preserve_session": True}
    )

    result = asyncio.run(_provider(tmp_path).run(request))

    assert result.response.ok is True
    assert result.thread_id == "session-test"
    assert result.resumed is True
    assert result.usage.calls == 2
    assert result.usage.model_requests == 2
    assert counter.read_text() == "2"
    invocation = json.loads(log.read_text())
    assert invocation["args"][invocation["args"].index("--resume") + 1] == "session-test"
    assert "Do not restart research" in invocation["args"][-1]
    manifest = json.loads(result.raw_events_path.with_name("context-manifest.json").read_text())
    assert [item["status"] for item in manifest["outcome"]["attempts"]] == [
        "execution_slice_exhausted",
        "valid",
    ]


def test_omp_provider_counts_tool_loop_turns_and_preserves_trace(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_OMP_TOOL_TRACE", "1")
    result = asyncio.run(_provider(tmp_path).run(_request(tmp_path)))
    assert result.usage.calls == 1
    assert result.usage.model_requests == 2
    assert result.usage.input_tokens == 18
    assert result.usage.output_tokens == 7
    assert result.trace_summary.model_turns == 2
    assert result.trace_summary.tool_errors == 0
    assert len(result.trace_summary.tool_calls) == 1
    tool = result.trace_summary.tool_calls[0]
    assert (tool.name, tool.intent, tool.success, tool.duration_ms) == (
        "bash",
        "potency",
        True,
        4.0,
    )
    trace = result.raw_events_path.read_text() if result.raw_events_path else ""
    assert "tool_execution_start" in trace
    assert "tool_execution_end" in trace


def test_omp_provider_accepts_jsonl_events_larger_than_asyncio_line_limit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_OMP_LARGE_EVENT_BYTES", str(128 * 1024))
    request = _request(tmp_path)
    live_events: list[dict[str, object]] = []
    request = request.model_copy(update={"activity_callback": live_events.append})

    result = asyncio.run(_provider(tmp_path).run(request))

    assert result.response.ok is True
    assert [item["type"] for item in live_events] == ["session", "message_end"]


def test_omp_provider_failure_preserves_observed_tool_trace(tmp_path: Path, monkeypatch) -> None:
    from frontier_harness.errors import ProviderCallError

    monkeypatch.setenv("FAKE_OMP_TOOL_TRACE", "1")
    monkeypatch.setenv("FAKE_OMP_FAIL_AFTER_TRACE", "1")
    with pytest.raises(ProviderCallError) as caught:
        asyncio.run(_provider(tmp_path).run(_request(tmp_path)))
    assert caught.value.trace_summary["model_turns"] == 1
    assert caught.value.trace_summary["tool_calls"][0]["name"] == "bash"
    assert caught.value.trace_summary["tool_calls"][0]["success"] is True
    assert caught.value.boundary_attempts == 1
    assert caught.value.thread_id == "session-test"
    assert caught.value.failure_kind == "process"


def test_omp_provider_identifies_a_rejected_result_boundary(tmp_path: Path, monkeypatch) -> None:
    from frontier_harness.errors import ProviderCallError

    monkeypatch.setenv("FAKE_OMP_RETRY_COUNTER", str(tmp_path / "attempts"))
    with pytest.raises(ProviderCallError) as caught:
        asyncio.run(_provider(tmp_path, attempts=1).run(_request(tmp_path)))

    assert caught.value.failure_kind == "boundary"
    assert caught.value.boundary_attempts == 1


def test_contained_network_access_adds_only_bounded_web_search(tmp_path: Path) -> None:
    provider = _provider(tmp_path, mode="contained")
    request = _request(tmp_path).model_copy(update={"network_access": True})
    assert provider._tool_names(request) == ["read", "grep", "glob", "web_search"]


def test_contained_mode_strips_ambient_credentials(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "workspace" / "args.json"
    monkeypatch.setenv("FAKE_OMP_LOG", str(log))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-contained-provider")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-contained-provider")
    provider = _provider(tmp_path, mode="contained")
    asyncio.run(provider.run(_request(tmp_path)))
    invocation = json.loads(log.read_text())
    assert invocation["has_api_key"] is False
    assert invocation["has_openrouter_key"] is False
    assert "--no-lsp" in invocation["args"]
    assert "--no-pty" in invocation["args"]


def test_os_sandbox_hides_files_outside_the_call_workspace(tmp_path: Path, monkeypatch) -> None:
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("must remain invisible", encoding="utf-8")
    monkeypatch.setenv("FAKE_OMP_SECRET", str(secret))
    provider = OmpCodexProvider(
        ProviderConfig(
            command=str(_fake_omp(tmp_path)),
            codex_auth_path=_auth(tmp_path / "auth.json"),
            provider_state_root=tmp_path / "provider-state",
            capabilities={"mode": "contained"},
            timeout_seconds=30,
        )
    )
    result = asyncio.run(provider.run(_request(tmp_path)))
    assert result.response.ok is True


def test_omp_provider_doctor_rejects_non_subscription_auth(tmp_path: Path) -> None:
    provider = OmpCodexProvider(
        ProviderConfig(
            command=str(_fake_omp(tmp_path)),
            codex_auth_path=_auth(tmp_path / "auth.json", mode="apikey"),
            provider_state_root=tmp_path / "provider-state",
            capabilities={"mode": "trusted"},
        )
    )
    doctor = asyncio.run(provider.doctor())
    assert doctor.ok is False
    assert "not a ChatGPT subscription" in doctor.details[0]


def test_omp_provider_doctor_rejects_an_unavailable_configured_model(
    tmp_path: Path,
) -> None:
    provider = OmpCodexProvider(
        ProviderConfig(
            command=str(_fake_omp(tmp_path)),
            codex_auth_path=_auth(tmp_path / "auth.json"),
            provider_state_root=tmp_path / "provider-state",
            capabilities={"mode": "trusted"},
            strong={"model": "model-that-does-not-exist", "reasoning_effort": "high"},
        )
    )
    doctor = asyncio.run(provider.doctor())
    assert doctor.ok is False
    assert "model-that-does-not-exist" in doctor.details[-1]


def test_safe_event_drops_provider_replay_material() -> None:
    event = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "summary",
                    "thinkingSignature": "encrypted",
                }
            ],
            "providerPayload": {"secret": "opaque"},
        },
    }
    safe = _safe_event(event)
    assert safe is not None
    encoded = json.dumps(safe)
    assert "summary" in encoded
    assert "encrypted" not in encoded
    assert "opaque" not in encoded


def test_safe_event_hashes_tool_arguments_and_outputs_but_keeps_cost() -> None:
    secret = "super-secret-command"
    started = _safe_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "tool-1",
            "toolName": "bash",
            "args": {"command": secret},
            "intent": "run check",
        }
    )
    ended = _safe_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "tool-1",
            "toolName": "bash",
            "result": {
                "content": [{"type": "text", "text": secret}],
                "details": {
                    "wallTimeMs": 8,
                    "usage": {"input": 3, "output": 1},
                },
            },
            "isError": False,
        }
    )
    encoded = json.dumps([started, ended])
    assert secret not in encoded
    assert started is not None and started["arguments"]["keys"] == ["command"]
    assert ended is not None and ended["result"]["details"]["wallTimeMs"] == 8


def test_safe_event_exposes_task_names_without_task_prompts() -> None:
    secret = "private specialist assignment"
    safe = _safe_event(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "task-1",
                        "name": "task",
                        "arguments": {
                            "context": secret,
                            "tasks": [
                                {"name": "ArtDirection", "task": secret},
                                {"name": "TruthResearch", "task": secret},
                            ],
                        },
                    }
                ],
            },
        }
    )

    encoded = json.dumps(safe)
    assert secret not in encoded
    assert safe is not None
    arguments = safe["message"]["content"][0]["arguments"]
    assert arguments["task_names"] == ["ArtDirection", "TruthResearch"]


def test_safe_event_reduces_subagent_messages_to_status_only() -> None:
    secret = "completed the private artifact at a sensitive path"
    safe = _safe_event(
        {
            "type": "custom_message",
            "customType": "irc:incoming",
            "details": {"from": "ArtDirection", "message": secret},
        }
    )

    assert safe == {
        "type": "subagent_activity",
        "agent": "ArtDirection",
        "state": "done",
        "message": "completed assigned work",
    }
    assert secret not in json.dumps(safe)


def test_trace_summary_records_failed_tools() -> None:
    summary = _trace_summary(
        [
            {
                "type": "tool_execution_start",
                "toolCallId": "call-1",
                "toolName": "bash",
                "args": {"command": "false"},
                "intent": "negative control",
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "call-1",
                "toolName": "bash",
                "result": {"content": [], "details": {"wallTimeMs": 9}},
                "isError": True,
            },
        ]
    )
    assert summary.tool_errors == 1
    assert summary.tool_calls[0].success is False


def test_trace_summary_accounts_for_nested_agent_requests() -> None:
    summary = _trace_summary(
        [
            {
                "type": "message_end",
                "message": {"role": "assistant", "content": [], "usage": {}},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "task-1",
                "toolName": "task",
                "result": {
                    "content": [],
                    "details": {
                        "totalDurationMs": 125,
                        "results": [{"requests": 2}],
                        "usage": {
                            "input": 80,
                            "cacheRead": 20,
                            "output": 9,
                            "reasoningTokens": 4,
                        },
                    },
                },
                "isError": False,
            },
        ]
    )
    assert summary.parent_model_turns == 1
    assert summary.nested_model_turns == 2
    assert summary.model_turns == 3
    assert summary.nested_usage.model_requests == 2
    assert summary.nested_usage.input_tokens == 80
    assert summary.tool_calls[0].duration_ms == 125
