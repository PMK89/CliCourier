from __future__ import annotations

from dataclasses import dataclass

from cli_courier.config import Settings, UnauthorizedReplyMode


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: int | None
    chat_id: int | None
    chat_type: str | None


def is_authorized(identity: TelegramIdentity, settings: Settings) -> bool:
    if identity.user_id is None:
        return False
    if identity.user_id not in settings.allowed_telegram_user_ids:
        return False
    if not settings.allow_group_chats and identity.chat_type != "private":
        return False
    return True


def unauthorized_reply(settings: Settings) -> str | None:
    if settings.unauthorized_reply_mode == UnauthorizedReplyMode.GENERIC:
        return "This bot is private."
    return None

