from __future__ import annotations

from dataclasses import dataclass


COMMAND_HELP = """CliCourier commands
/status - show bridge and agent status
/start_agent - start the configured agent
/stop_agent - stop the active agent
/restart_agent - restart the active agent
/agent <text> - send text to the active agent
/agents - list configured adapter options
/pwd - show workspace path used by file commands
/ls [path] - list a workspace directory
/tree [path] - show a small workspace tree
/cd <path> - change bot file-command directory
/cat <path> - return a small text file
/sendfile <path> - send a safe workspace file
/screenshot - send the newest screenshot artifact
/approve - approve the pending agent prompt
/reject - reject the pending agent prompt
/voice_approve - send the pending transcript
/voice_reject - discard the pending transcript
/voice_edit <text> - replace the pending transcript
/mute - suppress proactive agent output
/unmute - resume proactive agent output
/mute_status - show notification mute state
/help - show this command list
"""


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: str


def parse_command(text: str) -> ParsedCommand | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, args = stripped.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    if not name:
        return None
    return ParsedCommand(name=name, args=args.strip())
