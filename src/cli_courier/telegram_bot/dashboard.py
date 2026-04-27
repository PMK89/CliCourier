from __future__ import annotations

from dataclasses import dataclass

from cli_courier.agent.chunking import chunk_text

TELEGRAM_MESSAGE_LIMIT = 4096
DASHBOARD_LIMIT = 3900


@dataclass(frozen=True)
class DashboardSnapshot:
    agent_name: str
    state: str
    cwd: str
    current_phase: str
    last_event: str
    output_tail: str


def render_dashboard(
    snapshot: DashboardSnapshot,
    *,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> str:
    tail_limit = max(300, limit - 700)
    tail = _truncate_middle(snapshot.output_tail.strip(), tail_limit)
    lines = [
        f"Agent: {snapshot.agent_name}",
        f"State: {snapshot.state}",
        f"CWD: {snapshot.cwd}",
        f"Phase: {snapshot.current_phase or '-'}",
        f"Last: {_one_line(snapshot.last_event) or '-'}",
    ]
    if tail:
        lines.extend(["", "Recent:", tail])
    body = "\n".join(lines)
    if len(body) <= limit:
        return body
    return chunk_text(body, limit)[0]


def render_progress(
    lines: list[str],
    *,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> str:
    kept = [line for line in lines if line is not None]
    while kept:
        text = "\n".join(kept).strip()
        if text and len(text) <= limit:
            return text
        if len(kept) == 1:
            return kept[0][-limit:] if limit > 0 else ""
        kept = kept[1:]
    return ""


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...\n"
    keep = max(0, limit - len(marker))
    head = keep // 3
    tail = keep - head
    return f"{text[:head]}{marker}{text[-tail:]}"
