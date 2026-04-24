from __future__ import annotations


class OutputRingBuffer:
    def __init__(self, max_chars: int) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars
        self._text = ""

    def append(self, text: str) -> None:
        if not text:
            return
        self._text = (self._text + text)[-self.max_chars :]

    def recent(self, max_chars: int | None = None) -> str:
        if max_chars is None:
            return self._text
        return self._text[-max_chars:]

    def clear(self) -> None:
        self._text = ""


def chunk_text(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not text:
        return []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind(" ", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n ")
    if remaining:
        chunks.append(remaining)
    return chunks

