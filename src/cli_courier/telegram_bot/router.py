from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cli_courier.agent.approval import ApprovalDecision, interpret_approval_text, is_approval_like
from cli_courier.telegram_bot.commands import ParsedCommand, parse_command


class RouteKind(str, Enum):
    COMMAND = "command"
    AGENT_TEXT = "agent_text"
    APPROVAL = "approval"
    BLOCKED_APPROVAL = "blocked_approval"
    EMPTY = "empty"


@dataclass(frozen=True)
class TextRoute:
    kind: RouteKind
    command: ParsedCommand | None = None
    text: str = ""
    approval_decision: ApprovalDecision | None = None


def route_text(text: str, *, has_pending_approval: bool) -> TextRoute:
    if not text or not text.strip():
        return TextRoute(kind=RouteKind.EMPTY)
    command = parse_command(text)
    if command is not None:
        return TextRoute(kind=RouteKind.COMMAND, command=command)
    if is_approval_like(text):
        decision = interpret_approval_text(text)
        if has_pending_approval and decision is not None:
            return TextRoute(kind=RouteKind.APPROVAL, text=text, approval_decision=decision)
        return TextRoute(kind=RouteKind.BLOCKED_APPROVAL, text=text)
    return TextRoute(kind=RouteKind.AGENT_TEXT, text=text)

