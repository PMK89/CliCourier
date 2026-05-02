from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Protocol

from cli_courier.security.terminal import sanitize_terminal_text

if TYPE_CHECKING:
    from cli_courier.agent.events import AgentEvent


class AgentAdapter(Protocol):
    id: str
    display_name: str
    default_command: tuple[str, ...]
    capabilities: "AdapterCapabilities"
    approval_patterns: tuple[re.Pattern[str], ...]
    approve_input: str
    reject_input: str
    submit_sequence: str

    def build_command(self, configured_command: str | None = None) -> list[str]: ...

    def normalize_output(self, output: str) -> str: ...

    def cleanup_prompt(self, prompt: str) -> str: ...

    def build_resume_command(self, command: list[str]) -> list[str]: ...

    def strip_resume_command(self, command: list[str]) -> list[str]: ...

    def build_structured_turn_command(
        self,
        command: list[str],
        *,
        prompt: str,
        cwd: str,
        resume: bool,
        output_last_message_path: str | None = None,
    ) -> list[str]: ...

    def parse_jsonl_line(self, line: str, *, session_id: str | None = None) -> "Iterable[AgentEvent]": ...


@dataclass(frozen=True)
class AdapterCapabilities:
    supports_structured_stream: bool = False
    supports_resume: bool = False
    supports_partial_final: bool = False
    supports_approval_events: bool = False
    supports_file_events: bool = False
    requires_pty: bool = True


@dataclass(frozen=True)
class BaseAdapter:
    id: str
    display_name: str
    default_command: tuple[str, ...]
    approval_patterns: tuple[re.Pattern[str], ...]
    capabilities: AdapterCapabilities = AdapterCapabilities()
    approve_input: str = "y"
    reject_input: str = "n"
    submit_sequence: str = "\r"

    def build_command(self, configured_command: str | None = None) -> list[str]:
        if configured_command:
            command = shlex.split(configured_command)
        else:
            command = list(self.default_command)
        if not command:
            raise ValueError("agent command must not be empty")
        return command

    def normalize_output(self, output: str) -> str:
        return sanitize_terminal_text(output)

    def cleanup_prompt(self, prompt: str) -> str:
        return sanitize_terminal_text(prompt).strip()

    def build_resume_command(self, command: list[str]) -> list[str]:
        return command

    def strip_resume_command(self, command: list[str]) -> list[str]:
        return list(command)

    def build_structured_turn_command(
        self,
        command: list[str],
        *,
        prompt: str,
        cwd: str,
        resume: bool,
        output_last_message_path: str | None = None,
    ) -> list[str]:
        raise NotImplementedError(f"{self.id} does not support structured stream mode")

    def parse_jsonl_line(self, line: str, *, session_id: str | None = None) -> "Iterable[AgentEvent]":
        raise NotImplementedError(f"{self.id} does not support structured stream mode")


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


GENERIC_APPROVAL_PATTERNS = (
    _rx(r"\b(?:approve|allow|proceed|continue)\b.{0,120}(?:\?|y/n|\[y/n\]|\[y/N\])"),
    _rx(r"(?:do you want|would you like).{0,160}(?:\?|y/n|\[y/n\]|\[y/N\])"),
    _rx(r"(?:\[y/N\]|\[y/n\]|\(y/N\)|\(y/n\))"),
)


class CodexAdapter(BaseAdapter):
    def __init__(self) -> None:
        super().__init__(
            id="codex",
            display_name="Codex CLI",
            default_command=("codex",),
            approval_patterns=(
                _rx(r"codex.{0,120}(?:approve|allow|proceed|continue).{0,120}\?"),
                *GENERIC_APPROVAL_PATTERNS,
            ),
            capabilities=AdapterCapabilities(
                supports_structured_stream=True,
                supports_resume=True,
                supports_partial_final=True,
                supports_approval_events=True,
                supports_file_events=True,
                requires_pty=False,
            ),
            approve_input="y",
            reject_input="n",
        )

    def build_structured_turn_command(
        self,
        command: list[str],
        *,
        prompt: str,
        cwd: str,
        resume: bool,
        output_last_message_path: str | None = None,
    ) -> list[str]:
        if not command:
            raise ValueError("agent command must not be empty")
        base = _strip_existing_exec(command)
        if resume:
            result = [base[0], "exec", "resume", "--last", *base[1:]]
        else:
            result = [base[0], "exec", *base[1:]]
            if not _has_option_with_value(result, "--cd", "-C"):
                result.extend(["--cd", cwd])
        if "--json" not in result:
            result.append("--json")
        if output_last_message_path and not _has_option_with_value(
            result,
            "--output-last-message",
            "-o",
        ):
            result.extend(["--output-last-message", output_last_message_path])
        result.append(prompt)
        return result

    def build_resume_command(self, command: list[str]) -> list[str]:
        if not command:
            raise ValueError("agent command must not be empty")
        base = _strip_existing_exec(command)
        if len(base) >= 2 and base[1] == "resume":
            return base if "--last" in base else [base[0], "resume", "--last", *base[2:]]
        return [base[0], "resume", "--last", *base[1:]]

    def strip_resume_command(self, command: list[str]) -> list[str]:
        if len(command) >= 2 and command[1] == "resume":
            return [command[0], *(part for part in command[2:] if part != "--last")]
        if len(command) >= 3 and command[1] == "exec" and command[2] == "resume":
            return [command[0], "exec", *(part for part in command[3:] if part != "--last")]
        return list(command)

    def parse_jsonl_line(self, line: str, *, session_id: str | None = None) -> "Iterable[AgentEvent]":
        from cli_courier.agent.codex_jsonl import parse_codex_jsonl_line

        return parse_codex_jsonl_line(line, session_id=session_id)


class GenericCliAdapter(BaseAdapter):
    def __init__(self) -> None:
        super().__init__(
            id="generic",
            display_name="Generic CLI",
            default_command=("sh",),
            approval_patterns=GENERIC_APPROVAL_PATTERNS,
            capabilities=AdapterCapabilities(requires_pty=True),
            approve_input="y",
            reject_input="n",
        )


class ClaudeAdapter(BaseAdapter):
    def __init__(self) -> None:
        super().__init__(
            id="claude",
            display_name="Claude Code",
            default_command=("claude",),
            approval_patterns=GENERIC_APPROVAL_PATTERNS,
            capabilities=AdapterCapabilities(
                supports_structured_stream=True,
                supports_resume=True,
                supports_partial_final=True,
                supports_approval_events=False,
                supports_file_events=False,
                requires_pty=False,
            ),
            approve_input="y",
            reject_input="n",
        )

    def build_structured_turn_command(
        self,
        command: list[str],
        *,
        prompt: str,
        cwd: str,
        resume: bool,
        output_last_message_path: str | None = None,
    ) -> list[str]:
        if not command:
            raise ValueError("agent command must not be empty")
        result = list(command)
        if "--print" not in result and "-p" not in result:
            result.append("--print")
        if not _has_option_with_value(result, "--output-format"):
            result.extend(["--output-format", "stream-json"])
        # Required by Claude Code when using --print with stream-json
        if "--verbose" not in result:
            result.append("--verbose")
        if resume and "--continue" not in result and "-c" not in result:
            result.append("--continue")
        result.append(prompt)
        return result

    def build_resume_command(self, command: list[str]) -> list[str]:
        result = list(command)
        if "--continue" not in result and "-c" not in result:
            result.append("--continue")
        return result

    def strip_resume_command(self, command: list[str]) -> list[str]:
        return [part for part in command if part not in {"--continue", "-c"}]

    def parse_jsonl_line(self, line: str, *, session_id: str | None = None) -> "Iterable[AgentEvent]":
        from cli_courier.agent.claude_jsonl import parse_claude_jsonl_line

        return parse_claude_jsonl_line(line, session_id=session_id)


class GeminiAdapter(BaseAdapter):
    def __init__(self) -> None:
        super().__init__(
            id="gemini",
            display_name="Gemini CLI",
            default_command=("gemini",),
            approval_patterns=GENERIC_APPROVAL_PATTERNS,
            capabilities=AdapterCapabilities(
                supports_structured_stream=True,
                supports_resume=True,
                supports_partial_final=True,
                supports_approval_events=False,
                supports_file_events=False,
                requires_pty=False,
            ),
            approve_input="y",
            reject_input="n",
        )

    def build_structured_turn_command(
        self,
        command: list[str],
        *,
        prompt: str,
        cwd: str,
        resume: bool,
        output_last_message_path: str | None = None,
    ) -> list[str]:
        if not command:
            raise ValueError("agent command must not be empty")
        result = list(command)
        if not _has_option_with_value(result, "--output-format") and "-o" not in result:
            result.extend(["--output-format", "stream-json"])
        if "-y" not in result and "--yolo" not in result and "--approval-mode" not in result:
            result.append("--yolo")
        if "--skip-trust" not in result:
            result.append("--skip-trust")
        if resume and "-r" not in result and "--resume" not in result:
            result.extend(["--resume", "latest"])

        # gemini requires --prompt for non-interactive mode
        if "-p" not in result and "--prompt" not in result:
            result.extend(["--prompt", prompt])
        else:
            result.append(prompt)
        return result

    def build_resume_command(self, command: list[str]) -> list[str]:
        result = list(command)
        if "-r" not in result and "--resume" not in result:
            result.extend(["--resume", "latest"])
        return result

    def strip_resume_command(self, command: list[str]) -> list[str]:
        result: list[str] = []
        skip_next = False
        for index, part in enumerate(command):
            if skip_next:
                skip_next = False
                continue
            if part.startswith("--resume="):
                continue
            if part in {"--resume", "-r"}:
                if index + 1 < len(command) and not command[index + 1].startswith("-"):
                    skip_next = True
                continue
            result.append(part)
        return result

    def parse_jsonl_line(self, line: str, *, session_id: str | None = None) -> Iterable["AgentEvent"]:
        from cli_courier.agent.gemini_jsonl import parse_gemini_jsonl_line

        return parse_gemini_jsonl_line(line, session_id=session_id)


def list_adapters() -> dict[str, AgentAdapter]:
    adapters: tuple[AgentAdapter, ...] = (
        CodexAdapter(),
        ClaudeAdapter(),
        GeminiAdapter(),
        GenericCliAdapter(),
    )
    return {adapter.id: adapter for adapter in adapters}


def get_adapter(adapter_id: str) -> AgentAdapter:
    adapters = list_adapters()
    try:
        return adapters[adapter_id]
    except KeyError as exc:
        raise ValueError(f"unknown agent adapter: {adapter_id}") from exc


def _strip_existing_exec(command: list[str]) -> list[str]:
    if len(command) >= 2 and command[1] == "exec":
        return [command[0], *command[2:]]
    return command


def _has_option_with_value(command: list[str], *options: str) -> bool:
    return any(
        option in command or any(part.startswith(f"{option}=") for part in command)
        for option in options
    )
