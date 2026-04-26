from __future__ import annotations

from datetime import UTC, datetime

from cli_courier.agent.adapters import CodexAdapter, GenericCliAdapter
from cli_courier.agent.approval import detect_pending_approval, interpret_approval_text
from cli_courier.agent.chunking import OutputRingBuffer, chunk_text
from cli_courier.agent.codex_jsonl import parse_codex_jsonl_line, parse_codex_jsonl_lines
from cli_courier.agent.events import AgentEventKind
from cli_courier.agent.output_filter import agent_output_in_progress, prepare_agent_output
from cli_courier.agent.session import AgentSession, resolve_agent_backend, resolve_terminal_backend
from cli_courier.agent.tmux import safe_tmux_session_name


def test_adapter_builds_configured_command() -> None:
    command = CodexAdapter().build_command("codex --model gpt-5")
    assert command == ["codex", "--model", "gpt-5"]


def test_codex_adapter_builds_structured_exec_command(tmp_path) -> None:
    command = CodexAdapter().build_structured_turn_command(
        ["codex", "--model", "gpt-5"],
        prompt="hello",
        cwd=str(tmp_path),
        resume=False,
        output_last_message_path="/tmp/final.txt",
    )

    assert command == [
        "codex",
        "exec",
        "--model",
        "gpt-5",
        "--cd",
        str(tmp_path),
        "--json",
        "--output-last-message",
        "/tmp/final.txt",
        "hello",
    ]


def test_codex_adapter_builds_structured_resume_command() -> None:
    command = CodexAdapter().build_structured_turn_command(
        ["codex"],
        prompt="follow up",
        cwd="/repo",
        resume=True,
    )

    assert command == ["codex", "exec", "resume", "--last", "--json", "follow up"]


def test_resolve_terminal_backend_accepts_explicit_modes() -> None:
    assert resolve_terminal_backend("pty") == "pty"
    assert resolve_terminal_backend("tmux") == "tmux"


def test_codex_defaults_to_structured_backend() -> None:
    assert resolve_agent_backend(CodexAdapter(), "auto") == "structured"


def test_agent_session_accumulates_tmux_output_deltas(tmp_path) -> None:
    session = AgentSession(
        adapter=GenericCliAdapter(),
        command=["sh"],
        cwd=tmp_path,
        recent_output_max_chars=1000,
        terminal_backend="tmux",
    )

    assert session.replaces_output_snapshots is False


def test_tmux_session_names_are_sanitized(tmp_path) -> None:
    assert safe_tmux_session_name("Cli Courier:/repo", workspace=tmp_path) == "Cli-Courier-repo"


def test_detect_pending_approval_from_recent_output() -> None:
    pending = detect_pending_approval(
        "About to edit files. Do you want to proceed? [y/N]",
        GenericCliAdapter(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert pending is not None
    assert pending.adapter_id == "generic"
    assert "proceed" in pending.prompt_excerpt


def test_detect_pending_approval_ignores_auto_approval_output() -> None:
    pending = detect_pending_approval(
        "Do you want to proceed? [y/N]\n"
        "⚠ Automatic approval review approved (risk: low, authorization: high)\n"
        "✔ Auto-reviewer approved codex to run cp screenshot.png output/screenshot.png this time",
        GenericCliAdapter(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert pending is None


def test_interpret_approval_words() -> None:
    assert interpret_approval_text("okay") == "approve"
    assert interpret_approval_text("y") == "approve"
    assert interpret_approval_text("👍") == "approve"
    assert interpret_approval_text("👍🏻") == "approve"
    assert interpret_approval_text("❤️") == "approve"
    assert interpret_approval_text("cancel") == "reject"
    assert interpret_approval_text("n") == "reject"
    assert interpret_approval_text("👎") == "reject"
    assert interpret_approval_text("yes, run tests") is None


def test_ring_buffer_truncates_old_output() -> None:
    buffer = OutputRingBuffer(5)
    buffer.append("hello")
    buffer.append(" world")
    assert buffer.recent() == "world"


def test_chunk_text_prefers_newline_boundaries() -> None:
    assert chunk_text("aaa\nbbb\nccc", 7) == ["aaa\nbbb", "ccc"]


def test_prepare_agent_output_suppresses_trace_lines() -> None:
    output = prepare_agent_output(
        "thinking\nrunning tool: shell\nFinal answer\n",
        suppress_trace_lines=True,
    )

    assert output == "Final answer"


def test_prepare_agent_output_suppresses_codex_prompt_echo() -> None:
    output = prepare_agent_output(
        "›Pleaseopenawebsitewithplaywright  gpt-5.5 xhigh · ~/CliCourier\nFinal answer\n",
        suppress_trace_lines=True,
    )

    assert output == "Final answer"


def test_prepare_agent_output_preserves_codex_marked_final_lines() -> None:
    output = prepare_agent_output(
        "› Fixed final-output forwarding for Codex.\n"
        "› Added regression coverage for false choice prompts.\n"
        "Verified with pytest.\n",
        suppress_trace_lines=True,
    )

    assert output == (
        "Fixed final-output forwarding for Codex.\n"
        "Added regression coverage for false choice prompts.\n"
        "Verified with pytest."
    )


def test_prepare_agent_output_returns_empty_for_prompt_echo_only() -> None:
    output = prepare_agent_output(
        "›Pleaseopenawebsitewithplaywright  gpt-5.5 xhigh · ~/CliCourier\n",
        suppress_trace_lines=True,
    )

    assert output == ""


def test_prepare_agent_output_suppresses_working_status_lines() -> None:
    output = prepare_agent_output(
        "Working (14s • esc to interrupt)\n⠋ Reading files\nFinal answer\n",
        suppress_trace_lines=True,
    )

    assert output == "Final answer"


def test_prepare_agent_output_suppresses_codex_startup_banner() -> None:
    output = prepare_agent_output(
        "⚠ Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces.\n"
        "╭──────────────────────────────────────────────╮\n"
        "│ >_ OpenAI Codex (v0.125.0)                   │\n"
        "│ model:     gpt-5.5 medium   /model to change │\n"
        "│ directory: ~/CliCourier                      │\n"
        "╰──────────────────────────────────────────────╯\n"
        "Tip: New Use /fast to enable our fastest inference with increased plan usage.\n"
        "⚠ `[features].collab` is deprecated.\n"
        "[Pasted Content 1024 chars]\n"
        "Final answer\n",
        suppress_trace_lines=True,
    )

    assert output == "Final answer"


def test_prepare_agent_output_preserves_complete_done_response() -> None:
    output = prepare_agent_output(
        "thinking\n"
        "functions.exec_command({...})\n"
        "Done.\n"
        "\n"
        "Voice confirmation now works like this:\n"
        "- Send voice/audio.\n"
        "- CliCourier shows transcript with Send/Reject.\n"
        "- Reply with corrected plain text to replace the pending transcript.\n"
        "- Tap Send or use /voice_approve; it sends the corrected version.\n"
        "\n"
        "Verified with: .venv/bin/pytest -q\n"
        "Result: 82 passed\n",
        suppress_trace_lines=True,
    )

    assert output == (
        "Done.\n"
        "\n"
        "Voice confirmation now works like this:\n"
        "- Send voice/audio.\n"
        "- CliCourier shows transcript with Send/Reject.\n"
        "- Reply with corrected plain text to replace the pending transcript.\n"
        "- Tap Send or use /voice_approve; it sends the corrected version.\n"
        "\n"
        "Verified with: .venv/bin/pytest -q\n"
        "Result: 82 passed"
    )


def test_agent_output_in_progress_detects_codex_working_status() -> None:
    assert agent_output_in_progress("Working (14s • esc to interrupt)")


def test_agent_output_in_progress_ignores_stale_working_status() -> None:
    assert not agent_output_in_progress("Working (14s • esc to interrupt)\nFinal answer")


def test_codex_jsonl_maps_session_and_final_message() -> None:
    events = parse_codex_jsonl_lines(
        [
            '{"type":"session_configured","session_id":"sess_1","model":"gpt-5"}',
            '{"type":"agent_message","message":"Done."}',
        ]
    )

    assert [event.kind for event in events] == [
        AgentEventKind.SESSION_STARTED,
        AgentEventKind.FINAL_MESSAGE,
    ]
    assert events[0].session_id == "sess_1"
    assert events[1].text == "Done."


def test_codex_jsonl_maps_tool_events() -> None:
    event = parse_codex_jsonl_line(
        '{"type":"response_item","item":{"type":"function_call","name":"shell",'
        '"arguments":"{\\"cmd\\":\\"pytest\\"}","call_id":"call_1"}}'
    )

    assert event is not None
    assert event.kind == AgentEventKind.TOOL_STARTED
    assert event.tool_name == "shell"
    assert event.tool_call_id == "call_1"


def test_codex_jsonl_maps_tool_output() -> None:
    event = parse_codex_jsonl_line(
        '{"type":"response_item","item":{"type":"function_call_output",'
        '"output":"tests passed","call_id":"call_1"}}'
    )

    assert event is not None
    assert event.kind == AgentEventKind.TOOL_COMPLETED
    assert event.text == "tests passed"


def test_codex_jsonl_maps_approval_request() -> None:
    event = parse_codex_jsonl_line(
        '{"type":"approval_requested","id":"approval_1","message":"Run tests?",'
        '"choices":[{"id":"approve","label":"Approve"},{"id":"reject","label":"Reject"}]}'
    )

    assert event is not None
    assert event.kind == AgentEventKind.APPROVAL_REQUESTED
    assert event.approval_id == "approval_1"
    assert event.text == "Run tests?"
