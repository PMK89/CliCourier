from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from cli_courier.agent.adapters import AgentAdapter
from cli_courier.agent.output_filter import looks_like_codex_input_placeholder
from cli_courier.security.terminal import safe_excerpt, sanitize_terminal_text
from cli_courier.state import PendingApproval, new_nonce

ApprovalDecision = Literal["approve", "reject"]

_APPROVE_WORDS = {
    "y",
    "yes",
    "yep",
    "yup",
    "yeah",
    "sure",
    "approve",
    "approved",
    "ok",
    "okay",
    "alright",
    "proceed",
    "continue",
    "allow",
    "accept",
    "accepted",
    "confirm",
    "confirmed",
    "go ahead",
    "go for it",
    "do it",
    "fine",
    "sounds good",
    "agreed",
    "grant",
    "granted",
    "thumbs up",
    "thumbsup",
    "heart",
}
_REJECT_WORDS = {
    "n",
    "no",
    "nope",
    "nah",
    "reject",
    "rejected",
    "cancel",
    "stop",
    "deny",
    "denied",
    "abort",
    "refuse",
    "refused",
    "decline",
    "declined",
    "no way",
    "skip",
    "never",
    "thumbs down",
    "thumbsdown",
}
_APPROVE_EMOJI = {"👍", "❤", "♥"}
_REJECT_EMOJI = {"👎"}
_WORD_RE = re.compile(r"^[\s.!?,;:]+|[\s.!?,;:]+$")
_SKIN_TONE_RE = re.compile("[\U0001f3fb-\U0001f3ff]")
AUTO_APPROVAL_RE = re.compile(
    r"(?:automatic approval review approved|auto-reviewer approved|auto[- ]approved)",
    re.IGNORECASE,
)


def normalize_decision_text(text: str) -> str:
    normalized = text.replace("\ufe0f", "")
    normalized = _SKIN_TONE_RE.sub("", normalized)
    return _WORD_RE.sub("", normalized.strip().lower())


def interpret_approval_text(text: str) -> ApprovalDecision | None:
    normalized = normalize_decision_text(text)
    if normalized in _APPROVE_EMOJI:
        return "approve"
    if normalized in _REJECT_EMOJI:
        return "reject"
    if normalized in _APPROVE_WORDS:
        return "approve"
    if normalized in _REJECT_WORDS:
        return "reject"
    return None


def is_approval_like(text: str) -> bool:
    return interpret_approval_text(text) is not None


def has_auto_approval_marker(text: str) -> bool:
    return AUTO_APPROVAL_RE.search(sanitize_terminal_text(text)) is not None


def detect_pending_approval(
    recent_output: str,
    adapter: AgentAdapter,
    *,
    ttl: timedelta = timedelta(minutes=10),
    now: datetime | None = None,
    message_id: int | None = None,
) -> PendingApproval | None:
    cleaned = sanitize_terminal_text(recent_output)
    if not cleaned.strip():
        return None

    source_tail = _after_last_auto_approval(cleaned[-8000:])
    tail = _approval_scan_text(source_tail)[-4000:]
    if not tail.strip():
        return None
    for pattern in adapter.approval_patterns:
        match = pattern.search(tail)
        if match is None:
            continue
        if _is_auto_approval_context(tail, match):
            return None
        detected_at = now or datetime.now(UTC)
        return PendingApproval(
            prompt_excerpt=safe_excerpt(_approval_prompt_excerpt(tail, match), 900),
            detected_at=detected_at,
            adapter_id=adapter.id,
            nonce=new_nonce(),
            expires_at=detected_at + ttl,
            message_id=message_id,
        )
    return None


def _after_last_auto_approval(text: str) -> str:
    last_match: re.Match[str] | None = None
    for match in AUTO_APPROVAL_RE.finditer(text):
        last_match = match
    if last_match is None:
        return text
    return text[last_match.end() :]


def _approval_scan_text(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if AUTO_APPROVAL_RE.search(stripped):
            continue
        if _looks_like_terminal_noise(stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _looks_like_terminal_noise(line: str) -> bool:
    if looks_like_codex_input_placeholder(line):
        return True
    if line.startswith("›"):
        return True
    if "esc to interrupt" in line.lower():
        return True
    if re.match(r"^(?:gpt-|claude-|gemini-)\S*\s+", line, re.IGNORECASE):
        return True
    if re.match(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+", line):
        return True
    if re.match(r"^[MADRCU?!]{1,2}\s+", line):
        return True
    if re.match(
        r"^[•◦]\s*(running|ran|read|opened|searched|updated|patched|executing)\b",
        line,
        re.IGNORECASE,
    ):
        return True
    if line.startswith(("└", "╰")):
        return True
    return False


def _is_auto_approval_context(text: str, match: re.Match[str]) -> bool:
    context_start = max(0, match.start() - 160)
    context_end = min(len(text), match.end() + 160)
    return AUTO_APPROVAL_RE.search(text[context_start:context_end]) is not None


def _approval_prompt_excerpt(text: str, match: re.Match[str]) -> str:
    line_ranges: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines():
        start = offset
        end = start + len(line)
        line_ranges.append((start, end, line))
        offset = end + 1

    match_index = 0
    for index, (start, end, _line) in enumerate(line_ranges):
        if start <= match.start() <= end or start <= match.end() <= end:
            match_index = index
            break

    context = [
        line
        for _start, _end, line in line_ranges[max(0, match_index - 2) : match_index + 2]
        if line.strip()
    ]
    return "\n".join(context) or match.group(0)
