from __future__ import annotations

import re

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_ANSI_SINGLE_RE = re.compile(r"\x1b[@-Z\\-_]")
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_terminal_text(text: str) -> str:
    """Strip terminal escape/control sequences before forwarding text to Telegram."""

    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_SINGLE_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _UNSAFE_CONTROL_RE.sub("", text)


def safe_excerpt(text: str, max_chars: int = 800) -> str:
    cleaned = sanitize_terminal_text(text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[-max_chars:].lstrip()

