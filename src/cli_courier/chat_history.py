"""Persistent per-chat JSONL message log.

Each line is a JSON object: {"ts": ISO8601, "role": "user"|"agent", "text": "..."}.
The file persists across sessions and Telegram deletions. Agents can read it directly
to recover conversation context without it being fully loaded into the system prompt.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


_MAX_LINES = 2000


class ChatHistory:
    """Append-only JSONL log for one Telegram chat."""

    def __init__(self, path: Path, *, max_lines: int = _MAX_LINES) -> None:
        self._path = path
        self._max_lines = max_lines

    @property
    def path(self) -> Path:
        return self._path

    def append(self, *, role: str, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        entry = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "role": role,
                "text": stripped,
            },
            ensure_ascii=False,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
        self._maybe_rotate()

    def tail(self, n: int) -> list[dict]:
        """Return the last *n* log entries as parsed dicts."""
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        result: list[dict] = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return result

    def _maybe_rotate(self) -> None:
        """Trim to max_lines by dropping oldest entries."""
        if not self._path.exists():
            return
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= self._max_lines:
            return
        self._path.write_text("\n".join(lines[-self._max_lines :]) + "\n", encoding="utf-8")
