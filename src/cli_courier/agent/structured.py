from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from cli_courier.agent.adapters import AgentAdapter
from cli_courier.agent.events import AgentEvent, AgentEventKind
from cli_courier.agent.pty import build_agent_env


class StructuredAgentProcess:
    """Run an agent one turn at a time via JSONL-structured output."""

    def __init__(
        self,
        command: list[str],
        *,
        adapter: AgentAdapter,
        cwd: Path,
        env_allowlist: tuple[str, ...] = (),
        resume_last: bool = False,
    ) -> None:
        self.command = command
        self.adapter = adapter
        self.cwd = cwd
        self.env = build_agent_env(env_allowlist)
        self.output_queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._running = False
        self._has_started_turn = resume_last and adapter.capabilities.supports_resume
        self._turn_lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._turn_task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.output_queue.put(
            AgentEvent(
                kind=AgentEventKind.SESSION_STARTED,
                text=f"{self.adapter.display_name} structured stream ready.",
                session_id=self.adapter.id,
                data={"command": self.command, "cwd": str(self.cwd)},
            )
        )

    async def stop(self) -> None:
        self._running = False
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self._process = None

    async def send_line(self, text: str, *, submit_sequence: str = "\n") -> None:
        if not self._running:
            raise RuntimeError("agent process is not running")
        if self._turn_task is not None and not self._turn_task.done():
            raise RuntimeError("agent turn is still running")
        self._turn_task = asyncio.create_task(self._run_turn(text))

    async def send_approval(self, text: str) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            await self.send_line(text)
            return
        process.stdin.write((text.rstrip("\r\n") + "\n").encode())
        await process.stdin.drain()

    async def send_key(self, key: str) -> None:
        raise RuntimeError("structured agent mode does not expose terminal key menus")

    async def _run_turn(self, prompt: str) -> None:
        async with self._turn_lock:
            resume = self._has_started_turn and self.adapter.capabilities.supports_resume
            output_path = _temporary_output_path()
            command = self.adapter.build_structured_turn_command(
                self.command,
                prompt=prompt,
                cwd=str(self.cwd),
                resume=resume,
                output_last_message_path=str(output_path),
            )
            await self.output_queue.put(
                AgentEvent(
                    kind=AgentEventKind.TURN_STARTED,
                    text="Turn started.",
                    session_id=self.adapter.id,
                    data={"command": command, "resume": resume},
                )
            )
            final_message_text = ""
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self.cwd),
                    env=self.env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert self._process.stdout is not None
                stderr_task = asyncio.create_task(self._read_stderr(self._process))
                async for raw_line in self._process.stdout:
                    events = self.adapter.parse_jsonl_line(
                        raw_line.decode("utf-8", errors="replace"),
                        session_id=self.adapter.id,
                    )
                    for event in events:
                        if event.kind == AgentEventKind.FINAL_MESSAGE:
                            final_message_text = _select_final_message_text(final_message_text, event.text)
                            continue
                        if event.kind in {AgentEventKind.TURN_STARTED, AgentEventKind.TURN_COMPLETED}:
                            continue
                        await self.output_queue.put(event)
                returncode = await self._process.wait()
                await stderr_task
                final_text = _select_final_message_text(final_message_text, _read_output_file(output_path))
                if final_text:
                    await self.output_queue.put(
                        AgentEvent(
                            kind=AgentEventKind.FINAL_MESSAGE,
                            text=final_text,
                            session_id=self.adapter.id,
                        )
                    )
                if returncode == 0:
                    await self.output_queue.put(
                        AgentEvent(
                            kind=AgentEventKind.TURN_COMPLETED,
                            text="Turn completed.",
                            session_id=self.adapter.id,
                            data={"returncode": returncode},
                        )
                    )
                    self._has_started_turn = True
                else:
                    await self.output_queue.put(
                        AgentEvent(
                            kind=AgentEventKind.TURN_FAILED,
                            text=f"{self.adapter.display_name} exited with status {returncode}.",
                            session_id=self.adapter.id,
                            data={"returncode": returncode},
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - surfaced through event stream
                await self.output_queue.put(
                    AgentEvent(
                        kind=AgentEventKind.ERROR,
                        text=f"{self.adapter.display_name} structured stream failed: {exc}",
                        session_id=self.adapter.id,
                    )
                )
            finally:
                output_path.unlink(missing_ok=True)
                self._process = None

    async def _read_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        async for raw_line in process.stderr:
            text = raw_line.decode("utf-8", errors="replace").strip()
            if text:
                await self.output_queue.put(
                    AgentEvent(
                        kind=AgentEventKind.STATUS,
                        text=text,
                        session_id=self.adapter.id,
                        is_debug=True,
                    )
                )


StructuredCodexProcess = StructuredAgentProcess


def _temporary_output_path() -> Path:
    handle = tempfile.NamedTemporaryFile(prefix="clicourier-agent-final-", delete=False)
    handle.close()
    return Path(handle.name)


def _read_output_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return ""


def _select_final_message_text(current: str, candidate: str) -> str:
    current = current.strip()
    candidate = candidate.strip()
    if not current:
        return candidate
    if not candidate:
        return current
    if candidate == current:
        return candidate
    if len(candidate) > len(current) and (candidate.endswith(current) or current in candidate):
        return candidate
    if len(current) > len(candidate) and (current.endswith(candidate) or candidate in current):
        return current
    return candidate
