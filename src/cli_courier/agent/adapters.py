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
    approval_patterns: tuple[re.Pattern[str], ...]
    approve_input: str
    reject_input: str
    submit_sequence: str

    def build_command(self, configured_command: str | None = None) -> list[str]: ...

    def normalize_output(self, output: str) -> str: ...

    def cleanup_prompt(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class BaseAdapter:
    id: str
    display_name: str
    default_command: tuple[str, ...]
    approval_patterns: tuple[re.Pattern[str], ...]
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
            approve_input="y",
            reject_input="n",
        )


class GenericCliAdapter(BaseAdapter):
    def __init__(self) -> None:
        super().__init__(
            id="generic",
            display_name="Generic CLI",
            default_command=("sh",),
            approval_patterns=GENERIC_APPROVAL_PATTERNS,
            approve_input="y",
            reject_input="n",
        )


def list_adapters() -> dict[str, AgentAdapter]:
    adapters: tuple[AgentAdapter, ...] = (CodexAdapter(), GenericCliAdapter())
    return {adapter.id: adapter for adapter in adapters}


def get_adapter(adapter_id: str) -> AgentAdapter:
    adapters = list_adapters()
    try:
        return adapters[adapter_id]
    except KeyError as exc:
        raise ValueError(f"unknown agent adapter: {adapter_id}") from exc
