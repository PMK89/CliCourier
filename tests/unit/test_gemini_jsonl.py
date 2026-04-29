from __future__ import annotations

from cli_courier.agent.events import AgentEventKind
from cli_courier.agent.gemini_jsonl import parse_gemini_jsonl_line


def test_gemini_jsonl_maps_init_to_session_started() -> None:
    line = '{"type":"init","timestamp":"2026-04-29T14:57:35.396Z","session_id":"sess-1","model":"gemini-3"}'
    events = list(parse_gemini_jsonl_line(line))
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.SESSION_STARTED
    assert events[0].session_id == "sess-1"
    assert "gemini-3" in events[0].text


def test_gemini_jsonl_maps_message_assistant_delta_to_delta() -> None:
    line = '{"type":"message","timestamp":"2026-04-29T14:57:40.611Z","role":"assistant","content":"Hello","delta":true}'
    events = list(parse_gemini_jsonl_line(line))
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.ASSISTANT_DELTA
    assert events[0].text == "Hello"


def test_gemini_jsonl_maps_message_assistant_final_to_final() -> None:
    # If delta is false or missing, it's a final message
    line = '{"type":"message","role":"assistant","content":"Full message"}'
    events = list(parse_gemini_jsonl_line(line))
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.FINAL_MESSAGE
    assert events[0].text == "Full message"


def test_gemini_jsonl_maps_tool_use_to_tool_started() -> None:
    line = (
        '{"type":"tool_use","timestamp":"...","tool_name":"ls","tool_id":"call_123",'
        '"parameters":{"dir_path":"."}}'
    )
    events = list(parse_gemini_jsonl_line(line))
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.TOOL_STARTED
    assert events[0].tool_name == "ls"
    assert events[0].tool_call_id == "call_123"


def test_gemini_jsonl_maps_tool_result_to_tool_completed() -> None:
    line = '{"type":"tool_result","tool_id":"call_123","status":"success","output":"file1\\nfile2"}'
    events = list(parse_gemini_jsonl_line(line))
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.TOOL_COMPLETED
    assert events[0].tool_call_id == "call_123"
    assert events[0].text == "file1\nfile2"


def test_gemini_jsonl_maps_result_success_to_final_message() -> None:
    line = '{"type":"result","status":"success","stats":{"total_tokens":100}}'
    events = list(parse_gemini_jsonl_line(line))
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.STATUS


def test_gemini_jsonl_skips_user_message() -> None:
    line = '{"type":"message","role":"user","content":"hi"}'
    events = list(parse_gemini_jsonl_line(line))
    assert len(events) == 0
