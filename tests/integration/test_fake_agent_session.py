from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from cli_courier.agent.adapters import GenericCliAdapter
from cli_courier.agent.events import AgentEventKind
from cli_courier.agent.session import AgentSession


async def test_agent_session_round_trip(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures" / "fake_agent.py"
    session = AgentSession(
        adapter=GenericCliAdapter(),
        command=[sys.executable, str(script)],
        cwd=tmp_path,
        recent_output_max_chars=1000,
    )
    await session.start()
    try:
        ready = await asyncio.wait_for(session.output_queue.get(), timeout=2)
        assert ready.kind == AgentEventKind.ASSISTANT_DELTA
        await session.send_text("hello")
        output = await asyncio.wait_for(session.output_queue.get(), timeout=2)
        assert output.kind == AgentEventKind.ASSISTANT_DELTA
        assert "echo: hello" in output.text
    finally:
        await session.stop()
