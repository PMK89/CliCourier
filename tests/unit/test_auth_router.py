from __future__ import annotations

from pathlib import Path

from cli_courier.config import Settings
from cli_courier.filesystem import Sandbox
from cli_courier.screenshots import ScreenshotService
from cli_courier.state import RuntimeState
from cli_courier.telegram_bot.auth import TelegramIdentity, is_authorized
from cli_courier.telegram_bot.commands import parse_command
from cli_courier.telegram_bot.router import RouteKind, route_text
from cli_courier.telegram_bot.runtime import (
    TelegramBridgeBot,
    approval_decision_from_reactions,
    looks_like_screenshot_summary,
)
from cli_courier.voice import DisabledTranscriber


def settings(root: Path, **overrides) -> Settings:
    values = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "ALLOWED_TELEGRAM_USER_IDS": "42",
        "WORKSPACE_ROOT": str(root),
        "DEFAULT_AGENT_COMMAND": "codex",
        "SCREENSHOT_DIR": "",
        "ALLOW_GROUP_CHATS": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_private_allowlisted_user_is_authorized(tmp_path: Path) -> None:
    assert is_authorized(
        TelegramIdentity(user_id=42, chat_id=100, chat_type="private"),
        settings(tmp_path),
    )


def test_group_chat_is_blocked_by_default(tmp_path: Path) -> None:
    assert not is_authorized(
        TelegramIdentity(user_id=42, chat_id=100, chat_type="group"),
        settings(tmp_path),
    )


def test_parse_command_strips_bot_suffix() -> None:
    parsed = parse_command("/status@CliCourierBot now")
    assert parsed is not None
    assert parsed.name == "status"
    assert parsed.args == "now"


def test_route_blocks_approval_like_text_without_pending_approval() -> None:
    route = route_text("yes", has_pending_approval=False)
    assert route.kind == RouteKind.BLOCKED_APPROVAL


def test_route_maps_approval_when_pending() -> None:
    route = route_text("proceed", has_pending_approval=True)
    assert route.kind == RouteKind.APPROVAL
    assert route.approval_decision == "approve"


def test_route_maps_emoji_approval_when_pending() -> None:
    route = route_text("👍", has_pending_approval=True)
    assert route.kind == RouteKind.APPROVAL
    assert route.approval_decision == "approve"

    route = route_text("👎", has_pending_approval=True)
    assert route.kind == RouteKind.APPROVAL
    assert route.approval_decision == "reject"


def test_route_sends_regular_text_to_agent() -> None:
    route = route_text("please inspect the diff", has_pending_approval=False)
    assert route.kind == RouteKind.AGENT_TEXT


class FakeReaction:
    def __init__(self, emoji: str) -> None:
        self.emoji = emoji


def test_reaction_approval_mapping() -> None:
    assert approval_decision_from_reactions([FakeReaction("👍")]) == "approve"
    assert approval_decision_from_reactions([FakeReaction("❤️")]) == "approve"
    assert approval_decision_from_reactions([FakeReaction("👎")]) == "reject"


def test_screenshot_summary_detection() -> None:
    assert looks_like_screenshot_summary("Size: 1280x720 PNG.")
    assert looks_like_screenshot_summary("1280x720 jpeg")
    assert not looks_like_screenshot_summary("The screenshot is attached.")


def test_initial_agent_context_is_prepended_once(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    first = bot._agent_user_text("Do the task")
    second = bot._agent_user_text("Next task")

    assert "CliCourier workspace root" in first
    assert "User request:\nDo the task" in first
    assert second == "Next task"
