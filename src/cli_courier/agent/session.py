from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import shutil

from cli_courier.agent.adapters import AgentAdapter
from cli_courier.agent.chunking import OutputRingBuffer
from cli_courier.agent.events import DEBUG_EVENT_KINDS, AgentEvent, AgentEventKind
from cli_courier.agent.pty import PtyAgentProcess, build_agent_env
from cli_courier.agent.structured import StructuredCodexProcess
from cli_courier.agent.tmux import TmuxAgentProcess


@dataclass(frozen=True)
class AgentRuntimeStatus:
    adapter_id: str
    adapter_name: str
    command: tuple[str, ...]
    running: bool
    mode: str
    state: str
    current_tool: str | None
    last_event: str


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
        self.output_queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._buffer = OutputRingBuffer(recent_output_max_chars)
        self._visible_buffer = OutputRingBuffer(recent_output_max_chars)
        backend = resolve_agent_backend(adapter, terminal_backend)
        self.backend = backend
        self.replaces_output_snapshots = False
        if backend == "tmux":
            env = build_agent_env(env_allowlist)
            self._process = TmuxAgentProcess(
                command,
                cwd=cwd,
                env=env,
                session_name=tmux_session_name,
                history_lines=tmux_history_lines,
            )
        elif backend == "structured":
            self._process = StructuredCodexProcess(
                command,
                adapter=adapter,
                cwd=cwd,
                env_allowlist=env_allowlist,
            )
        else:
            env = build_agent_env(env_allowlist)
            self._process = PtyAgentProcess(
                command,
                cwd=cwd,
                env=env,
            )
        self._consumer_task: asyncio.Task[None] | None = None
        self.state = "starting"
        self.current_tool: str | None = None
        self.last_event: AgentEvent | None = None

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

    async def send_approval(self, text: str) -> None:
        send_approval = getattr(self._process, "send_approval", None)
        if send_approval is not None:
            await send_approval(text)
            return
        await self._process.send_line(text, submit_sequence=self.adapter.submit_sequence)

    async def send_key(self, key: str) -> None:
        await self._process.send_key(key)

    def recent_output(self, max_chars: int | None = None) -> str:
        return self._buffer.recent(max_chars)

    def recent_visible_output(self, max_chars: int | None = None) -> str:
        return self._visible_buffer.recent(max_chars)

    def status(self) -> AgentRuntimeStatus:
        return AgentRuntimeStatus(
            adapter_id=self.adapter.id,
            adapter_name=self.adapter.display_name,
            command=tuple(self.command),
            running=self.is_running,
            mode=self.backend,
            state=self.state,
            current_tool=self.current_tool,
            last_event=self.last_event.display_text() if self.last_event else "",
        )

    async def _consume_output(self) -> None:
        while True:
            raw = await self._process.output_queue.get()
            if isinstance(raw, AgentEvent):
                event = raw
            else:
                normalized = self.adapter.normalize_output(raw)
                event = AgentEvent(
                    kind=AgentEventKind.ASSISTANT_DELTA,
                    text=normalized,
                    session_id=self.adapter.id,
                    is_debug=False,
                )
            self._record_event(event)
            await self.output_queue.put(event)

    def _record_event(self, event: AgentEvent) -> None:
        self.last_event = event
        if event.text:
            self._buffer.append(event.text)
            if event.kind not in DEBUG_EVENT_KINDS and not event.is_debug:
                self._visible_buffer.append(event.text)
        if event.kind == AgentEventKind.SESSION_STARTED:
            self.state = "idle"
        elif event.kind == AgentEventKind.TURN_STARTED:
            self.state = "running"
            self.current_tool = None
        elif event.kind == AgentEventKind.APPROVAL_REQUESTED:
            self.state = "approval_required"
        elif event.kind in {AgentEventKind.TURN_COMPLETED, AgentEventKind.FINAL_MESSAGE}:
            self.state = "idle"
            self.current_tool = None
        elif event.kind in {AgentEventKind.TURN_FAILED, AgentEventKind.ERROR}:
            self.state = "failed"
        elif event.kind == AgentEventKind.TOOL_STARTED:
            self.current_tool = event.tool_name or event.display_text()
        elif event.kind in {AgentEventKind.TOOL_COMPLETED, AgentEventKind.TOOL_FAILED}:
            self.current_tool = None


def resolve_terminal_backend(value: str) -> str:
    if value == "auto":
        return "tmux" if shutil.which("tmux") else "pty"
    if value in {"pty", "tmux"}:
        return value
    raise ValueError(f"unknown agent terminal backend: {value}")


def resolve_agent_backend(adapter: AgentAdapter, terminal_backend: str) -> str:
    if terminal_backend == "auto" and adapter.capabilities.supports_structured_stream:
        return "structured"
    return resolve_terminal_backend(terminal_backend)
