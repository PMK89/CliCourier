from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from cli_courier.agent.adapters import AgentAdapter
from cli_courier.security.terminal import safe_excerpt, sanitize_terminal_text
from cli_courier.state import PendingApproval, new_nonce

ApprovalDecision = Literal["approve", "reject"]

_APPROVE_WORDS = {"y", "yes", "approve", "approved", "ok", "okay", "proceed", "continue", "allow"}
_REJECT_WORDS = {"n", "no", "reject", "rejected", "cancel", "stop", "deny"}
_WORD_RE = re.compile(r"^[\s.!?,;:]+|[\s.!?,;:]+$")


def normalize_decision_text(text: str) -> str:
    return _WORD_RE.sub("", text.strip().lower())


def interpret_approval_text(text: str) -> ApprovalDecision | None:
    normalized = normalize_decision_text(text)
    if normalized in _APPROVE_WORDS:
        return "approve"
    if normalized in _REJECT_WORDS:
        return "reject"
    return None


def is_approval_like(text: str) -> bool:
    return interpret_approval_text(text) is not None


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

    tail = cleaned[-4000:]
    for pattern in adapter.approval_patterns:
        match = pattern.search(tail)
        if match is None:
            continue
        detected_at = now or datetime.now(UTC)
        return PendingApproval(
            prompt_excerpt=safe_excerpt(tail[max(0, match.start() - 500) :], 900),
            detected_at=detected_at,
            adapter_id=adapter.id,
            nonce=new_nonce(),
            expires_at=detected_at + ttl,
            message_id=message_id,
        )
    return None

