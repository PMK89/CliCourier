from __future__ import annotations

import json
from typing import Any, Iterable

from cli_courier.agent.events import AgentEvent, AgentEventKind


def parse_gemini_jsonl_line(line: str, *, session_id: str | None = None) -> Iterable[AgentEvent]:
    stripped = line.strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        yield AgentEvent(
            kind=AgentEventKind.ERROR,
            text=f"Invalid Gemini CLI JSON event: {exc}",
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

    if event_type == "init":
        model = payload.get("model", "")
        yield AgentEvent(
            kind=AgentEventKind.SESSION_STARTED,
            text=f"Gemini CLI session started. model={model}" if model else "Gemini CLI session started.",
            session_id=resolved_session_id,
            data=payload,
        )
        return

    if event_type == "message":
        role = payload.get("role", "")
        if role == "assistant":
            content = payload.get("content", "")
            is_delta = payload.get("delta", False)
            if content:
                yield AgentEvent(
                    kind=AgentEventKind.ASSISTANT_DELTA if is_delta else AgentEventKind.FINAL_MESSAGE,
                    text=content,
                    session_id=resolved_session_id,
                    data=payload,
                )
        return

    if event_type == "tool_use":
        tool_name = payload.get("tool_name") or "tool"
        parameters = payload.get("parameters") or {}
        text = _format_tool_input(tool_name, parameters)
        yield AgentEvent(
            kind=AgentEventKind.TOOL_STARTED,
            text=text,
            session_id=resolved_session_id,
            tool_name=tool_name,
            tool_call_id=payload.get("tool_id"),
            data=payload,
        )
        return

    if event_type == "tool_result":
        tool_id = payload.get("tool_id")
        status = payload.get("status", "")
        is_error = status == "error"
        output = payload.get("output") or ""
        yield AgentEvent(
            kind=AgentEventKind.TOOL_FAILED if is_error else AgentEventKind.TOOL_COMPLETED,
            text=str(output),
            session_id=resolved_session_id,
            tool_call_id=tool_id,
            data=payload,
        )
        return

    if event_type == "result":
        status = payload.get("status", "")
        result_text = payload.get("result") or ""
        is_error = status == "error"
        if is_error:
            yield AgentEvent(
                kind=AgentEventKind.ERROR,
                text=result_text or "Gemini CLI reported an error.",
                session_id=resolved_session_id,
                data=payload,
            )
        elif result_text:
            yield AgentEvent(
                kind=AgentEventKind.FINAL_MESSAGE,
                text=result_text,
                session_id=resolved_session_id,
                data=payload,
            )
        else:
            yield AgentEvent(
                kind=AgentEventKind.STATUS,
                text=f"Gemini CLI result: {status}",
                session_id=resolved_session_id,
                is_debug=True,
                data=payload,
            )
        return

    # other internal events
    yield AgentEvent(
        kind=AgentEventKind.STATUS,
        text=event_type or "event",
        session_id=resolved_session_id,
        is_debug=True,
        data=payload,
    )


def _format_tool_input(tool_name: str, parameters: dict[str, Any]) -> str:
    if not parameters:
        return tool_name
    if "command" in parameters:
        return str(parameters["command"])
    if "description" in parameters:
        return str(parameters["description"])
    parts = ", ".join(f"{k}={v!r}" for k, v in list(parameters.items())[:3])
    return f"{tool_name}({parts})"
