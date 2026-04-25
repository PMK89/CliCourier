from __future__ import annotations

from datetime import UTC, datetime

from cli_courier.agent.adapters import CodexAdapter, GenericCliAdapter
from cli_courier.agent.approval import detect_pending_approval, interpret_approval_text
from cli_courier.agent.chunking import OutputRingBuffer, chunk_text
from cli_courier.agent.output_filter import prepare_agent_output
from cli_courier.agent.session import resolve_terminal_backend
from cli_courier.agent.tmux import safe_tmux_session_name


def test_adapter_builds_configured_command() -> None:
    command = CodexAdapter().build_command("codex --model gpt-5")
    assert command == ["codex", "--model", "gpt-5"]


def test_resolve_terminal_backend_accepts_explicit_modes() -> None:
    assert resolve_terminal_backend("pty") == "pty"
    assert resolve_terminal_backend("tmux") == "tmux"


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


def test_prepare_agent_output_returns_empty_for_prompt_echo_only() -> None:
    output = prepare_agent_output(
        "›Pleaseopenawebsitewithplaywright  gpt-5.5 xhigh · ~/CliCourier\n",
        suppress_trace_lines=True,
    )

    assert output == ""
