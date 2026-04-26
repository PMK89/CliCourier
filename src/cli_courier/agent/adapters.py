from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Protocol

from cli_courier.security.terminal import sanitize_terminal_text


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

    def build_structured_turn_command(
        self,
        command: list[str],
        *,
        prompt: str,
        cwd: str,
        resume: bool,
        output_last_message_path: str | None = None,
    ) -> list[str]: ...


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


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


GENERIC_APPROVAL_PATTERNS = (
    _rx(r"(?:approve|allow|proceed|continue).{0,120}(?:\?|y/n|\[y/n\]|\[y/N\])"),
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
            capabilities=AdapterCapabilities(requires_pty=True),
            approve_input="y",
            reject_input="n",
        )


class GeminiAdapter(BaseAdapter):
    def __init__(self) -> None:
        super().__init__(
            id="gemini",
            display_name="Gemini CLI",
            default_command=("gemini",),
            approval_patterns=GENERIC_APPROVAL_PATTERNS,
            capabilities=AdapterCapabilities(requires_pty=True),
            approve_input="y",
            reject_input="n",
        )


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
