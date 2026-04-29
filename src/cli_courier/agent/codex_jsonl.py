from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from cli_courier.agent.events import AgentEvent, AgentEventKind

PROMPT_PLACEHOLDERS = {"{{prompt}}", "{prompt}", "<prompt>", "[prompt]", "explain this codebase"}


def parse_codex_jsonl_line(line: str, *, session_id: str | None = None) -> Iterable[AgentEvent]:
    stripped = line.strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        yield AgentEvent(
            kind=AgentEventKind.ERROR,
            text=f"Invalid Codex JSONL event: {exc}",
            session_id=session_id,
            data={"line": stripped},
        )
        return
    if not isinstance(payload, dict):
        yield AgentEvent(
            kind=AgentEventKind.STATUS,
            text=str(payload),
            session_id=session_id,
            data={"raw": payload},
        )
        return
    yield from codex_payload_to_events(payload, session_id=session_id)


def parse_codex_jsonl_lines(
    lines: Iterable[str],
    *,
    session_id: str | None = None,
) -> list[AgentEvent]:
    events = []
    for line in lines:
        for event in parse_codex_jsonl_line(line, session_id=session_id):
            events.append(event)
    return events


def codex_payload_to_events(
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
) -> Iterable[AgentEvent]:
    event_type = _event_type(payload)
    normalized_type = _normalize_type(event_type)
    native_session_id = _first_str(payload, "session_id", "sessionId", "conversation_id", "id")
    session_id = session_id or native_session_id
    turn_id = _first_str(payload, "turn_id", "turnId")

    if normalized_type in {"session_started", "session_configured", "session_created"}:
        yield AgentEvent(
            kind=AgentEventKind.SESSION_STARTED,
            text=_status_text(payload) or "Codex session started.",
            session_id=session_id,
            turn_id=turn_id,
            data=payload,
        )
        return
    if normalized_type in {"turn_started", "task_started"}:
        yield AgentEvent(
            kind=AgentEventKind.TURN_STARTED,
            text=_status_text(payload) or "Turn started.",
            session_id=session_id,
            turn_id=turn_id,
            data=payload,
        )
        return
    if normalized_type in {"turn_completed", "task_completed"}:
        yield AgentEvent(
            kind=AgentEventKind.TURN_COMPLETED,
            text=_status_text(payload) or "Turn completed.",
            session_id=session_id,
            turn_id=turn_id,
            data=payload,
        )
        return
    if normalized_type in {"turn_failed", "task_failed"}:
        yield AgentEvent(
            kind=AgentEventKind.TURN_FAILED,
            text=_text(payload) or "Turn failed.",
            session_id=session_id,
            turn_id=turn_id,
            is_debug=False,
            data=payload,
        )
        return
    if normalized_type in {"agent_message_delta", "assistant_delta", "message_delta"}:
        yield AgentEvent(
            kind=AgentEventKind.ASSISTANT_DELTA,
            text=_delta(payload),
            session_id=session_id,
            turn_id=turn_id,
            data=payload,
        )
        return
    if normalized_type in {"agent_message", "assistant_message", "final_message", "final_answer"}:
        yield AgentEvent(
            kind=_assistant_message_kind(payload, normalized_type=normalized_type),
            text=_text(payload),
            session_id=session_id,
            turn_id=turn_id,
            data=payload,
        )
        return
    if normalized_type in {"reasoning", "reasoning_delta", "agent_reasoning"}:
        yield AgentEvent(
            kind=AgentEventKind.REASONING,
            text=_delta(payload) or _text(payload),
            session_id=session_id,
            turn_id=turn_id,
            is_debug=True,
            data=payload,
        )
        return
    if normalized_type in {"tool_started", "tool_call", "function_call", "exec_command_begin"}:
        yield _tool_event(AgentEventKind.TOOL_STARTED, payload, session_id=session_id, turn_id=turn_id)
        return
    if normalized_type in {"tool_delta", "tool_output_delta", "exec_command_output"}:
        yield _tool_event(AgentEventKind.TOOL_DELTA, payload, session_id=session_id, turn_id=turn_id)
        return
    if normalized_type in {"tool_completed", "tool_result", "function_call_output", "exec_command_end"}:
        yield _tool_event(AgentEventKind.TOOL_COMPLETED, payload, session_id=session_id, turn_id=turn_id)
        return
    if normalized_type in {"tool_failed", "exec_command_failed"}:
        yield _tool_event(AgentEventKind.TOOL_FAILED, payload, session_id=session_id, turn_id=turn_id)
        return
    if normalized_type in {"approval_requested", "approval_request"}:
        yield _approval_event(payload, session_id=session_id, turn_id=turn_id)
        return
    if normalized_type in {"choice_request", "choice_requested", "selection_request", "selection_requested"}:
        event = _choice_event(payload, session_id=session_id, turn_id=turn_id)
        if event:
            yield event
        return
    if normalized_type in {"approval_resolved", "approval_decision"}:
        yield AgentEvent(
            kind=AgentEventKind.APPROVAL_RESOLVED,
            text=_text(payload) or "Approval resolved.",
            session_id=session_id,
            turn_id=turn_id,
            approval_id=_first_str(payload, "approval_id", "id"),
            data=payload,
        )
        return
    if normalized_type in {"file_changed", "file_modified", "patch_applied"}:
        yield AgentEvent(
            kind=AgentEventKind.FILE_CHANGED,
            text=_text(payload) or _first_str(payload, "path", "file") or "File changed.",
            session_id=session_id,
            turn_id=turn_id,
            artifact_path=_first_str(payload, "path", "file"),
            data=payload,
        )
        return
    if normalized_type in {"artifact_available", "artifact"}:
        yield AgentEvent(
            kind=AgentEventKind.ARTIFACT_AVAILABLE,
            text=_text(payload) or _first_str(payload, "path") or "Artifact available.",
            session_id=session_id,
            turn_id=turn_id,
            artifact_path=_first_str(payload, "path"),
            data=payload,
        )
        return
    if normalized_type in {"screenshot_available", "screenshot"}:
        yield AgentEvent(
            kind=AgentEventKind.SCREENSHOT_AVAILABLE,
            text=_text(payload) or _first_str(payload, "path") or "Screenshot available.",
            session_id=session_id,
            turn_id=turn_id,
            screenshot_path=_first_str(payload, "path"),
            data=payload,
        )
        return
    if normalized_type in {"error", "fatal_error"}:
        yield AgentEvent(
            kind=AgentEventKind.ERROR,
            text=_text(payload) or "Codex reported an error.",
            session_id=session_id,
            turn_id=turn_id,
            data=payload,
        )
        return
    if normalized_type == "response_item":
        item = payload.get("item")
        if isinstance(item, dict):
            yield _response_item_to_event(payload, item, session_id=session_id, turn_id=turn_id)
            return

    yield AgentEvent(
        kind=AgentEventKind.STATUS,
        text=_status_text(payload) or _text(payload) or event_type,
        session_id=session_id,
        turn_id=turn_id,
        is_debug=True,
        data=payload,
    )


def _response_item_to_event(
    payload: dict[str, Any],
    item: dict[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
) -> AgentEvent:
    item_type = _normalize_type(str(item.get("type", "")))
    merged = {**payload, **item, "item": item}
    if item_type in {"message", "assistant_message"} and item.get("role") == "assistant":
        return AgentEvent(
            kind=_assistant_message_kind(merged, normalized_type=item_type),
            text=_text(merged),
            session_id=session_id,
            turn_id=turn_id,
            data=payload,
        )
    if item_type in {"reasoning", "reasoning_summary"}:
        return AgentEvent(
            kind=AgentEventKind.REASONING,
            text=_text(merged),
            session_id=session_id,
            turn_id=turn_id,
            is_debug=True,
            data=payload,
        )
    if item_type in {"function_call", "tool_call", "local_shell_call"}:
        return _tool_event(AgentEventKind.TOOL_STARTED, merged, session_id=session_id, turn_id=turn_id)
    if item_type in {"function_call_output", "tool_call_output", "local_shell_call_output"}:
        return _tool_event(AgentEventKind.TOOL_COMPLETED, merged, session_id=session_id, turn_id=turn_id)
    return AgentEvent(
        kind=AgentEventKind.STATUS,
        text=_text(merged) or str(item.get("type", "response item")),
        session_id=session_id,
        turn_id=turn_id,
        is_debug=True,
        data=payload,
    )


def _tool_event(
    kind: AgentEventKind,
    payload: dict[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
) -> AgentEvent:
    tool_name = _first_str(payload, "tool_name", "name", "command", "cmd") or "tool"
    text = _delta(payload) or _text(payload) or tool_name
    return AgentEvent(
        kind=kind,
        text=text,
        session_id=session_id,
        turn_id=turn_id,
        tool_name=tool_name,
        tool_call_id=_first_str(payload, "tool_call_id", "call_id", "id"),
        is_debug=kind == AgentEventKind.TOOL_DELTA,
        data=payload,
    )


def _approval_event(
    payload: dict[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
) -> AgentEvent:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        choices = [
            {"id": "approve", "label": "Approve"},
            {"id": "reject", "label": "Reject"},
        ]
    return AgentEvent(
        kind=AgentEventKind.APPROVAL_REQUESTED,
        text=_text(payload) or "Approval required.",
        session_id=session_id,
        turn_id=turn_id,
        approval_id=_first_str(payload, "approval_id", "id"),
        data={**payload, "choices": choices},
    )


def _choice_event(
    payload: dict[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
) -> AgentEvent | None:
    prompt = _first_non_placeholder_str(payload, "prompt", "title")
    text = _text(payload)
    placeholder_only = _looks_like_prompt_placeholder(text) or any(
        isinstance(payload.get(key), str) and _looks_like_prompt_placeholder(payload[key])
        for key in ("prompt", "title")
    )
    if _looks_like_prompt_placeholder(text):
        text = ""
    choices = _normalize_choices(payload.get("choices"))
    if not choices or (placeholder_only and not (prompt or text)):
        return None
    return AgentEvent(
        kind=AgentEventKind.CHOICE_REQUEST,
        text=prompt or text or "Select an option.",
        session_id=session_id,
        turn_id=turn_id,
        data={**payload, "choices": choices},
    )


def _event_type(payload: dict[str, Any]) -> str:
    for key in ("type", "event", "event_type", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "status"


def _assistant_message_kind(payload: dict[str, Any], *, normalized_type: str) -> AgentEventKind:
    if normalized_type in {"final_message", "final_answer"}:
        return AgentEventKind.FINAL_MESSAGE
    channel = _normalize_type(_first_str(payload, "channel", "stream") or "")
    if channel in {"commentary", "comment", "progress"}:
        return AgentEventKind.ASSISTANT_DELTA
    return AgentEventKind.FINAL_MESSAGE


def _normalize_type(value: str) -> str:
    return value.strip().lower().replace(".", "_").replace("-", "_")


def _first_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_non_placeholder_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and not _looks_like_prompt_placeholder(value):
            return value
    return None


def _delta(payload: dict[str, Any]) -> str:
    value = payload.get("delta")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _text(value)
    return _text(payload, keys=("chunk", "output_delta", "text_delta"))


def _status_text(payload: dict[str, Any]) -> str:
    return _text(payload, keys=("message", "status", "summary"))


def _text(payload: dict[str, Any], *, keys: tuple[str, ...] = ("message", "text", "content", "output", "error")) -> str:
    for key in keys:
        value = payload.get(key)
        text = _stringify_text_value(value)
        if text:
            return text
    return ""


def _stringify_text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _stringify_text_value(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        if "text" in value:
            return _stringify_text_value(value["text"])
        if value.get("type") == "output_text" and "content" in value:
            return _stringify_text_value(value["content"])
        if "summary" in value:
            return _stringify_text_value(value["summary"])
    return ""


def _normalize_choices(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            label = item.strip()
            if not label or _looks_like_prompt_placeholder(label):
                continue
            normalized.append({"id": str(index), "label": label, "value": str(index)})
            continue
        if not isinstance(item, dict):
            continue
        label = _stringify_text_value(item.get("label")) or _stringify_text_value(item.get("text"))
        value_text = _stringify_text_value(item.get("value"))
        choice_id = _stringify_text_value(item.get("id")) or str(index)
        if _looks_like_prompt_placeholder(label):
            label = ""
        if _looks_like_prompt_placeholder(value_text):
            value_text = ""
        if _looks_like_prompt_placeholder(choice_id):
            choice_id = str(index)
        if not label and value_text:
            label = value_text
        if not label:
            continue
        normalized.append(
            {
                "id": choice_id,
                "label": label,
                "value": value_text or choice_id,
            }
        )
    return normalized


def _looks_like_prompt_placeholder(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    first_line = stripped.splitlines()[0].strip().lstrip("›>").strip()
    return first_line.lower() in PROMPT_PLACEHOLDERS
