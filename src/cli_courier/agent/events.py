from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4


class AgentEventKind(str, Enum):
    SESSION_STARTED = "session_started"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    ASSISTANT_DELTA = "assistant_delta"
    FINAL_MESSAGE = "final_message"
    REASONING = "reasoning"
    TOOL_STARTED = "tool_started"
    TOOL_DELTA = "tool_delta"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    FILE_CHANGED = "file_changed"
    ARTIFACT_AVAILABLE = "artifact_available"
    SCREENSHOT_AVAILABLE = "screenshot_available"
    ERROR = "error"
    STATUS = "status"
    CHOICE_REQUEST = "choice_request"


DEBUG_EVENT_KINDS = {
    AgentEventKind.REASONING,
    AgentEventKind.TOOL_DELTA,
    AgentEventKind.STATUS,
}

IMPORTANT_EVENT_KINDS = {
    AgentEventKind.FINAL_MESSAGE,
    AgentEventKind.APPROVAL_REQUESTED,
    AgentEventKind.ERROR,
    AgentEventKind.ARTIFACT_AVAILABLE,
    AgentEventKind.SCREENSHOT_AVAILABLE,
}


def new_event_id() -> str:
    return f"evt_{uuid4().hex[:12]}"


@dataclass(slots=True)
class AgentEvent:
    kind: AgentEventKind
    text: str = ""
    event_id: str = field(default_factory=new_event_id)
    session_id: str | None = None
    turn_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    title: str = ""
    tool_name: str | None = None
    tool_call_id: str | None = None
    approval_id: str | None = None
    artifact_path: str | None = None
    screenshot_path: str | None = None
    is_debug: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def display_text(self) -> str:
        if self.text:
            return self.text
        if self.title:
            return self.title
        if self.tool_name:
            return self.tool_name
        return self.kind.value


def coerce_event_kind(value: str) -> AgentEventKind:
    normalized = value.strip().lower().replace(".", "_").replace("-", "_")
    for kind in AgentEventKind:
        if normalized == kind.value:
            return kind
    return AgentEventKind.STATUS

