"""Small typed projection from provider telemetry to operator activity."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderActivity:
    kind: str
    label: str
    message: str
    state: str = "active"
    action_id: str | None = None

    @classmethod
    def from_event(cls, event: dict[str, object]) -> ProviderActivity | None:
        event_type = str(event.get("type") or "provider")
        if event_type == "session":
            return cls(event_type, "session", "model context opened")
        if event_type == "tool_execution_start":
            return cls._tool_start(event)
        if event_type == "tool_execution_end":
            return cls._tool_end(event)
        if event_type == "subagent_activity":
            return cls._subagent(event)
        if event_type == "message_end":
            return cls(event_type, "model", "model returned a move boundary", "done")
        return None

    @classmethod
    def _tool_start(cls, event: dict[str, object]) -> ProviderActivity:
        intent = " ".join(str(event.get("intent") or "").split())
        return cls(
            "tool_execution_start",
            str(event.get("toolName") or "tool"),
            intent[:180] or "tool started",
            action_id=cls._action_id(event),
        )

    @classmethod
    def _tool_end(cls, event: dict[str, object]) -> ProviderActivity:
        failed = bool(event.get("isError"))
        return cls(
            "tool_execution_end",
            str(event.get("toolName") or "tool"),
            "tool failed" if failed else "tool completed",
            "warn" if failed else "done",
            cls._action_id(event),
        )

    @classmethod
    def _subagent(cls, event: dict[str, object]) -> ProviderActivity:
        candidate = str(event.get("state") or "active")
        state = candidate if candidate in {"active", "done", "warn"} else "active"
        message = " ".join(str(event.get("message") or "reported progress").split())[:180]
        return cls(
            "subagent_activity",
            str(event.get("agent") or "subagent"),
            message,
            state,
        )

    @staticmethod
    def _action_id(event: dict[str, object]) -> str | None:
        return str(event.get("toolCallId") or "") or None
