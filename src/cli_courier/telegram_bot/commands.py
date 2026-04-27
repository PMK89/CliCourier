from __future__ import annotations

from dataclasses import dataclass


BOT_COMMAND_SPECS: tuple[tuple[str, str], ...] = (
    ("start", "show bridge command list"),
    ("botstatus", "show bridge and agent status"),
    ("start_agent", "start the configured agent"),
    ("stop_agent", "stop the active agent"),
    ("restart_agent", "restart the active agent"),
    ("agent", "send text to the active agent"),
    ("agents", "list configured adapter options"),
    ("pwd", "show workspace path used by file commands"),
    ("ls", "list a workspace directory"),
    ("tree", "show a small workspace tree"),
    ("cd", "change bot file-command directory"),
    ("cat", "return a small text file"),
    ("sendfile", "send a safe workspace file"),
    ("screenshot", "send the newest screenshot artifact"),
    ("artifacts", "list recent screenshot artifacts"),
    ("tail", "show recent raw agent events"),
    ("log", "alias for /tail"),
    ("sendlog", "send recent raw agent events as a text file"),
    ("stream", "use editable 60-line progress output"),
    ("final", "use editable 60-line progress output"),
    ("trace_on", "include reasoning/tool/status lines"),
    ("trace_off", "suppress reasoning/tool/status lines"),
    ("approve", "approve the pending agent prompt"),
    ("reject", "reject the pending agent prompt"),
    ("voice_approve", "send the pending transcript"),
    ("voice_reject", "discard the pending transcript"),
    ("mute", "suppress proactive agent output"),
    ("unmute", "resume proactive agent output"),
    ("desktop", "same as /mute"),
    ("telegram", "same as /unmute"),
    ("mute_status", "show notification mute state"),
    ("bothelp", "show this command list"),
)


COMMAND_HELP = "CliCourier commands\n" + "\n".join(
    f"/{name} - {description}" for name, description in BOT_COMMAND_SPECS if name != "start"
)


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
