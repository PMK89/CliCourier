from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable


_SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


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
        return self._has_session()

    async def start(self) -> None:
        if not tmux_available():
            raise RuntimeError("tmux is required for AGENT_TERMINAL_BACKEND=tmux")
        if not self._has_session():
            await asyncio.to_thread(self._new_session)
            self._created_session = True
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
            await asyncio.to_thread(
                subprocess.run,
                ["tmux", "kill-session", "-t", self.session_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

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

    def _shell_command(self) -> str:
        assignments = " ".join(_shell_assignment(key, value) for key, value in self.env.items())
        command = shlex.join(self.command)
        if assignments:
            return f"env -i {assignments} {command}"
        return command

    def _send_text_with_submit(self, text: str, submit_sequence: str) -> None:
        normalized = " ".join(text.replace("\r\n", "\n").replace("\r", "\n").splitlines())
        if normalized:
            subprocess.run(
                ["tmux", "set-buffer", normalized],
                check=True,
            )
            subprocess.run(
                ["tmux", "paste-buffer", "-d", "-p", "-t", self.target],
                check=True,
            )
            if self.submit_delay_seconds > 0:
                time.sleep(self.submit_delay_seconds)
        key = submit_sequence if submit_sequence and len(submit_sequence) > 1 else "Enter"
        subprocess.run(["tmux", "send-keys", "-t", self.target, key], check=True)

    async def _read_loop(self) -> None:
        while self._has_session():
            snapshot = await asyncio.to_thread(self._capture_snapshot)
            if snapshot and snapshot != self._last_snapshot:
                await self.output_queue.put(snapshot)
                self._last_snapshot = snapshot
            await asyncio.sleep(self.poll_interval_seconds)

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

    def _has_session(self) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0


def _shell_assignment(key: str, value: str) -> str:
    return f"{key}={shlex.quote(value)}"
