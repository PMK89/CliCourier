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
class PendingChoice:
    prompt_excerpt: str
    options: tuple[str, ...]
    selected_index: int
    detected_at: datetime
    nonce: str
    expires_at: datetime
    message_id: int | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


@dataclass(frozen=True, slots=True)
class PendingActionChoice:
    id: str
    label: str
    value: str = ""


@dataclass(slots=True)
class PendingAction:
    id: str
    kind: str
    session_id: str | None
    chat_id: int | None
    created_at: datetime
    expires_at: datetime
    choices: tuple[PendingActionChoice, ...]
    source_event_id: str | None = None
    message_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at

    def choice(self, choice_id: str) -> PendingActionChoice | None:
        return next((choice for choice in self.choices if choice.id == choice_id), None)


@dataclass(slots=True)
class RuntimeState:
    workspace_root: Path
    cwd: Path
    active_agent: Any | None = None
    agent_chat_id: int | None = None
    pending_approval: PendingApproval | None = None
    pending_voice: PendingVoiceTranscript | None = None
    pending_choice: PendingChoice | None = None
    pending_actions: dict[str, PendingAction] = field(default_factory=dict)
    sessions: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, workspace_root: Path) -> "RuntimeState":
        root = workspace_root.resolve()
        return cls(workspace_root=root, cwd=root)

    def clear_pending_approval(self) -> None:
        self.pending_approval = None
        self.clear_pending_actions(kind="approval")

    def clear_pending_voice(self) -> None:
        self.pending_voice = None
        self.clear_pending_actions(kind="voice_transcript")

    def clear_pending_choice(self) -> None:
        self.pending_choice = None
        self.clear_pending_actions(kind="choice_request")

    def set_cwd(self, path: Path) -> None:
        self.cwd = path.resolve()

    def add_pending_action(self, action: PendingAction) -> PendingAction:
        self.prune_expired_pending_actions()
        self.pending_actions[action.id] = action
        return action

    def pending_action(self, action_id: str, *, now: datetime | None = None) -> PendingAction | None:
        action = self.pending_actions.get(action_id)
        if action is None:
            return None
        if action.is_expired(now):
            self.pending_actions.pop(action_id, None)
            return None
        return action

    def active_pending_action(
        self,
        kind: str,
        *,
        session_id: str | None = None,
        chat_id: int | None = None,
        now: datetime | None = None,
    ) -> PendingAction | None:
        self.prune_expired_pending_actions(now)
        actions = [
            action
            for action in self.pending_actions.values()
            if action.kind == kind
            and (session_id is None or action.session_id == session_id)
            and (chat_id is None or action.chat_id == chat_id)
        ]
        if not actions:
            return None
        return max(actions, key=lambda action: action.created_at)

    def clear_pending_action(self, action_id: str) -> None:
        self.pending_actions.pop(action_id, None)

    def clear_pending_actions(self, *, kind: str | None = None, chat_id: int | None = None) -> None:
        if kind is None:
            if chat_id is None:
                self.pending_actions.clear()
                return
            for action_id in [
                action.id for action in self.pending_actions.values() if action.chat_id == chat_id
            ]:
                self.pending_actions.pop(action_id, None)
            return
        if chat_id is None:
            for action_id in [
                action.id for action in self.pending_actions.values() if action.kind == kind
            ]:
                self.pending_actions.pop(action_id, None)
            return
        for action_id in [
            action.id
            for action in self.pending_actions.values()
            if action.kind == kind and action.chat_id == chat_id
        ]:
            self.pending_actions.pop(action_id, None)

    def prune_expired_pending_actions(self, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        for action_id in [
            action.id for action in self.pending_actions.values() if action.is_expired(current)
        ]:
            self.pending_actions.pop(action_id, None)


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


def pending_choice_from_options(
    prompt_excerpt: str,
    options: tuple[str, ...],
    *,
    selected_index: int = 0,
    ttl: timedelta = timedelta(minutes=5),
    now: datetime | None = None,
) -> PendingChoice:
    created = now or datetime.now(UTC)
    return PendingChoice(
        prompt_excerpt=prompt_excerpt,
        options=options,
        selected_index=selected_index,
        detected_at=created,
        nonce=new_nonce(),
        expires_at=created + ttl,
    )


def new_action_id() -> str:
    return f"act_{secrets.token_urlsafe(8).replace('-', '').replace('_', '')}"


def pending_action(
    *,
    kind: str,
    choices: tuple[PendingActionChoice, ...],
    session_id: str | None = None,
    chat_id: int | None = None,
    source_event_id: str | None = None,
    ttl: timedelta = timedelta(minutes=10),
    now: datetime | None = None,
    data: dict[str, Any] | None = None,
) -> PendingAction:
    created = now or datetime.now(UTC)
    return PendingAction(
        id=new_action_id(),
        kind=kind,
        session_id=session_id,
        chat_id=chat_id,
        created_at=created,
        expires_at=created + ttl,
        choices=choices,
        source_event_id=source_event_id,
        data=dict(data or {}),
    )


def pending_approval_action(
    *,
    session_id: str | None,
    chat_id: int | None,
    source_event_id: str | None,
    prompt: str,
    ttl: timedelta = timedelta(minutes=10),
    now: datetime | None = None,
    data: dict[str, Any] | None = None,
) -> PendingAction:
    return pending_action(
        kind="approval",
        session_id=session_id,
        chat_id=chat_id,
        source_event_id=source_event_id,
        ttl=ttl,
        now=now,
        choices=(
            PendingActionChoice(id="approve", label="Approve", value="approve"),
            PendingActionChoice(id="reject", label="Reject", value="reject"),
            PendingActionChoice(id="details", label="Details", value="details"),
            PendingActionChoice(id="sendlog", label="Send log", value="sendlog"),
        ),
        data={"prompt": prompt, **dict(data or {})},
    )


def pending_voice_action_from_transcript(
    transcript: str,
    *,
    session_id: str | None = None,
    chat_id: int | None = None,
    ttl: timedelta = timedelta(minutes=10),
    now: datetime | None = None,
) -> PendingAction:
    return pending_action(
        kind="voice_transcript",
        session_id=session_id,
        chat_id=chat_id,
        ttl=ttl,
        now=now,
        choices=(
            PendingActionChoice(id="send", label="Send", value="send"),
            PendingActionChoice(id="cancel", label="Cancel", value="cancel"),
            PendingActionChoice(id="edit", label="Edit", value="edit"),
        ),
        data={"transcript": transcript},
    )
