from __future__ import annotations

import re

from cli_courier.security.terminal import sanitize_terminal_text


TRACE_LINE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*(thinking|reasoning|working|planning|analyzing)\b.*$",
        r"^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+.*$",
        r"^\s*(tool call|tool_call|function call|calling tool|running tool)\b.*$",
        r"^\s*(executing|reading|searching|editing|applying patch|observing)\b.*$",
        r"^\s*(bash|shell|python|apply_patch|functions\.[a-z_]+|web\.[a-z_]+)\s*(\(|:|$).*$",
        r"^\s*[•◦]\s*(running|ran|read|opened|searched|updated|patched|executing)\b.*$",
        r"^\s*[└╰]\s+.*$",
        r"^\s*(tokens|context window|model:|cwd:|sandbox:)\b.*$",
        r"^\s*(directory|tip):.*$",
        r"^\s*│\s*(model:|directory:).*$",
        r"^\s*(?:gpt-|claude|gemini).*$",
        r"^\s*.*\bOpenAI Codex\b.*$",
        r"^\s*.*\[features\]\..*deprecated.*$",
        r"^\s*⚠.*$",
        r"^\s*[╭╮╰╯│─_> ]{4,}$",
        r"^\s*\[Pasted Content \d+ chars\]\s*$",
        r"^\s*.*\besc\s+to\s+interrupt\b.*$",
        r"^\s*[-*]\s*(ran|read|opened|searched|updated|patched)\b.*$",
    )
)

CODEX_PROMPT_ECHO_RE = re.compile(
    r"^\s*›(?:\S.*(?:\bgpt-[\w.-]+|\bclaude\b|\bgemini\b|·\s*~)|\S{20,}.*)$",
    re.IGNORECASE,
)
CODEX_INPUT_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:[›>]\s*)?(?:[\u2580-\u259f\u25a0-\u25ff]\s*)?.*@filename\s*$",
    re.IGNORECASE,
)
CODEX_INPUT_PLACEHOLDER_TEXTS = {"explain this codebase"}
CODEX_LEADING_MARKER_RE = re.compile(r"^\s*›\s+")
SGR_SEQUENCE_RE = re.compile(r"\x1b\[([0-9;:]*)m")

IN_PROGRESS_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bworking\s*\(",
        r"\besc\s+to\s+interrupt\b",
        r"^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+",
    )
)


def prepare_agent_output(text: str, *, suppress_trace_lines: bool = True) -> str:
    cleaned = _remove_terminal_rewrite_noise(text)
    cleaned = sanitize_terminal_text(cleaned)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    lines = [line for line in lines if not looks_like_codex_input_placeholder(line)]
    lines = [_normalize_codex_output_line(line) for line in lines]
    lines = _trim_blank_edges(lines)
    if not suppress_trace_lines:
        return "\n".join(lines).strip()

    filtered = [line for line in lines if not _looks_like_trace_line(line)]
    filtered = _trim_blank_edges(filtered)
    return "\n".join(filtered).strip()


def agent_output_in_progress(text: str) -> bool:
    cleaned = sanitize_terminal_text(text)
    lines = [line for line in cleaned.splitlines() if line.strip()]
    tail = lines[-1] if lines else cleaned
    return any(pattern.search(tail) for pattern in IN_PROGRESS_PATTERNS)


def looks_like_codex_input_placeholder(line: str) -> bool:
    stripped = line.strip()
    if CODEX_INPUT_PLACEHOLDER_RE.match(stripped):
        return True
    normalized = _normalize_codex_input_placeholder_line(stripped).lower()
    return normalized in CODEX_INPUT_PLACEHOLDER_TEXTS


def _normalize_codex_input_placeholder_line(line: str) -> str:
    cleaned = line.strip().strip("│|").strip()
    cleaned = re.sub(r"^(?:[›>]\s*)?(?:[\u2580-\u259f\u25a0-\u25ff]\s*)?", "", cleaned).strip()
    cleaned = cleaned.strip("│|").strip()
    return re.sub(r"\s+", " ", cleaned)


def _looks_like_trace_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if CODEX_PROMPT_ECHO_RE.match(stripped):
        return True
    return any(pattern.match(stripped) for pattern in TRACE_LINE_PATTERNS)


def _normalize_codex_output_line(line: str) -> str:
    if CODEX_PROMPT_ECHO_RE.match(line.strip()):
        return line
    return CODEX_LEADING_MARKER_RE.sub("", line, count=1)


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
        if _line_has_background_sgr(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _line_has_background_sgr(line: str) -> bool:
    for match in SGR_SEQUENCE_RE.finditer(line):
        params = match.group(1).replace(":", ";")
        codes = [part for part in params.split(";") if part]
        index = 0
        while index < len(codes):
            code = codes[index]
            try:
                value = int(code)
            except ValueError:
                index += 1
                continue
            if value == 7 or 40 <= value <= 49 or value == 48 or 100 <= value <= 107:
                return True
            if value == 38 and index + 1 < len(codes):
                mode = codes[index + 1]
                if mode == "5":
                    index += 3
                    continue
                if mode == "2":
                    index += 5
                    continue
            index += 1
    return False
