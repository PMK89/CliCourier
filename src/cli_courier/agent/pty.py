from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Iterable

import pexpect


DEFAULT_AGENT_ENV_KEYS = (
    "ALL_PROXY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "CLOUDSDK_CONFIG",
    "CODEX_HOME",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "NO_PROXY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT",
    "PATH",
    "SHELL",
    "SSH_AUTH_SOCK",
    "TERM",
    "TZ",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)


def build_agent_env(allowlist: Iterable[str] = ()) -> dict[str, str]:
    allowed = set(DEFAULT_AGENT_ENV_KEYS) | set(allowlist)
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["TERM"] = env.get("TERM", "xterm-256color")
    return env


class PtyAgentProcess:
    """Small async wrapper around a configured PTY child process."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        dimensions: tuple[int, int] = (40, 120),
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = command
        self.cwd = cwd
        self.env = env
        self.dimensions = dimensions
        self.output_queue: asyncio.Queue[str] = asyncio.Queue()
        self._child: pexpect.spawn | None = None
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._child is not None and self._child.isalive()

    async def start(self) -> None:
        if self.is_running:
            return
        rows, cols = self.dimensions
        self._child = pexpect.spawn(
            self.command[0],
            self.command[1:],
            cwd=str(self.cwd),
            env=self.env,
            dimensions=(rows, cols),
            encoding="utf-8",
            codec_errors="replace",
            echo=False,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        child = self._child
        if child is None:
            return
        if child.isalive():
            await asyncio.to_thread(child.terminate, True)
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self._child = None
        self._reader_task = None

    async def send_line(self, text: str, *, submit_sequence: str = "\r") -> None:
        if not self.is_running or self._child is None:
            raise RuntimeError("agent process is not running")
        await asyncio.to_thread(self._send_text_with_submit, text, submit_sequence)

    async def send_key(self, key: str) -> None:
        if not self.is_running or self._child is None:
            raise RuntimeError("agent process is not running")
        keys = {
            "Enter": "\r",
            "Up": "\x1b[A",
            "Down": "\x1b[B",
        }
        try:
            sequence = keys[key]
        except KeyError as exc:
            raise ValueError(f"unsupported key: {key}") from exc
        await asyncio.to_thread(self._child.send, sequence)

    def _send_text_with_submit(self, text: str, submit_sequence: str) -> None:
        assert self._child is not None
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self._child.send(normalized)
        self._child.send(submit_sequence)

    async def _read_loop(self) -> None:
        assert self._child is not None
        child = self._child
        while child.isalive():
            try:
                output = await asyncio.to_thread(child.read_nonblocking, 4096, 0.2)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
            if output:
                await self.output_queue.put(output)
