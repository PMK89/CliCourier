from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def new_nonce() -> str:
    return secrets.token_urlsafe(8)


@dataclass(slots=True)
class PendingApproval:
    prompt_excerpt: str
    detected_at: datetime
    adapter_id: str
    nonce: str
    expires_at: datetime
    message_id: int | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


@dataclass(slots=True)
class PendingVoiceTranscript:
    transcript: str
    detected_at: datetime
    nonce: str
    expires_at: datetime
    message_id: int | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


@dataclass(slots=True)
class RuntimeState:
    workspace_root: Path
    cwd: Path
    active_agent: Any | None = None
    agent_chat_id: int | None = None
    pending_approval: PendingApproval | None = None
    pending_voice: PendingVoiceTranscript | None = None
    sessions: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, workspace_root: Path) -> "RuntimeState":
        root = workspace_root.resolve()
        return cls(workspace_root=root, cwd=root)

    def clear_pending_approval(self) -> None:
        self.pending_approval = None

    def clear_pending_voice(self) -> None:
        self.pending_voice = None

    def set_cwd(self, path: Path) -> None:
        self.cwd = path.resolve()


def pending_voice_from_transcript(
    transcript: str,
    *,
    ttl: timedelta = timedelta(minutes=10),
    now: datetime | None = None,
) -> PendingVoiceTranscript:
    created = now or datetime.now(UTC)
    return PendingVoiceTranscript(
        transcript=transcript,
        detected_at=created,
        nonce=new_nonce(),
        expires_at=created + ttl,
    )

