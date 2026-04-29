from __future__ import annotations

import json
from typing import Any, Iterable

from cli_courier.agent.events import AgentEvent, AgentEventKind


def parse_claude_jsonl_line(line: str, *, session_id: str | None = None) -> Iterable[AgentEvent]:
    stripped = line.strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        yield AgentEvent(
            kind=AgentEventKind.ERROR,
            text=f"Invalid Claude Code JSON event: {exc}",
            session_id=session_id,
            data={"line": stripped},
        )
        return
    if not isinstance(payload, dict):
        yield AgentEvent(
            kind=AgentEventKind.STATUS,
            text=str(payload),
            session_id=session_id,
            is_debug=True,
            data={"raw": payload},
        )
        return
    yield from _payload_to_events(payload, session_id=session_id)


def _payload_to_events(payload: dict[str, Any], *, session_id: str | None) -> Iterable[AgentEvent]:
    event_type = payload.get("type", "")
    native_session_id = payload.get("session_id")
    resolved_session_id = session_id or native_session_id

    if event_type == "system":
        yield _handle_system(payload, session_id=resolved_session_id)
        return

    if event_type == "assistant":
        message = payload.get("message")
        if isinstance(message, dict):
            yield from _handle_assistant_message(message, payload, session_id=resolved_session_id)
        return

    if event_type == "user":
        message = payload.get("message")
        if isinstance(message, dict):
            yield from _handle_user_message(message, payload, session_id=resolved_session_id)
        return

    if event_type == "result":
        yield _handle_result(payload, session_id=resolved_session_id)
        return

    # rate_limit_event, stream_event, and other internal events
    yield AgentEvent(
        kind=AgentEventKind.STATUS,
        text=event_type or "event",
        session_id=resolved_session_id,
        is_debug=True,
        data=payload,
    )


def _handle_system(payload: dict[str, Any], *, session_id: str | None) -> AgentEvent:
    subtype = payload.get("subtype", "")
    if subtype == "init":
        model = payload.get("model", "")
        return AgentEvent(
            kind=AgentEventKind.SESSION_STARTED,
            text=f"Claude Code session started. model={model}" if model else "Claude Code session started.",
            session_id=session_id,
            data=payload,
        )
    return AgentEvent(
        kind=AgentEventKind.STATUS,
        text=payload.get("status", subtype or "system"),
        session_id=session_id,
        is_debug=True,
        data=payload,
    )


def _handle_assistant_message(
    message: dict[str, Any],
    payload: dict[str, Any],
    *,
    session_id: str | None,
) -> Iterable[AgentEvent]:
    content = message.get("content", [])
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")

        if block_type == "tool_use":
            tool_name = block.get("name") or "tool"
            tool_input = block.get("input") or {}
            text = _format_tool_input(tool_name, tool_input)
            yield AgentEvent(
                kind=AgentEventKind.TOOL_STARTED,
                text=text,
                session_id=session_id,
                tool_name=tool_name,
                tool_call_id=block.get("id"),
                data=payload,
            )

        elif block_type == "text":
            text = block.get("text", "")
            if text:
                yield AgentEvent(
                    kind=AgentEventKind.ASSISTANT_DELTA,
                    text=text,
                    session_id=session_id,
                    data=payload,
                )

        elif block_type == "thinking":
            thinking = block.get("thinking", "")
            if thinking:
                yield AgentEvent(
                    kind=AgentEventKind.REASONING,
                    text=thinking,
                    session_id=session_id,
                    is_debug=True,
                    data=payload,
                )


def _handle_user_message(
    message: dict[str, Any],
    payload: dict[str, Any],
    *,
    session_id: str | None,
) -> Iterable[AgentEvent]:
    content = message.get("content", [])
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            tool_call_id = block.get("tool_use_id")
            is_error = bool(block.get("is_error", False))
            raw_content = block.get("content", "")
            text = _stringify_tool_result_content(raw_content)
            # Prefer structured stdout/stderr from tool_use_result if available
            tool_use_result = payload.get("tool_use_result")
            if isinstance(tool_use_result, dict) and not text:
                stdout = tool_use_result.get("stdout", "")
                stderr = tool_use_result.get("stderr", "")
                text = stdout or stderr
            yield AgentEvent(
                kind=AgentEventKind.TOOL_FAILED if is_error else AgentEventKind.TOOL_COMPLETED,
                text=text,
                session_id=session_id,
                tool_call_id=tool_call_id,
                data=payload,
            )


def _handle_result(payload: dict[str, Any], *, session_id: str | None) -> AgentEvent:
    is_error = bool(payload.get("is_error", False))
    result_text = payload.get("result") or ""
    native_session_id = payload.get("session_id") or session_id

    if is_error:
        return AgentEvent(
            kind=AgentEventKind.ERROR,
            text=result_text or "Claude Code reported an error.",
            session_id=native_session_id,
            data=payload,
        )

    return AgentEvent(
        kind=AgentEventKind.FINAL_MESSAGE,
        text=result_text,
        session_id=native_session_id,
        data=payload,
    )


def _format_tool_input(tool_name: str, tool_input: dict[str, Any]) -> str:
    if not tool_input:
        return tool_name
    # Use command/description for Bash; fall back to JSON-like summary
    if "command" in tool_input:
        return str(tool_input["command"])
    if "description" in tool_input:
        return str(tool_input["description"])
    parts = ", ".join(f"{k}={v!r}" for k, v in list(tool_input.items())[:3])
    return f"{tool_name}({parts})"


def _stringify_tool_result_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(str(text))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(value) if value else ""
