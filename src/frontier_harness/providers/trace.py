"""Fold sanitized provider events into one compact execution trace."""

from __future__ import annotations

from typing import Any

from ..models import Usage
from ..util import canonical_json, sha256_text
from .base import ProviderTraceSummary, ToolCallSummary


class TraceAccumulator:
    """A small event fold; each event kind owns only its own accounting."""

    def __init__(self) -> None:
        self.calls: dict[str, ToolCallSummary] = {}
        self.model_turns = 0
        self.stop_reason: str | None = None

    def accept(self, event: dict[str, Any]) -> None:
        handlers = {
            "message_end": self._message,
            "tool_execution_start": self._tool_start,
            "tool_execution_end": self._tool_end,
        }
        handler = handlers.get(str(event.get("type") or ""))
        if handler is not None:
            handler(event)

    def summary(self) -> ProviderTraceSummary:
        ordered = list(self.calls.values())
        nested_usage = Usage()
        for item in ordered:
            nested_usage = nested_usage.plus(item.nested_usage)
        return ProviderTraceSummary(
            model_turns=self.model_turns + nested_usage.model_requests,
            parent_model_turns=self.model_turns,
            nested_model_turns=nested_usage.model_requests,
            nested_usage=nested_usage,
            tool_calls=ordered,
            tool_errors=sum(item.success is False for item in ordered),
            stop_reason=self.stop_reason,
        )

    def _message(self, event: dict[str, Any]) -> None:
        message = event.get("message")
        if not isinstance(message, dict):
            return
        self.model_turns += 1
        if isinstance(message.get("stopReason"), str):
            self.stop_reason = str(message["stopReason"])
        for block in message.get("content", []):
            if isinstance(block, dict) and block.get("type") == "toolCall":
                self._message_tool(block)

    def _message_tool(self, block: dict[str, Any]) -> None:
        call_id = str(block.get("id") or "")
        if not call_id:
            return
        self.calls[call_id] = ToolCallSummary(
            call_id=call_id,
            name=str(block.get("name") or "unknown"),
            intent=str(block.get("intent") or ""),
            arguments_sha256=self._digest(block.get("arguments", {})),
        )

    def _tool_start(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("toolCallId") or "")
        if not call_id:
            return
        current = self.calls.get(call_id)
        self.calls[call_id] = ToolCallSummary(
            call_id=call_id,
            name=str(event.get("toolName") or (current.name if current else "unknown")),
            intent=str(event.get("intent") or (current.intent if current else "")),
            arguments_sha256=self._digest(event.get("arguments", event.get("args", {}))),
        )

    def _tool_end(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("toolCallId") or "")
        if not call_id:
            return
        current = self.calls.get(call_id) or ToolCallSummary(
            call_id=call_id,
            name=str(event.get("toolName") or "unknown"),
        )
        result = event.get("result", {})
        details = result.get("details", {}) if isinstance(result, dict) else {}
        duration = self._duration(details)
        self.calls[call_id] = current.model_copy(
            update={
                "success": not bool(event.get("isError", False)),
                "duration_ms": duration,
                "result_sha256": self._digest(result),
                "nested_usage": self._nested_usage(details),
            }
        )

    @staticmethod
    def _duration(details: Any) -> float | None:
        if not isinstance(details, dict):
            return None
        value = details.get("wallTimeMs", details.get("totalDurationMs"))
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _nested_usage(details: Any) -> Usage:
        if not isinstance(details, dict):
            return Usage()
        raw = details.get("usage", {})
        usage = raw if isinstance(raw, dict) else {}
        results = details.get("results", [])
        requests = (
            sum(int(item.get("requests", 0) or 0) for item in results if isinstance(item, dict))
            if isinstance(results, list)
            else 0
        )
        return Usage(
            model_requests=requests,
            input_tokens=int(usage.get("input", 0) or 0),
            cached_input_tokens=int(usage.get("cacheRead", 0) or 0),
            output_tokens=int(usage.get("output", 0) or 0),
            reasoning_output_tokens=int(usage.get("reasoningTokens", 0) or 0),
        )

    @staticmethod
    def _digest(value: Any) -> str:
        if isinstance(value, dict) and value.get("sha256"):
            return str(value["sha256"])
        return sha256_text(canonical_json(value))


def trace_summary(events: list[dict[str, Any]]) -> ProviderTraceSummary:
    trace = TraceAccumulator()
    for event in events:
        trace.accept(event)
    return trace.summary()
