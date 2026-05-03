from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable


def tmux_session_has_attached_client(session_name: str) -> bool:
    """Return True if any terminal is currently attached to the tmux session."""
    result = subprocess.run(
        ["tmux", "list-clients", "-t", session_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


_SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SEND_TEXT_CHUNK_CHARS = 700
_LONG_TEXT_DOUBLE_SUBMIT_CHARS = 1000
_VISIBLE_INPUT_TAIL_CHARS = 80
_VISIBLE_INPUT_WAIT_SECONDS = 3.0
_VISIBLE_INPUT_POLL_SECONDS = 0.05
_AGENT_STATE_OPTION = "@clicourier_agent_state"
_AGENT_STATE_RUNNING = "running"
_AGENT_STATE_EXITED = "exited"
_SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def safe_tmux_session_name(value: str | None, *, workspace: Path) -> str:
    raw = value or f"clicourier-{workspace.name or 'workspace'}"
    normalized = _SESSION_SAFE_RE.sub("-", raw).strip("-")
    return normalized or "clicourier"


class TmuxAgentProcess:
    """Run an agent in tmux so the same CLI is visible and remotely controllable."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        session_name: str | None = None,
        history_lines: int = 300,
        poll_interval_seconds: float = 0.5,
        submit_delay_seconds: float = 0.05,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = command
        self.cwd = cwd
        self.env = dict(env or {})
        self.env["TERM"] = "screen-256color"
        self.session_name = safe_tmux_session_name(session_name, workspace=cwd)
        self.history_lines = history_lines
        self.poll_interval_seconds = poll_interval_seconds
        self.submit_delay_seconds = submit_delay_seconds
        self.output_queue: asyncio.Queue[str] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._last_snapshot = ""
        self._created_session = False

    @property
    def target(self) -> str:
        return f"{self.session_name}:0.0"

    @property
    def is_running(self) -> bool:
        return tmux_session_has_running_agent(self.session_name)

    async def start(self) -> None:
        if not tmux_available():
            raise RuntimeError("tmux is required for AGENT_TERMINAL_BACKEND=tmux")
        session_exists = self._has_session()
        if session_exists and not tmux_session_has_running_agent(self.session_name):
            await asyncio.to_thread(self._kill_session)
            session_exists = False
        if not session_exists:
            await asyncio.to_thread(self._new_session)
            await asyncio.to_thread(self._wait_for_agent_state)
            self._created_session = True
            self.initial_snapshot: str | None = None
        else:
            # Reattaching to a running session: capture existing scrollback so
            # the reader loop and AgentSession can skip it and only forward new output.
            initial = await asyncio.to_thread(self._capture_snapshot)
            self.initial_snapshot = initial
            self._last_snapshot = initial
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._created_session and self._has_session():
            if not tmux_session_has_attached_client(self.session_name):
                await asyncio.to_thread(self._kill_session)

    async def send_line(self, text: str, *, submit_sequence: str = "Enter") -> None:
        if not self.is_running:
            raise RuntimeError("agent process is not running")
        await asyncio.to_thread(self._send_text_with_submit, text, submit_sequence)

    async def send_key(self, key: str) -> None:
        if not self.is_running:
            raise RuntimeError("agent process is not running")
        if key not in {"Enter", "Up", "Down"}:
            raise ValueError(f"unsupported key: {key}")
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "send-keys", "-t", self.target, key],
            check=True,
        )

    def _new_session(self) -> None:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.session_name,
                "-c",
                str(self.cwd),
                self._shell_command(),
            ],
            check=True,
        )

    def _kill_session(self) -> None:
        subprocess.run(
            ["tmux", "kill-session", "-t", self.session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def _shell_command(self) -> str:
        assignments = " ".join(_shell_assignment(key, value) for key, value in self.env.items())
        command = shlex.join(self.command)
        keep_open = (
            "status=$?; "
            f"{_tmux_set_agent_state_command(self.session_name, _AGENT_STATE_EXITED)}; "
            "printf '\\n[CliCourier] agent exited with status %s\\n' \"$status\"; "
            "exec \"${SHELL:-/bin/sh}\""
        )
        mark_running = _tmux_set_agent_state_command(self.session_name, _AGENT_STATE_RUNNING)
        if assignments:
            return f"{mark_running}; env -i {assignments} {command}; {keep_open}"
        return f"{mark_running}; {command}; {keep_open}"

    def _send_text_with_submit(self, text: str, submit_sequence: str) -> None:
        normalized = " ".join(text.replace("\r\n", "\n").replace("\r", "\n").splitlines())
        if normalized:
            # Use literal key injection and wait for the TUI to render the tail before
            # submit. Ink/React TUIs can render pasted text after the key queue has
            # advanced, which leaves text visible but unsubmitted if Enter arrives early.
            chunks = list(_text_chunks(normalized, _SEND_TEXT_CHUNK_CHARS))
            for index, chunk in enumerate(chunks):
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.target, "-l", chunk],
                    check=True,
                )
                if index < len(chunks) - 1:
                    time.sleep(0.08)
            delay = self._submit_delay_for_text(normalized)
            if delay > 0:
                time.sleep(delay)
            self._wait_for_visible_input_tail(normalized)
        submit = _tmux_submit_sequence(submit_sequence)
        self._send_submit(submit)
        if len(normalized) >= _LONG_TEXT_DOUBLE_SUBMIT_CHARS and submit in {"\r", "\n", "Enter", "C-m"}:
            time.sleep(0.35)
            self._send_submit(submit)

    def _submit_delay_for_text(self, text: str) -> float:
        if not text:
            return 0
        scaled_delay = min(2.0, 0.15 + (len(text) / 4000))
        return max(self.submit_delay_seconds, scaled_delay)

    async def _read_loop(self) -> None:
        while self.is_running:
            snapshot = await asyncio.to_thread(self._capture_snapshot)
            if snapshot and snapshot != self._last_snapshot:
                await self.output_queue.put(snapshot)
                self._last_snapshot = snapshot
            await asyncio.sleep(self.poll_interval_seconds)

    def _wait_for_agent_state(self) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            state = _tmux_agent_state(self.session_name)
            if state == _AGENT_STATE_EXITED:
                return
            if state == _AGENT_STATE_RUNNING and not _tmux_pane_is_idle_shell(self.session_name):
                return
            if not self._has_live_pane():
                return
            time.sleep(0.05)

    def _capture_snapshot(self) -> str:
        result = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-t",
                self.target,
                "-p",
                "-J",
                "-S",
                f"-{self.history_lines}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return result.stdout.rstrip()

    def _wait_for_visible_input_tail(self, text: str) -> None:
        tail = text[-_VISIBLE_INPUT_TAIL_CHARS:]
        if not tail:
            return
        deadline = time.monotonic() + _VISIBLE_INPUT_WAIT_SECONDS
        while time.monotonic() < deadline:
            if tail in self._capture_snapshot():
                return
            time.sleep(_VISIBLE_INPUT_POLL_SECONDS)

    def _send_submit(self, submit: str) -> None:
        if submit in {"\r", "\n"}:
            subprocess.run(["tmux", "send-keys", "-t", self.target, "-l", submit], check=True)
            return
        subprocess.run(["tmux", "send-keys", "-t", self.target, submit], check=True)

    def _has_session(self) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def _has_live_pane(self) -> bool:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", self.session_name, "-F", "#{pane_dead}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        return any(line.strip() == "0" for line in result.stdout.splitlines())


def _shell_assignment(key: str, value: str) -> str:
    return f"{key}={shlex.quote(value)}"


def _text_chunks(text: str, size: int) -> Iterable[str]:
    for start in range(0, len(text), size):
        yield text[start : start + size]


def _tmux_submit_sequence(submit_sequence: str) -> str:
    if submit_sequence in {"", "\r", "\n"}:
        return "\r"
    return submit_sequence


def tmux_session_has_running_agent(session_name: str) -> bool:
    return (
        _tmux_has_live_pane(session_name)
        and _tmux_agent_state(session_name) == _AGENT_STATE_RUNNING
        and not _tmux_pane_is_idle_shell(session_name)
    )


def _tmux_has_live_pane(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_dead}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    return any(line.strip() == "0" for line in result.stdout.splitlines())


def _tmux_agent_state(session_name: str) -> str:
    result = subprocess.run(
        ["tmux", "show-options", "-qv", "-t", session_name, _AGENT_STATE_OPTION],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _tmux_pane_is_idle_shell(session_name: str) -> bool:
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            f"{session_name}:0.0",
            "#{pane_current_command}\t#{pane_pid}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    command, _, pid = result.stdout.strip().partition("\t")
    if Path(command).name not in _SHELL_COMMANDS:
        return False
    return not _process_has_child(pid)


def _process_has_child(pid: str) -> bool:
    if not pid.isdigit():
        return False
    result = subprocess.run(
        ["ps", "-eo", "ppid=,pid="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == pid:
            return True
    return False


def _tmux_set_agent_state_command(session_name: str, state: str) -> str:
    return (
        "tmux set-option -q -t "
        f"{shlex.quote(session_name)} {_AGENT_STATE_OPTION} {shlex.quote(state)} "
        ">/dev/null 2>&1 || true"
    )
