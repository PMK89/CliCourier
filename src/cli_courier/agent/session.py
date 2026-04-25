from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import shutil

from cli_courier.agent.adapters import AgentAdapter
from cli_courier.agent.chunking import OutputRingBuffer
from cli_courier.agent.pty import PtyAgentProcess, build_agent_env
from cli_courier.agent.tmux import TmuxAgentProcess


@dataclass(frozen=True)
class AgentRuntimeStatus:
    adapter_id: str
    adapter_name: str
    command: tuple[str, ...]
    running: bool


class AgentSession:
    def __init__(
        self,
        *,
        adapter: AgentAdapter,
        command: list[str],
        cwd: Path,
        recent_output_max_chars: int,
        env_allowlist: tuple[str, ...] = (),
        terminal_backend: str = "auto",
        tmux_session_name: str | None = None,
        tmux_history_lines: int = 300,
    ) -> None:
        self.adapter = adapter
        self.command = command
        self.cwd = cwd
        self.output_queue: asyncio.Queue[str] = asyncio.Queue()
        self._buffer = OutputRingBuffer(recent_output_max_chars)
        env = build_agent_env(env_allowlist)
        backend = resolve_terminal_backend(terminal_backend)
        if backend == "tmux":
            self._process = TmuxAgentProcess(
                command,
                cwd=cwd,
                env=env,
                session_name=tmux_session_name,
                history_lines=tmux_history_lines,
            )
        else:
            self._process = PtyAgentProcess(
                command,
                cwd=cwd,
                env=env,
            )
        self._consumer_task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._process.is_running

    async def start(self) -> None:
        await self._process.start()
        self._consumer_task = asyncio.create_task(self._consume_output())

    async def stop(self) -> None:
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None
        await self._process.stop()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def send_text(self, text: str) -> None:
        await self._process.send_line(text, submit_sequence=self.adapter.submit_sequence)

    def recent_output(self, max_chars: int | None = None) -> str:
        return self._buffer.recent(max_chars)

    def status(self) -> AgentRuntimeStatus:
        return AgentRuntimeStatus(
            adapter_id=self.adapter.id,
            adapter_name=self.adapter.display_name,
            command=tuple(self.command),
            running=self.is_running,
        )

    async def _consume_output(self) -> None:
        while True:
            raw = await self._process.output_queue.get()
            normalized = self.adapter.normalize_output(raw)
            self._buffer.append(normalized)
            await self.output_queue.put(normalized)


def resolve_terminal_backend(value: str) -> str:
    if value == "auto":
        return "tmux" if shutil.which("tmux") else "pty"
    if value in {"pty", "tmux"}:
        return value
    raise ValueError(f"unknown agent terminal backend: {value}")
