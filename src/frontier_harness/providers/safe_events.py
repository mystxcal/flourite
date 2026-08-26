"""Privacy-preserving projection of provider events into durable diagnostics."""

from __future__ import annotations

from typing import Any

from ..util import canonical_json, sha256_text


class SafeEventProjector:
    """Retain operational signal without retaining provider replay material."""

    def project(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event_type = event.get("type")
        if event_type == "session":
            return {key: event.get(key) for key in ("type", "version", "id", "timestamp", "cwd")}
        if event_type == "custom_message" and event.get("customType") == "irc:incoming":
            return self._subagent_activity(event.get("details"))
        if event_type == "tool_execution_start":
            return self._tool_start(event)
        if event_type == "tool_execution_end":
            return self._tool_end(event)
        if event_type == "message_end":
            return self._message_end(event.get("message"))
        return None

    @staticmethod
    def _label(value: Any, *, fallback: str) -> str:
        label = " ".join(str(value or "").split())[:80]
        return label or fallback

    def _subagent_activity(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        raw = str(value.get("message") or "").strip().casefold()
        if raw.startswith(("completed", "finished", "done")):
            state, message = "done", "completed assigned work"
        elif raw.startswith(("failed", "error")):
            state, message = "warn", "reported a failure"
        else:
            state, message = "active", "reported progress"
        return {
            "type": "subagent_activity",
            "agent": self._label(value.get("from"), fallback="subagent"),
            "state": state,
            "message": message,
        }

    def _tool_start(self, event: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(event.get("toolName") or "")
        return {
            "type": "tool_execution_start",
            "toolCallId": event.get("toolCallId"),
            "toolName": tool_name,
            "arguments": self._argument_summary(event.get("args", {}), tool_name=tool_name),
            "intent": event.get("intent", ""),
        }

    def _tool_end(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "tool_execution_end",
            "toolCallId": event.get("toolCallId"),
            "toolName": event.get("toolName"),
            "result": self._result_summary(event.get("result", {})),
            "isError": bool(event.get("isError", False)),
        }

    def _argument_summary(self, value: Any, *, tool_name: str) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "sha256": sha256_text(canonical_json(value)),
            "keys": sorted(value) if isinstance(value, dict) else [],
        }
        if tool_name != "task" or not isinstance(value, dict):
            return summary
        names = [
            self._label(item.get("name"), fallback="subagent")
            for item in value.get("tasks", [])
            if isinstance(item, dict)
        ]
        if names:
            summary["task_names"] = list(dict.fromkeys(names))[:16]
        return summary

    def _result_summary(self, value: Any) -> dict[str, Any]:
        summary: dict[str, Any] = {"sha256": sha256_text(canonical_json(value))}
        if not isinstance(value, dict):
            return summary
        content = [
            self._content_summary(block)
            for block in value.get("content", [])
            if isinstance(block, dict)
        ]
        if content:
            summary["content"] = content
        details = self._details_summary(value.get("details"))
        if details:
            summary["details"] = details
        return summary

    @staticmethod
    def _content_summary(block: dict[str, Any]) -> dict[str, Any]:
        item: dict[str, Any] = {"type": str(block.get("type") or "unknown")}
        text = block.get("text")
        if isinstance(text, str):
            item.update({"bytes": len(text.encode()), "sha256": sha256_text(text)})
        return item

    def _details_summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        summary = {
            key: value[key]
            for key in ("wallTimeMs", "totalDurationMs", "usage", "async")
            if key in value
        }
        results = [
            self._nested_result(item) for item in value.get("results", []) if isinstance(item, dict)
        ]
        if results:
            summary["results"] = results
        return summary

    @staticmethod
    def _nested_result(value: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value[key]
            for key in (
                "id",
                "agent",
                "agentSource",
                "modelRole",
                "resolvedModel",
                "requests",
                "tokens",
                "durationMs",
                "exitCode",
                "aborted",
                "truncated",
                "usage",
            )
            if key in value
        }
        if "output" in value:
            result["outputSha256"] = sha256_text(str(value["output"]))
        return result

    def _message_end(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or value.get("role") != "assistant":
            return None
        content = [
            projected
            for block in value.get("content", [])
            if isinstance(block, dict)
            if (projected := self._message_block(block)) is not None
        ]
        return {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": content,
                "usage": value.get("usage", {}),
                "stopReason": value.get("stopReason"),
                "model": value.get("model"),
                "provider": value.get("provider"),
                "responseId": value.get("responseId"),
            },
        }

    def _message_block(self, block: dict[str, Any]) -> dict[str, Any] | None:
        block_type = block.get("type")
        if block_type == "text":
            return {"type": "text", "text": block.get("text", "")}
        if block_type == "thinking":
            return {"type": "thinking", "thinking": block.get("thinking", "")}
        if block_type != "toolCall":
            return None
        tool_name = str(block.get("name") or "")
        return {
            "type": "toolCall",
            "id": block.get("id"),
            "name": tool_name,
            "arguments": self._argument_summary(
                block.get("arguments", {}),
                tool_name=tool_name,
            ),
            "intent": block.get("intent", ""),
        }


def safe_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Project one raw provider event onto its safe durable representation."""

    return SafeEventProjector().project(event)
