from __future__ import annotations

from datetime import UTC, datetime

from cli_courier.agent.adapters import ClaudeAdapter, CodexAdapter, GeminiAdapter, GenericCliAdapter
from cli_courier.agent.approval import detect_pending_approval, interpret_approval_text
from cli_courier.agent.chunking import OutputRingBuffer, chunk_text
from cli_courier.agent.claude_jsonl import parse_claude_jsonl_line
from cli_courier.agent.codex_jsonl import parse_codex_jsonl_line, parse_codex_jsonl_lines
from cli_courier.agent.events import AgentEvent, AgentEventKind
from cli_courier.agent.output_filter import agent_output_in_progress, prepare_agent_output
from cli_courier.agent.pty import build_agent_env
from cli_courier.agent.session import AgentSession, resolve_agent_backend, resolve_terminal_backend
from cli_courier.agent.structured import _select_final_message_text
from cli_courier.agent.tmux import TmuxAgentProcess, safe_tmux_session_name


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


def test_codex_adapter_builds_interactive_resume_command() -> None:
    command = CodexAdapter().build_resume_command(["codex", "--model", "gpt-5.5"])

    assert command == ["codex", "resume", "--last", "--model", "gpt-5.5"]


def test_resolve_terminal_backend_accepts_explicit_modes() -> None:
    assert resolve_terminal_backend("pty") == "pty"
    assert resolve_terminal_backend("tmux") == "tmux"


def test_auto_backend_prefers_tmux_when_available(monkeypatch) -> None:
    monkeypatch.setattr("cli_courier.agent.session.shutil.which", lambda name: "/usr/bin/tmux")

    assert resolve_agent_backend(CodexAdapter(), "auto") == "tmux"


def test_codex_auto_backend_uses_structured_without_tmux(monkeypatch) -> None:
    monkeypatch.setattr("cli_courier.agent.session.shutil.which", lambda name: None)

    assert resolve_agent_backend(CodexAdapter(), "auto") == "structured"


def test_agent_session_replaces_tmux_output_snapshots(tmp_path) -> None:
    session = AgentSession(
        adapter=GenericCliAdapter(),
        command=["sh"],
        cwd=tmp_path,
        recent_output_max_chars=1000,
        terminal_backend="tmux",
    )

    assert session.replaces_output_snapshots is True

    session._record_event(AgentEvent(kind=AgentEventKind.ASSISTANT_DELTA, text="old screen"))
    session._record_event(AgentEvent(kind=AgentEventKind.ASSISTANT_DELTA, text="new screen"))

    assert session.recent_output() == "new screen"


def test_agent_session_uses_resume_command_for_codex_terminal_backend(tmp_path) -> None:
    session = AgentSession(
        adapter=CodexAdapter(),
        command=["codex", "--model", "gpt-5.5"],
        cwd=tmp_path,
        recent_output_max_chars=1000,
        terminal_backend="tmux",
        resume_last=True,
    )

    assert session.command == ["codex", "resume", "--last", "--model", "gpt-5.5"]


def test_agent_session_uses_resume_command_for_claude_terminal_backend(tmp_path) -> None:
    session = AgentSession(
        adapter=ClaudeAdapter(),
        command=["claude", "--dangerously-skip-permissions"],
        cwd=tmp_path,
        recent_output_max_chars=1000,
        terminal_backend="tmux",
        resume_last=True,
    )

    assert session.command == ["claude", "--dangerously-skip-permissions", "--continue"]


def test_agent_session_uses_resume_command_for_gemini_terminal_backend(tmp_path) -> None:
    session = AgentSession(
        adapter=GeminiAdapter(),
        command=["gemini"],
        cwd=tmp_path,
        recent_output_max_chars=1000,
        terminal_backend="tmux",
        resume_last=True,
    )

    assert session.command == ["gemini", "--resume", "latest"]


def test_agent_session_uses_resume_last_for_codex_structured_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("cli_courier.agent.session.shutil.which", lambda name: None)

    session = AgentSession(
        adapter=CodexAdapter(),
        command=["codex"],
        cwd=tmp_path,
        recent_output_max_chars=1000,
        terminal_backend="auto",
        resume_last=True,
    )

    assert session.resume_last is True
    assert getattr(session._process, "_has_started_turn") is True


def test_agent_session_resets_tmux_snapshot_output_for_next_turn(tmp_path) -> None:
    session = AgentSession(
        adapter=GenericCliAdapter(),
        command=["sh"],
        cwd=tmp_path,
        recent_output_max_chars=1000,
        terminal_backend="tmux",
    )
    session._record_event(AgentEvent(kind=AgentEventKind.ASSISTANT_DELTA, text="old line 1\nold line 2"))

    session.reset_output_for_next_turn()
    session._record_event(AgentEvent(kind=AgentEventKind.ASSISTANT_DELTA, text="old line 1\nold line 2"))

    assert session.recent_output() == ""

    session._record_event(
        AgentEvent(
            kind=AgentEventKind.ASSISTANT_DELTA,
            text="old line 1\nold line 2\nnew line",
        )
    )

    assert session.recent_output() == "new line"


def test_agent_session_resets_tmux_snapshot_output_when_capture_scrolls(tmp_path) -> None:
    session = AgentSession(
        adapter=GenericCliAdapter(),
        command=["sh"],
        cwd=tmp_path,
        recent_output_max_chars=1000,
        terminal_backend="tmux",
    )
    session._record_event(
        AgentEvent(kind=AgentEventKind.ASSISTANT_DELTA, text="old line 1\nold line 2\nold line 3")
    )

    session.reset_output_for_next_turn()
    session._record_event(AgentEvent(kind=AgentEventKind.ASSISTANT_DELTA, text="old line 2\nold line 3\nnew line"))

    assert session.recent_output() == "new line"


def test_tmux_session_names_are_sanitized(tmp_path) -> None:
    assert safe_tmux_session_name("Cli Courier:/repo", workspace=tmp_path) == "Cli-Courier-repo"


def test_tmux_process_does_not_treat_dead_pane_as_running(tmp_path, monkeypatch) -> None:
    def fake_run(command, **kwargs):
        if command[:3] == ["tmux", "list-panes", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "1\n"})()
        if command[:3] == ["tmux", "has-session", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": ""})()
        raise AssertionError(command)

    monkeypatch.setattr("cli_courier.agent.tmux.subprocess.run", fake_run)
    process = TmuxAgentProcess(["sh"], cwd=tmp_path, session_name="clicourier")

    assert process.is_running is False


def test_tmux_process_does_not_treat_unmanaged_live_shell_as_running(tmp_path, monkeypatch) -> None:
    def fake_run(command, **kwargs):
        if command[:3] == ["tmux", "list-panes", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "0\n"})()
        if command[:4] == ["tmux", "show-options", "-qv", "-t"]:
            return type("Result", (), {"returncode": 1, "stdout": ""})()
        raise AssertionError(command)

    monkeypatch.setattr("cli_courier.agent.tmux.subprocess.run", fake_run)
    process = TmuxAgentProcess(["sh"], cwd=tmp_path, session_name="clicourier")

    assert process.is_running is False


def test_tmux_process_does_not_treat_stuck_running_shell_as_running(tmp_path, monkeypatch) -> None:
    def fake_run(command, **kwargs):
        if command[:3] == ["tmux", "list-panes", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "0\n"})()
        if command[:4] == ["tmux", "show-options", "-qv", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "running\n"})()
        if command[:4] == ["tmux", "display-message", "-p", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "bash\n"})()
        raise AssertionError(command)

    monkeypatch.setattr("cli_courier.agent.tmux.subprocess.run", fake_run)
    process = TmuxAgentProcess(["sh"], cwd=tmp_path, session_name="clicourier")

    assert process.is_running is False


def test_tmux_process_treats_running_agent_pane_as_running(tmp_path, monkeypatch) -> None:
    def fake_run(command, **kwargs):
        if command[:3] == ["tmux", "list-panes", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "0\n"})()
        if command[:4] == ["tmux", "show-options", "-qv", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "running\n"})()
        if command[:4] == ["tmux", "display-message", "-p", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "claude\n"})()
        raise AssertionError(command)

    monkeypatch.setattr("cli_courier.agent.tmux.subprocess.run", fake_run)
    process = TmuxAgentProcess(["sh"], cwd=tmp_path, session_name="clicourier")

    assert process.is_running is True


def test_tmux_process_treats_shell_with_child_as_running(tmp_path, monkeypatch) -> None:
    def fake_run(command, **kwargs):
        if command[:3] == ["tmux", "list-panes", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "0\n"})()
        if command[:4] == ["tmux", "show-options", "-qv", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "running\n"})()
        if command[:4] == ["tmux", "display-message", "-p", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "bash\t123\n"})()
        if command[:3] == ["ps", "-eo", "ppid=,pid="]:
            return type("Result", (), {"returncode": 0, "stdout": "123 456\n"})()
        raise AssertionError(command)

    monkeypatch.setattr("cli_courier.agent.tmux.subprocess.run", fake_run)
    process = TmuxAgentProcess(["sh"], cwd=tmp_path, session_name="clicourier")

    assert process.is_running is True


def test_tmux_process_waits_longer_before_submitting_large_text(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["tmux", "capture-pane", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "x" * 80})()
        return type("Result", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr("cli_courier.agent.tmux.subprocess.run", fake_run)
    monkeypatch.setattr("cli_courier.agent.tmux.time.sleep", lambda seconds: sleeps.append(seconds))
    process = TmuxAgentProcess(
        ["sh"],
        cwd=tmp_path,
        session_name="clicourier",
        submit_delay_seconds=0.05,
    )

    process._send_text_with_submit("x" * 4000, "\r")

    assert sleeps == [0.08, 0.08, 0.08, 0.08, 0.08, 1.15, 0.15, 0.35]
    assert [call[:4] for call in calls[:6]] == [
        ["tmux", "send-keys", "-t", "clicourier:0.0"],
        ["tmux", "send-keys", "-t", "clicourier:0.0"],
        ["tmux", "send-keys", "-t", "clicourier:0.0"],
        ["tmux", "send-keys", "-t", "clicourier:0.0"],
        ["tmux", "send-keys", "-t", "clicourier:0.0"],
        ["tmux", "send-keys", "-t", "clicourier:0.0"],
    ]
    assert ["tmux", "capture-pane", "-t", "clicourier:0.0", "-p", "-J"] in calls
    assert calls[-2] == ["tmux", "send-keys", "-t", "clicourier:0.0", "Enter"]
    assert calls[-1] == ["tmux", "send-keys", "-t", "clicourier:0.0", "Enter"]


def test_tmux_process_waits_for_visible_input_tail_before_submit(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []
    snapshots = iter(["", "partial prompt", "visible final tail"])

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["tmux", "capture-pane", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": next(snapshots)})()
        return type("Result", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr("cli_courier.agent.tmux.subprocess.run", fake_run)
    monkeypatch.setattr("cli_courier.agent.tmux.time.sleep", lambda seconds: sleeps.append(seconds))
    process = TmuxAgentProcess(
        ["sh"],
        cwd=tmp_path,
        session_name="clicourier",
        submit_delay_seconds=0,
    )

    process._send_text_with_submit("visible final tail", "\r")

    capture_calls = [call for call in calls if call[:3] == ["tmux", "capture-pane", "-t"]]
    assert len(capture_calls) == 3
    assert len(sleeps) == 4
    assert 0.15 < sleeps[0] < 0.16
    assert sleeps[1:3] == [0.05, 0.05]
    assert sleeps[3] == 0.15  # settle delay after tail found
    assert calls[-1] == ["tmux", "send-keys", "-t", "clicourier:0.0", "Enter"]


def test_default_agent_env_preserves_cli_login_but_not_provider_api_keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anthropic-auth-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://claude.example.test")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", "/tmp/anthropic")
    monkeypatch.setenv("ANTHROPIC_PROFILE", "work")
    monkeypatch.setenv("CLAUDE_CODE_API_BASE_URL", "https://claude-code.example.test")
    monkeypatch.setenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", "https://oauth.example.test")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/claude")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-test")
    monkeypatch.setenv("CLAUDE_API_KEY", "claude-api-test")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/config")
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-forward")

    env = build_agent_env()

    assert env["ANTHROPIC_API_KEY"] == "anthropic-test"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "anthropic-auth-test"
    assert env["ANTHROPIC_BASE_URL"] == "https://claude.example.test"
    assert env["ANTHROPIC_CONFIG_DIR"] == "/tmp/anthropic"
    assert env["ANTHROPIC_PROFILE"] == "work"
    assert env["CLAUDE_CODE_API_BASE_URL"] == "https://claude-code.example.test"
    assert env["CLAUDE_CODE_CUSTOM_OAUTH_URL"] == "https://oauth.example.test"
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/claude"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-test"
    assert env["XDG_CONFIG_HOME"] == "/tmp/config"
    assert "CLAUDE_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env
    assert "UNRELATED_SECRET" not in env


def test_agent_env_allowlist_can_forward_provider_api_keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")

    env = build_agent_env(("ANTHROPIC_API_KEY",))

    assert env["ANTHROPIC_API_KEY"] == "anthropic-test"


def test_tmux_shell_command_keeps_pane_open_after_agent_exit(tmp_path) -> None:
    process = TmuxAgentProcess(
        ["claude"],
        cwd=tmp_path,
        env={"HOME": "/root", "PATH": "/usr/bin", "SHELL": "/bin/bash"},
        session_name="clicourier",
    )

    command = process._shell_command()

    assert command.startswith("tmux set-option")
    assert " env -i " in command
    assert " claude; status=$?; " in command
    assert "@clicourier_agent_state exited" in command
    assert "[CliCourier] agent exited with status %s" in command
    assert 'exec "${SHELL:-/bin/sh}"' in command


async def test_tmux_start_replaces_existing_dead_session(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    has_session = True
    live_pane = False
    agent_state = ""

    def fake_run(command, **kwargs):
        nonlocal has_session, live_pane, agent_state
        calls.append(command)
        if command[:3] == ["tmux", "has-session", "-t"]:
            return type("Result", (), {"returncode": 0 if has_session else 1, "stdout": ""})()
        if command[:3] == ["tmux", "list-panes", "-t"]:
            stdout = "0\n" if live_pane else "1\n"
            return type("Result", (), {"returncode": 0 if has_session else 1, "stdout": stdout})()
        if command[:4] == ["tmux", "show-options", "-qv", "-t"]:
            return type("Result", (), {"returncode": 0 if agent_state else 1, "stdout": agent_state})()
        if command[:4] == ["tmux", "display-message", "-p", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "claude\n"})()
        if command[:3] == ["tmux", "list-clients", "-t"]:
            return type("Result", (), {"returncode": 1, "stdout": ""})()
        if command[:3] == ["tmux", "kill-session", "-t"]:
            has_session = False
            live_pane = False
            agent_state = ""
            return type("Result", (), {"returncode": 0, "stdout": ""})()
        if command[:3] == ["tmux", "new-session", "-d"]:
            has_session = True
            live_pane = True
            agent_state = "running\n"
            return type("Result", (), {"returncode": 0, "stdout": ""})()
        if command[:3] == ["tmux", "capture-pane", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": ""})()
        raise AssertionError(command)

    monkeypatch.setattr("cli_courier.agent.tmux.tmux_available", lambda: True)
    monkeypatch.setattr("cli_courier.agent.tmux.subprocess.run", fake_run)
    process = TmuxAgentProcess(
        ["sh"],
        cwd=tmp_path,
        session_name="clicourier",
        poll_interval_seconds=60,
    )

    await process.start()
    await process.stop()

    assert ["tmux", "kill-session", "-t", "clicourier"] in calls
    assert any(command[:3] == ["tmux", "new-session", "-d"] for command in calls)


async def test_tmux_start_replaces_existing_unmanaged_live_session(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    has_session = True
    live_pane = True
    agent_state = ""

    def fake_run(command, **kwargs):
        nonlocal has_session, live_pane, agent_state
        calls.append(command)
        if command[:3] == ["tmux", "has-session", "-t"]:
            return type("Result", (), {"returncode": 0 if has_session else 1, "stdout": ""})()
        if command[:3] == ["tmux", "list-panes", "-t"]:
            stdout = "0\n" if live_pane else "1\n"
            return type("Result", (), {"returncode": 0 if has_session else 1, "stdout": stdout})()
        if command[:4] == ["tmux", "show-options", "-qv", "-t"]:
            return type("Result", (), {"returncode": 0 if agent_state else 1, "stdout": agent_state})()
        if command[:4] == ["tmux", "display-message", "-p", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": "claude\n"})()
        if command[:3] == ["tmux", "list-clients", "-t"]:
            return type("Result", (), {"returncode": 1, "stdout": ""})()
        if command[:3] == ["tmux", "kill-session", "-t"]:
            has_session = False
            live_pane = False
            agent_state = ""
            return type("Result", (), {"returncode": 0, "stdout": ""})()
        if command[:3] == ["tmux", "new-session", "-d"]:
            has_session = True
            live_pane = True
            agent_state = "running\n"
            return type("Result", (), {"returncode": 0, "stdout": ""})()
        if command[:3] == ["tmux", "capture-pane", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": ""})()
        raise AssertionError(command)

    monkeypatch.setattr("cli_courier.agent.tmux.tmux_available", lambda: True)
    monkeypatch.setattr("cli_courier.agent.tmux.subprocess.run", fake_run)
    process = TmuxAgentProcess(
        ["sh"],
        cwd=tmp_path,
        session_name="clicourier",
        poll_interval_seconds=60,
    )

    await process.start()
    await process.stop()

    assert ["tmux", "kill-session", "-t", "clicourier"] in calls
    assert any(command[:3] == ["tmux", "new-session", "-d"] for command in calls)


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


def test_detect_pending_approval_ignores_auto_approval_near_git_status_questions() -> None:
    pending = detect_pending_approval(
        "✔ Auto-reviewer approved codex to run git status --short this time\n"
        " M src/cli_courier/telegram_bot/runtime.py\n"
        "?? tests/unit/test_output_renderer.py\n",
        GenericCliAdapter(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert pending is None


def test_detect_pending_approval_omits_codex_status_from_prompt_excerpt() -> None:
    pending = detect_pending_approval(
        "› Write tests for @filename\n"
        "Find and fix a bug in @filename\n"
        "gpt-5.5 xhigh · ~/CliCourier Working (1m 45s • esc to interrupt)\n"
        "Do you want to proceed? [y/N]\n",
        GenericCliAdapter(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert pending is not None
    assert pending.prompt_excerpt == "Do you want to proceed? [y/N]"
    assert "Working" not in pending.prompt_excerpt
    assert "Write tests" not in pending.prompt_excerpt
    assert "Find and fix" not in pending.prompt_excerpt


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
    # Extended approval words
    for word in ("sure", "yep", "yup", "yeah", "alright", "go ahead", "go for it",
                 "do it", "fine", "sounds good", "agreed", "confirm", "confirmed",
                 "accept", "accepted", "grant", "granted"):
        assert interpret_approval_text(word) == "approve", word
        assert interpret_approval_text(word.upper()) == "approve", word
    # Extended rejection words
    for word in ("nope", "nah", "abort", "refuse", "refused", "decline", "declined",
                 "no way", "skip", "never", "denied", "deny"):
        assert interpret_approval_text(word) == "reject", word
        assert interpret_approval_text(word.upper()) == "reject", word


def test_ring_buffer_truncates_old_output() -> None:
    buffer = OutputRingBuffer(5)
    buffer.append("hello")
    buffer.append(" world")
    assert buffer.recent() == "world"


def test_ring_buffer_replace_discards_old_output() -> None:
    buffer = OutputRingBuffer(20)
    buffer.append("old terminal snapshot")
    buffer.replace("new snapshot")

    assert buffer.recent() == "new snapshot"


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


def test_prepare_agent_output_suppresses_codex_input_placeholder() -> None:
    output = prepare_agent_output(
        "› Write tests for @filename\n"
        "  gpt-5.5 xhigh · ~/CliCourier\n"
        "Actual output\n",
        suppress_trace_lines=True,
    )

    assert output == "Actual output"


def test_prepare_agent_output_suppresses_codex_find_bug_placeholder() -> None:
    output = prepare_agent_output(
        "Find and fix a bug in @filename\n"
        "Actual output\n",
        suppress_trace_lines=True,
    )

    assert output == "Actual output"


def test_prepare_agent_output_suppresses_codex_explain_codebase_placeholder() -> None:
    output = prepare_agent_output(
        "› Explain this codebase\n"
        "│ Explain this codebase │\n"
        "Actual output\n",
        suppress_trace_lines=True,
    )

    assert output == "Actual output"


def test_prepare_agent_output_suppresses_background_input_line() -> None:
    output = prepare_agent_output(
        "\x1b[48;5;236mFind and fix a bug in @filename\x1b[0m\n"
        "Actual output\n",
        suppress_trace_lines=True,
    )

    assert output == "Actual output"


def test_prepare_agent_output_keeps_foreground_colored_output() -> None:
    output = prepare_agent_output(
        "\x1b[38;5;48mActual output\x1b[0m\n",
        suppress_trace_lines=True,
    )

    assert output == "Actual output"


def test_prepare_agent_output_suppresses_working_status_lines() -> None:
    output = prepare_agent_output(
        "Working (14s • esc to interrupt)\n⠋ Reading files\nFinal answer\n",
        suppress_trace_lines=True,
    )

    assert output == "Final answer"


def test_prepare_agent_output_suppresses_codex_tool_status_lines() -> None:
    output = prepare_agent_output(
        "◦ Running uv run pytest -q\n"
        "└ /bin/bash -lc 'uv run pytest -q'\n"
        "153 passed in 2.60s\n",
        suppress_trace_lines=True,
    )

    assert output == "153 passed in 2.60s"


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


def test_codex_jsonl_maps_commentary_assistant_message_to_delta() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"assistant_message","channel":"commentary","message":"Working on it."}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.ASSISTANT_DELTA
    assert event.text == "Working on it."


def test_codex_jsonl_maps_tool_events() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"response_item","item":{"type":"function_call","name":"shell",'
        '"arguments":"{\\"cmd\\":\\"pytest\\"}","call_id":"call_1"}}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.TOOL_STARTED
    assert event.tool_name == "shell"
    assert event.tool_call_id == "call_1"


def test_codex_jsonl_maps_response_item_commentary_message_to_delta() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"response_item","item":{"type":"message","role":"assistant",'
        '"channel":"commentary","content":[{"type":"output_text","text":"Progress update"}]}}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.ASSISTANT_DELTA
    assert event.text == "Progress update"


def test_codex_jsonl_maps_tool_output() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"response_item","item":{"type":"function_call_output",'
        '"output":"tests passed","call_id":"call_1"}}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.TOOL_COMPLETED
    assert event.text == "tests passed"


def test_structured_final_message_prefers_full_output_file_text() -> None:
    full_text = "\n".join(f"bridge-test line {index}" for index in range(21, 81))

    assert _select_final_message_text("bridge-test line 80", full_text) == full_text


def test_structured_final_message_keeps_longer_streamed_text_without_output_file() -> None:
    streamed = "Line 1\nLine 2\nLine 3"

    assert _select_final_message_text(streamed, "Line 3") == streamed


def test_codex_jsonl_maps_approval_request() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"approval_requested","id":"approval_1","message":"Run tests?",'
        '"choices":[{"id":"approve","label":"Approve"},{"id":"reject","label":"Reject"}]}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.APPROVAL_REQUESTED
    assert event.approval_id == "approval_1"
    assert event.text == "Run tests?"


def test_codex_jsonl_maps_choice_request() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"choice_request","prompt":"Select model",'
        '"choices":[{"id":"1","label":"gpt-5.5","value":"gpt-5.5"},'
        '{"id":"2","label":"gpt-5","value":"gpt-5"}]}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.CHOICE_REQUEST
    assert event.text == "Select model"
    assert event.data["choices"] == [
        {"id": "1", "label": "gpt-5.5", "value": "gpt-5.5"},
        {"id": "2", "label": "gpt-5", "value": "gpt-5"},
    ]


def test_codex_jsonl_prefers_prompt_over_placeholder_text_for_choice_request() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"choice_request","text":"{{prompt}}","prompt":"Select model",'
        '"choices":[{"id":"1","label":"gpt-5.5","value":"gpt-5.5"}]}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.CHOICE_REQUEST
    assert event.text == "Select model"


def test_codex_jsonl_drops_placeholder_only_choice_request() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"choice_request","text":"› {{prompt}}\\n  Write the answer here",'
        '"choices":[{"id":"1","label":"Write the answer here","value":"1"}]}'
    ))

    assert not events


def test_codex_jsonl_drops_explain_codebase_placeholder_only_choice_request() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"choice_request","text":"Explain this codebase",'
        '"choices":[{"id":"1","label":"Write the answer here","value":"1"}]}'
    ))

    assert not events


def test_codex_jsonl_filters_prompt_placeholder_choice_labels() -> None:
    events = list(parse_codex_jsonl_line(
        '{"type":"choice_request","prompt":"Select model",'
        '"choices":["{{prompt}}",{"id":"2","label":"gpt-5","value":"gpt-5"}]}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.CHOICE_REQUEST
    assert event.data["choices"] == [{"id": "2", "label": "gpt-5", "value": "gpt-5"}]


# Claude Code adapter tests


def test_claude_auto_backend_uses_structured_without_tmux(monkeypatch) -> None:
    monkeypatch.setattr("cli_courier.agent.session.shutil.which", lambda name: None)

    assert resolve_agent_backend(ClaudeAdapter(), "auto") == "structured"


def test_claude_adapter_builds_structured_turn_command(tmp_path) -> None:
    command = ClaudeAdapter().build_structured_turn_command(
        ["claude"],
        prompt="hello",
        cwd=str(tmp_path),
        resume=False,
    )

    assert command == ["claude", "--print", "--output-format", "stream-json", "--verbose", "hello"]


def test_claude_adapter_builds_structured_command_with_model_flag(tmp_path) -> None:
    command = ClaudeAdapter().build_structured_turn_command(
        ["claude", "--model", "opus"],
        prompt="hello",
        cwd=str(tmp_path),
        resume=False,
    )

    assert command == [
        "claude",
        "--model",
        "opus",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "hello",
    ]


def test_claude_adapter_builds_structured_resume_command(tmp_path) -> None:
    command = ClaudeAdapter().build_structured_turn_command(
        ["claude"],
        prompt="follow up",
        cwd=str(tmp_path),
        resume=True,
    )

    assert "--continue" in command
    assert command[-1] == "follow up"


def test_claude_adapter_does_not_duplicate_flags(tmp_path) -> None:
    command = ClaudeAdapter().build_structured_turn_command(
        ["claude", "--print", "--output-format", "stream-json", "--verbose"],
        prompt="hello",
        cwd=str(tmp_path),
        resume=False,
    )

    assert command.count("--print") == 1
    assert command.count("--verbose") == 1
    assert command.count("--output-format") == 1


def test_claude_adapter_builds_interactive_resume_command() -> None:
    command = ClaudeAdapter().build_resume_command(["claude", "--model", "sonnet"])

    assert "--continue" in command
    assert "claude" in command


def test_claude_adapter_does_not_duplicate_continue_flag() -> None:
    command = ClaudeAdapter().build_resume_command(["claude", "--continue"])

    assert command.count("--continue") == 1


def test_adapters_strip_generated_resume_flags() -> None:
    assert CodexAdapter().strip_resume_command(["codex", "resume", "--last", "--model", "gpt-5"]) == [
        "codex",
        "--model",
        "gpt-5",
    ]
    assert ClaudeAdapter().strip_resume_command(["claude", "--model", "opus", "--continue"]) == [
        "claude",
        "--model",
        "opus",
    ]
    assert GeminiAdapter().strip_resume_command(["gemini", "--resume", "latest", "--yolo"]) == [
        "gemini",
        "--yolo",
    ]


# Claude Code JSONL parser tests


def test_claude_jsonl_maps_system_init_to_session_started() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"system","subtype":"init","session_id":"sess-1","model":"claude-sonnet-4-6",'
        '"cwd":"/tmp","permissionMode":"bypassPermissions"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.SESSION_STARTED
    assert event.session_id == "sess-1"
    assert "claude-sonnet-4-6" in event.text


def test_claude_jsonl_maps_system_status_to_debug_status() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"system","subtype":"status","status":"requesting","session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.STATUS
    assert event.is_debug is True


def test_claude_jsonl_maps_assistant_text_to_delta() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Hello!"}],'
        '"stop_reason":null},"session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.ASSISTANT_DELTA
    assert event.text == "Hello!"
    assert event.session_id == "sess-1"


def test_claude_jsonl_maps_assistant_tool_use_to_tool_started() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_01",'
        '"name":"Bash","input":{"command":"ls -la","description":"List files"}}],'
        '"stop_reason":null},"session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.TOOL_STARTED
    assert event.tool_name == "Bash"
    assert event.tool_call_id == "toolu_01"
    assert "ls -la" in event.text


def test_claude_jsonl_maps_assistant_thinking_to_reasoning() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"I should run ls.",'
        '"signature":"sig"}],"stop_reason":null},"session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.REASONING
    assert event.is_debug is True
    assert "I should run ls." in event.text


def test_claude_jsonl_prefers_tool_use_over_text_in_same_message() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","id":"toolu_01","name":"Edit","input":{"path":"/f","content":"x"}},'
        '{"type":"text","text":"Done."}'
        '],"stop_reason":null},"session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.TOOL_STARTED
    assert event.tool_name == "Edit"


def test_claude_jsonl_maps_user_tool_result_to_tool_completed() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_01",'
        '"type":"tool_result","content":"hello_world","is_error":false}]},"session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.TOOL_COMPLETED
    assert event.text == "hello_world"
    assert event.tool_call_id == "toolu_01"


def test_claude_jsonl_maps_user_tool_result_error_to_tool_failed() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_01",'
        '"type":"tool_result","content":"Permission denied","is_error":true}]},"session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.TOOL_FAILED
    assert event.text == "Permission denied"


def test_claude_jsonl_maps_tool_result_content_list() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_01",'
        '"type":"tool_result","content":[{"type":"text","text":"file1\\nfile2"}],"is_error":false}]}}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.TOOL_COMPLETED
    assert "file1" in event.text


def test_claude_jsonl_maps_result_success_to_final_message() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"result","subtype":"success","is_error":false,"result":"Done!",'
        '"session_id":"sess-1","stop_reason":"end_turn"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.FINAL_MESSAGE
    assert event.text == "Done!"
    assert event.session_id == "sess-1"


def test_claude_jsonl_maps_result_error_to_error_event() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"result","subtype":"error_max_turns","is_error":true,'
        '"result":"Max turns reached","session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.ERROR
    assert "Max turns reached" in event.text


def test_claude_jsonl_maps_rate_limit_event_to_debug_status() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"},"session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.STATUS
    assert event.is_debug is True


def test_claude_jsonl_maps_stream_event_to_debug_status() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"stream_event","event":{"type":"message_start"},"session_id":"sess-1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.STATUS
    assert event.is_debug is True


def test_claude_jsonl_propagates_session_id_from_top_level() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}],'
        '"stop_reason":null},"session_id":"uuid-123"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.session_id == "uuid-123"


def test_claude_jsonl_session_id_parameter_takes_precedence() -> None:
    events = list(parse_claude_jsonl_line(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}],'
        '"stop_reason":null},"session_id":"from-payload"}',
        session_id="from-param",
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.session_id == "from-param"


def test_claude_adapter_delegates_parse_jsonl_line() -> None:
    adapter = ClaudeAdapter()
    events = list(adapter.parse_jsonl_line(
        '{"type":"result","subtype":"success","is_error":false,"result":"OK","session_id":"s1"}'
    ))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.FINAL_MESSAGE
    assert event.text == "OK"


def test_codex_adapter_delegates_parse_jsonl_line() -> None:
    adapter = CodexAdapter()
    events = list(adapter.parse_jsonl_line('{"type":"agent_message","message":"Done."}'))
    event = events[0] if events else None

    assert event is not None
    assert event.kind == AgentEventKind.FINAL_MESSAGE
    assert event.text == "Done."
