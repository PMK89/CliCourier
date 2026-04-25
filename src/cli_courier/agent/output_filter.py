from __future__ import annotations

import re

from cli_courier.security.terminal import sanitize_terminal_text


TRACE_LINE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*(thinking|reasoning|working|planning|analyzing)\b\.?:?\s*$",
        r"^\s*(tool call|tool_call|function call|calling tool|running tool)\b.*$",
        r"^\s*(executing|reading|searching|editing|applying patch|observing)\b.*$",
        r"^\s*(bash|shell|python|apply_patch|functions\.[a-z_]+|web\.[a-z_]+)\s*(\(|:|$).*$",
        r"^\s*(tokens|context window|model:|cwd:|sandbox:)\b.*$",
        r"^\s*›.*$",
        r"^\s*[-*]\s*(ran|read|opened|searched|updated|patched)\b.*$",
    )
)


def prepare_agent_output(text: str, *, suppress_trace_lines: bool = True) -> str:
    cleaned = sanitize_terminal_text(text)
    cleaned = _remove_terminal_rewrite_noise(cleaned)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    lines = _trim_blank_edges(lines)
    if not suppress_trace_lines:
        return "\n".join(lines).strip()

    filtered = [line for line in lines if not _looks_like_trace_line(line)]
    filtered = _trim_blank_edges(filtered)
    return "\n".join(filtered).strip()


def _looks_like_trace_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return any(pattern.match(stripped) for pattern in TRACE_LINE_PATTERNS)


def _trim_blank_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _remove_terminal_rewrite_noise(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if "\b" in line:
            continue
        lines.append(line)
    return "\n".join(lines)
