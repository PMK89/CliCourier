from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from cli_courier.agent.adapters import GenericCliAdapter
from cli_courier.agent.events import AgentEvent, AgentEventKind
from cli_courier.config import Settings
from cli_courier.filesystem import Sandbox
from cli_courier.screenshots import ScreenshotService
from cli_courier.state import (
    PendingActionChoice,
    RuntimeState,
    pending_action,
    pending_approval_action,
    pending_voice_action_from_transcript,
)
from cli_courier.telegram_bot.auth import TelegramIdentity, is_authorized
from cli_courier.telegram_bot.commands import parse_command
from cli_courier.telegram_bot.dashboard import DashboardSnapshot, render_dashboard
from cli_courier.telegram_bot.router import RouteKind, route_text
from cli_courier.telegram_bot.runtime import (
    TelegramBridgeBot,
    approval_decision_from_reactions,
    detect_interactive_choices,
    extract_screenshot_reference,
    looks_like_screenshot_reference,
    looks_like_screenshot_summary,
    normalize_echo_text,
)
from cli_courier.voice import DisabledTranscriber


class EchoTranscriber:
    async def transcribe(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")


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


def test_screenshot_reference_detection() -> None:
    assert looks_like_screenshot_reference("Screenshot saved to output/playwright/site.png")
    assert looks_like_screenshot_reference("Captured image artifact: page.webp")
    assert looks_like_screenshot_reference("Size: 1280x720 PNG.")
    assert not looks_like_screenshot_reference("This is not the screenshot, please fix clicourier")
    assert not looks_like_screenshot_reference("No visual artifact was created.")


def test_extract_screenshot_reference_from_saved_message() -> None:
    assert (
        extract_screenshot_reference(
            "• Screenshot saved to output/playwright/openai-com-2026-04-25.png"
        )
        == "output/playwright/openai-com-2026-04-25.png"
    )


def test_normalize_echo_text_collapses_terminal_prompt_text() -> None:
    assert normalize_echo_text("  Please open\nopenai.com   ") == "Please open openai.com"


def test_detect_interactive_choices_detects_reasoning_menu() -> None:
    assert detect_interactive_choices(
        "Select reasoning effort\n"
        "› low\n"
        "  medium\n"
        "  high\n"
        "  xhigh\n"
    ) == (
        "Select reasoning effort",
        ["low", "medium", "high", "xhigh"],
        0,
    )


def test_detect_interactive_choices_ignores_markdown_example_bullets() -> None:
    assert (
        detect_interactive_choices(
            "Examples of requests:\n"
            "* summarize recent commits\n"
            "* change the model\n"
            "These are example phrases, not choices.\n"
        )
        is None
    )


def test_detect_interactive_choices_ignores_codex_prompt_and_status_snapshot() -> None:
    assert (
        detect_interactive_choices(
            "› I send to voice messages that are supposed to be transcribed with the whisper "
            "model but this doesn't work. Please fix it\n"
            "  Summarize recent commits\n"
            "  gpt-5.5 medium · ~/CliCourier\n"
        )
        is None
    )


def test_detect_interactive_choices_ignores_numbered_normal_output() -> None:
    assert (
        detect_interactive_choices(
            "1. I send two voice messages that should be transcribed.\n"
            "2. This is normal output, not a terminal selection.\n"
            "3. gpt-5.5 medium · ~/CliCourier\n"
        )
        is None
    )


def test_detect_interactive_choices_ignores_codex_final_output_marker() -> None:
    assert (
        detect_interactive_choices(
            "› Fixed final-output forwarding for Codex.\n"
            "  Added regression coverage for choice detection.\n"
            "  Verified with pytest.\n"
        )
        is None
    )


def test_prompt_placeholder_is_not_detected_as_choice() -> None:
    assert (
        detect_interactive_choices(
            "› {{prompt}}\n"
            "  Write the answer here\n"
            "  gpt-5.5 medium · ~/CliCourier\n"
        )
        is None
    )


def test_detect_interactive_choices_detects_codex_model_menu() -> None:
    assert detect_interactive_choices(
        "Select Model\n"
        "Pick a quick auto mode or browse all models.\n"
        "› gpt-5.5 xhigh\n"
        "  gpt-5.4 high\n"
        "  gpt-5.3-codex medium\n"
    ) == (
        "Select Model",
        ["gpt-5.5 xhigh", "gpt-5.4 high", "gpt-5.3-codex medium"],
        0,
    )


def test_detect_interactive_choices_detects_codex_numbered_model_menu() -> None:
    assert detect_interactive_choices(
        "Select Model and Effort\n"
        "Access legacy models by running codex -m <model_name> or in your config.toml\n"
        "› 1. gpt-5.5 (current)  Frontier model for complex coding, research, and real-world work.\n"
        "  2. gpt-5.4  Strong model for everyday coding.\n"
        "  3. gpt-5.4-mini  Small, fast, and cost-efficient model for simpler coding tasks.\n"
        "  4. gpt-5.3-codex  Coding-optimized model.\n"
        "  5. gpt-5.2  Optimized for professional work and long-running agents.\n"
        "Press enter to select reasoning effort, or esc to dismiss.\n"
    ) == (
        "Select Model and Effort",
        [
            "gpt-5.5 (current)  Frontier model for complex coding, research, and real-world work.",
            "gpt-5.4  Strong model for everyday coding.",
            "gpt-5.4-mini  Small, fast, and cost-efficient model for simpler coding tasks.",
            "gpt-5.3-codex  Coding-optimized model.",
            "gpt-5.2  Optimized for professional work and long-running agents.",
        ],
        0,
    )


def test_detect_interactive_choices_detects_codex_numbered_reasoning_menu() -> None:
    assert detect_interactive_choices(
        "Select Reasoning Level for gpt-5.5\n"
        "  1. Low  Fast responses with lighter reasoning\n"
        "› 2. Medium (default) (current)  Balances speed and reasoning depth for everyday tasks\n"
        "  3. High  Greater reasoning depth for complex problems\n"
        "  4. Extrahigh  Extra high reasoning depth for complex problems\n"
        "Press enter to confirm or esc to go back\n"
    ) == (
        "Select Reasoning Level for gpt-5.5",
        [
            "Low  Fast responses with lighter reasoning",
            "Medium (default) (current)  Balances speed and reasoning depth for everyday tasks",
            "High  Greater reasoning depth for complex problems",
            "Extrahigh  Extra high reasoning depth for complex problems",
        ],
        1,
    )


def test_detect_interactive_choices_detects_generic_slash_menu() -> None:
    assert detect_interactive_choices(
        "Select Personality\n"
        "› Warm\n"
        "  Concise\n"
    ) == ("Select Personality", ["Warm", "Concise"], 0)


def test_pending_action_creation_and_lookup(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    action = pending_action(
        kind="approval",
        session_id="codex",
        chat_id=100,
        choices=(PendingActionChoice(id="approve", label="Approve"),),
        source_event_id="evt_1",
    )

    state.add_pending_action(action)

    assert state.pending_action(action.id) is action
    assert state.active_pending_action("approval") is action
    assert state.active_pending_action("approval", chat_id=100) is action
    assert state.active_pending_action("approval", chat_id=200) is None


def test_dashboard_rendering_stays_under_telegram_limit() -> None:
    rendered = render_dashboard(
        DashboardSnapshot(
            agent_name="Codex CLI",
            state="running",
            cwd="/repo",
            current_phase="shell",
            last_event="pytest is running",
            output_tail="x" * 10000,
        ),
        limit=4096,
    )

    assert len(rendered) <= 4096


async def test_dashboard_update_skips_unchanged_text(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    await bridge._maybe_update_dashboard(bot_api, 100, session, force=True)
    await bridge._maybe_update_dashboard(bot_api, 100, session, force=True)

    assert len(bot_api.send_calls) == 1
    assert bot_api.edit_calls == []


async def test_dashboard_update_ignores_telegram_not_modified_error(
    tmp_path: Path,
    capsys,
) -> None:
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = NotModifiedEditBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    await bridge._maybe_update_dashboard(bot_api, 100, session, force=True)
    bridge._dashboard_last_text[100] = "previous dashboard text"
    await bridge._maybe_update_dashboard(bot_api, 100, session, force=True)

    assert len(bot_api.send_calls) == 1
    assert len(bot_api.edit_calls) == 1
    assert "telegram dashboard edit failed" not in capsys.readouterr().out


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


class FakeAgent:
    is_running = True
    adapter = GenericCliAdapter()

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.keys: list[str] = []
        self.output_queue: asyncio.Queue = asyncio.Queue()

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_approval(self, text: str) -> None:
        self.sent.append(text)

    async def send_key(self, key: str) -> None:
        self.keys.append(key)


class FakeFlushSession:
    replaces_output_snapshots = False
    adapter = GenericCliAdapter()
    backend = "pty"
    cwd = Path(".")

    def __init__(self) -> None:
        self.output_queue: asyncio.Queue[str] = asyncio.Queue()
        self.is_running = True
        self.current_tool = None

    def recent_output(self, max_chars: int | None = None) -> str:
        return ""

    def recent_visible_output(self, max_chars: int | None = None) -> str:
        return ""

    def status(self):
        return SimpleNamespace(
            adapter_name="Generic CLI",
            state="idle",
            current_tool=None,
            last_event="",
        )


class FakeMessage:
    chat_id = 100

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.text: str | None = None
        self.caption: str | None = None
        self.voice = None
        self.audio = None
        self.document = None
        self.photo = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.photos = 0
        self.documents: list[str | None] = []
        self.messages: list[str] = []
        self.reply_markups: list[object] = []
        self.send_calls: list[tuple[int, int, str]] = []
        self.edits: list[tuple[int, str]] = []
        self.edit_calls: list[tuple[int, int, str]] = []
        self.fail_photo = False
        self.commands = None

    async def send_chat_action(self, *, chat_id: int, action: str) -> None:
        return None

    async def send_message(self, *, chat_id: int, text: str, **kwargs):
        message_id = len(self.messages) + 1
        self.messages.append(text)
        self.reply_markups.append(kwargs.get("reply_markup"))
        self.send_calls.append((message_id, chat_id, text))
        return SimpleNamespace(message_id=message_id)

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, **kwargs) -> None:
        self.edits.append((message_id, text))
        self.edit_calls.append((message_id, chat_id, text))
        if 1 <= message_id <= len(self.messages):
            self.messages[message_id - 1] = text

    async def set_my_commands(self, commands) -> None:
        self.commands = commands

    async def send_photo(self, *, chat_id: int, photo) -> None:
        self.photos += 1
        if self.fail_photo:
            raise RuntimeError("photo rejected")

    async def send_document(self, *, chat_id: int, document, filename: str | None = None) -> None:
        self.documents.append(filename)


def non_dashboard_messages(messages: list[str]) -> list[str]:
    return [progress_body(message) for message in messages if not message.startswith("Agent:")]


def non_dashboard_send_calls(bot: FakeBot) -> list[tuple[int, int, str]]:
    return [
        (message_id, chat_id, progress_body(text))
        for message_id, chat_id, text in bot.send_calls
        if not text.startswith("Agent:")
    ]


def progress_body(text: str) -> str:
    lines = text.splitlines()
    if (
        len(lines) >= 3
        and lines[0] in {"Running.", "Finished."}
        and lines[1] in {"Showing latest 60 lines", "Showing final 60 lines"}
        and lines[2] == ""
    ):
        return "\n".join(lines[3:])
    return text


class EditFailBot(FakeBot):
    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, **kwargs) -> None:
        self.edits.append((message_id, text))
        self.edit_calls.append((message_id, chat_id, text))
        raise RuntimeError("edit rejected")


class NotModifiedEditBot(FakeBot):
    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, **kwargs) -> None:
        self.edits.append((message_id, text))
        self.edit_calls.append((message_id, chat_id, text))
        raise RuntimeError(
            "Message is not modified: specified new message content and reply markup "
            "are exactly the same as a current content and reply markup of the message"
        )


class FakeTelegramFile:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def download_to_drive(self, *, custom_path: Path) -> None:
        custom_path.write_bytes(self.content)


class FakeFileBot(FakeBot):
    def __init__(self, content: bytes) -> None:
        super().__init__()
        self.content = content
        self.requested_file_id: str | None = None

    async def get_file(self, file_id: str) -> FakeTelegramFile:
        self.requested_file_id = file_id
        return FakeTelegramFile(self.content)


class FakeContext:
    def __init__(self) -> None:
        self.bot = FakeBot()
        self.application = SimpleNamespace(bot=self.bot, running=False)


class FakeFileContext:
    def __init__(self, content: bytes) -> None:
        self.bot = FakeFileBot(content)


class FakeAudioAttachment:
    file_id = "file-1"
    file_unique_id = "unique-1"
    file_size = 10
    file_name = "voice-message.ogg"
    mime_type = "audio/ogg"


class FakeImageAttachment:
    file_id = "image-1"
    file_unique_id = "image-unique-1"
    file_size = 10
    file_name = "photo.jpg"
    mime_type = "image/jpeg"


class FakeDocumentAttachment:
    file_id = "document-1"
    file_unique_id = "document-unique-1"
    file_size = 10
    file_name = "example.py"
    mime_type = "text/x-python"


class FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.edits: list[str] = []
        self.answered = False

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)


class FakeCallbackUpdate:
    def __init__(self, query: FakeCallbackQuery, *, chat_id: int = 100) -> None:
        self.callback_query = query
        self.effective_user = SimpleNamespace(id=42)
        self.effective_chat = SimpleNamespace(id=chat_id, type="private")


async def test_send_to_agent_acknowledges_when_output_is_muted(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, NOTIFICATION_BLOCK_FILE=str(tmp_path / ".muted"))
    app_settings.notification_block_file.write_text("muted\n", encoding="utf-8")
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()

    await bot._send_to_agent("open example.com", message, FakeContext())

    assert state.active_agent.sent
    assert message.replies == [
        "Request sent. Proactive agent output is muted; use /telegram to resume."
    ]


async def test_muted_telegram_request_still_delivers_final_output(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, NOTIFICATION_BLOCK_FILE=str(tmp_path / ".muted"))
    app_settings.notification_block_file.write_text("muted\n", encoding="utf-8")
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()
    bot_api = FakeBot()

    await bridge._send_to_agent("open example.com", message, FakeContext())
    await bridge._send_agent_output(bot_api, 100, "Done.", complete_request=True)

    assert non_dashboard_messages(bot_api.messages) == ["Done."]


async def test_muted_telegram_request_still_delivers_approval_prompt(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, NOTIFICATION_BLOCK_FILE=str(tmp_path / ".muted"))
    app_settings.notification_block_file.write_text("muted\n", encoding="utf-8")
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()
    bot_api = FakeBot()

    await bridge._send_to_agent("open example.com", message, FakeContext())
    await bridge._handle_agent_event(
        bot_api,
        100,
        state.active_agent,
        AgentEvent(
            kind=AgentEventKind.APPROVAL_REQUESTED,
            text="Run command?",
            session_id="generic",
        ),
    )

    assert bot_api.messages == ["Approval required:\nRun command?"]


async def test_terminal_progress_posts_after_sixty_lines_and_edits_after_next_sixty(
    tmp_path: Path,
) -> None:
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1"),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = FakeBot()

    first_page = "".join(f"line {index}\n" for index in range(1, 61))
    second_page = "".join(f"line {index}\n" for index in range(61, 121))

    await bridge._update_terminal_progress(bot_api, 100, first_page)

    assert [progress_body(message) for message in bot_api.messages] == [first_page.strip()]
    assert bot_api.edits == []

    await asyncio.sleep(0.002)
    await bridge._update_terminal_progress(bot_api, 100, second_page)

    assert [progress_body(message) for message in bot_api.messages] == [second_page.strip()]
    assert [(message_id, progress_body(text)) for message_id, text in bot_api.edits] == [
        (1, second_page.strip())
    ]


async def test_terminal_progress_final_output_updates_same_message_with_last_lines(
    tmp_path: Path,
) -> None:
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1"),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = FakeBot()

    progress = "".join(f"line {index}\n" for index in range(1, 61))
    final_text = "".join(f"line {index}\n" for index in range(1, 76))

    await bridge._update_terminal_progress(bot_api, 100, progress)
    await bridge._send_agent_output(bot_api, 100, final_text, complete_request=True)

    expected = "\n".join(f"line {index}" for index in range(16, 76))
    assert [progress_body(message) for message in bot_api.messages] == [expected]
    assert (bot_api.edits[-1][0], progress_body(bot_api.edits[-1][1])) == (1, expected)


async def test_terminal_progress_final_tail_does_not_replace_accumulated_page(
    tmp_path: Path,
) -> None:
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = FakeBot()

    progress = "".join(f"line {index}\n" for index in range(1, 61))

    await bridge._update_terminal_progress(bot_api, 100, progress)
    await bridge._send_agent_output(bot_api, 100, "line 60", complete_request=True)

    expected = "\n".join(f"line {index}" for index in range(1, 61))
    assert [progress_body(message) for message in bot_api.messages] == [expected]
    assert (bot_api.edits[-1][0], progress_body(bot_api.edits[-1][1])) == (1, expected)


async def test_send_to_agent_autostarts_agent_when_missing(tmp_path: Path, monkeypatch) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    started = {}

    async def fake_start_agent_session(*, chat_id: int, application=None, bot=None) -> str:
        started["chat_id"] = chat_id
        state.active_agent = FakeAgent()
        return "Agent started: codex"

    monkeypatch.setattr(bridge, "_start_agent_session", fake_start_agent_session)
    message = FakeMessage()
    context = FakeContext()

    await bridge._send_to_agent("open example.com", message, context)

    assert started["chat_id"] == 100
    assert state.active_agent is not None
    assert state.active_agent.sent[-1].endswith("User request:\nopen example.com")


async def test_stale_callback_is_rejected(tmp_path: Path) -> None:
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    query = FakeCallbackQuery("cc:act_missing:approve")

    await bridge.handle_callback(FakeCallbackUpdate(query), FakeContext())

    assert query.edits == ["This action expired or is no longer pending."]


async def test_approval_callback_requires_matching_pending_action(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    action = state.add_pending_action(
        pending_approval_action(
            session_id="codex",
            chat_id=100,
            source_event_id="evt_1",
            prompt="Run command?",
        )
    )
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    query = FakeCallbackQuery(f"cc:{action.id}:approve")

    await bridge.handle_callback(FakeCallbackUpdate(query), FakeContext())

    bridge._stop_typing(100)
    assert state.active_agent.sent == ["y"]
    assert state.pending_action(action.id) is None
    assert query.edits == ["Sent approve."]


async def test_approval_callback_rejects_wrong_chat(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    action = state.add_pending_action(
        pending_approval_action(
            session_id="codex",
            chat_id=100,
            source_event_id="evt_1",
            prompt="Run command?",
        )
    )
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    query = FakeCallbackQuery(f"cc:{action.id}:approve")
    query.message.chat_id = 200

    await bridge.handle_callback(FakeCallbackUpdate(query, chat_id=200), FakeContext())

    assert state.pending_action(action.id) is action
    assert state.active_agent.sent == []
    assert query.edits == ["This action belongs to a different chat."]


async def test_audio_message_is_transcribed_like_voice(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=EchoTranscriber(),
    )
    message = FakeMessage()
    message.audio = FakeAudioAttachment()
    context = FakeFileContext(b"send this to the agent")

    await bridge._handle_voice(message, context)

    pending_voice = state.active_pending_action("voice_transcript", chat_id=100)
    assert pending_voice is not None
    assert pending_voice.data["transcript"] == "send this to the agent"
    assert context.bot.requested_file_id == "file-1"
    assert message.replies == [
        "Transcript:\nsend this to the agent\n\nReply with corrected text to change it."
    ]


async def test_text_reply_updates_pending_voice_transcript(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    pending = state.add_pending_action(pending_voice_action_from_transcript("send wrong text", chat_id=100))
    message = FakeMessage()

    handled = await bridge._maybe_handle_voice_correction("send corrected text", message)

    assert handled is True
    assert state.pending_action(pending.id) is not None
    assert state.pending_action(pending.id).data["transcript"] == "send corrected text"
    assert message.replies == ["Transcript updated. Tap Send or use /voice_approve to send it."]


async def test_non_audio_document_is_not_transcribed(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=EchoTranscriber(),
    )
    message = FakeMessage()
    document = FakeAudioAttachment()
    document.file_name = "notes.txt"
    document.mime_type = "text/plain"
    message.document = document
    context = FakeFileContext(b"not audio")

    await bridge._handle_voice(message, context)

    assert state.active_pending_action("voice_transcript", chat_id=100) is None
    assert context.bot.requested_file_id is None
    assert message.replies == []


async def test_audio_document_is_forwarded_as_file_upload(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=EchoTranscriber(),
    )
    message = FakeMessage()
    message.document = FakeAudioAttachment()
    context = FakeFileContext(b"audio bytes")

    await bridge.handle_update(
        SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100, type="private"),
            message_reaction=None,
        ),
        context,
    )

    assert state.active_pending_action("voice_transcript", chat_id=100) is None
    assert len(state.active_agent.sent) == 1
    sent = state.active_agent.sent[0]
    assert "Please inspect the attached file." in sent
    uploaded_files = list((tmp_path / ".clicourier" / "incoming-files").iterdir())
    assert len(uploaded_files) == 1
    assert uploaded_files[0].name.endswith("-voice-message.ogg")
    assert uploaded_files[0].read_bytes() == b"audio bytes"
    assert str(uploaded_files[0]) in sent
    assert context.bot.requested_file_id == "file-1"


async def test_photo_caption_is_forwarded_with_saved_image_path(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()
    message.caption = "Please inspect this"
    message.photo = [FakeImageAttachment()]
    context = FakeFileContext(b"fake jpeg bytes")

    await bridge.handle_update(
        SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100, type="private"),
            message_reaction=None,
        ),
        context,
    )

    assert len(state.active_agent.sent) == 1
    sent = state.active_agent.sent[0]
    assert "Please inspect this" in sent
    assert "Attached image files from the bridge (including WhatsApp media):" in sent
    assert "Inspect those local image files as part of this request." in sent
    assert "incoming-media" in sent
    media_files = list((tmp_path / ".clicourier" / "incoming-media").iterdir())
    assert len(media_files) == 1
    assert media_files[0].read_bytes() == b"fake jpeg bytes"
    assert str(media_files[0]) in sent
    assert context.bot.requested_file_id == "image-1"


async def test_image_only_message_uses_default_prompt_text(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()
    document = FakeImageAttachment()
    document.file_name = "whatsapp.png"
    document.mime_type = "image/png"
    message.document = document
    context = FakeFileContext(b"png bytes")

    await bridge.handle_update(
        SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100, type="private"),
            message_reaction=None,
        ),
        context,
    )

    assert len(state.active_agent.sent) == 1
    sent = state.active_agent.sent[0]
    assert "Please inspect the attached image." in sent
    assert "whatsapp" not in sent
    media_files = list((tmp_path / ".clicourier" / "incoming-media").iterdir())
    assert len(media_files) == 1
    assert media_files[0].read_bytes() == b"png bytes"
    assert str(media_files[0]) in sent
    assert context.bot.requested_file_id == "image-1"


async def test_document_caption_is_forwarded_with_saved_file_path(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()
    message.caption = "Please inspect this file"
    message.document = FakeDocumentAttachment()
    context = FakeFileContext(b"print('hello')\n")

    await bridge.handle_update(
        SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100, type="private"),
            message_reaction=None,
        ),
        context,
    )

    assert len(state.active_agent.sent) == 1
    sent = state.active_agent.sent[0]
    assert "Please inspect this file" in sent
    assert "Attached files from the bridge:" in sent
    assert "Read those local files as part of this request." in sent
    assert "incoming-files" in sent
    uploaded_files = list((tmp_path / ".clicourier" / "incoming-files").iterdir())
    assert len(uploaded_files) == 1
    assert uploaded_files[0].name.endswith("-example.py")
    assert uploaded_files[0].read_bytes() == b"print('hello')\n"
    assert str(uploaded_files[0]) in sent
    assert context.bot.requested_file_id == "document-1"


async def test_file_only_message_uses_default_prompt_text(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()
    message.document = FakeDocumentAttachment()
    context = FakeFileContext(b"print('hello')\n")

    await bridge.handle_update(
        SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100, type="private"),
            message_reaction=None,
        ),
        context,
    )

    assert len(state.active_agent.sent) == 1
    sent = state.active_agent.sent[0]
    assert "Please inspect the attached file." in sent
    uploaded_files = list((tmp_path / ".clicourier" / "incoming-files").iterdir())
    assert len(uploaded_files) == 1
    assert uploaded_files[0].read_bytes() == b"print('hello')\n"
    assert str(uploaded_files[0]) in sent
    assert context.bot.requested_file_id == "document-1"


def test_agent_output_suppresses_sent_text_echo(tmp_path: Path) -> None:
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bridge._remember_agent_input_echo(100, "Please open openai.com and send a screenshot")

    assert (
        bridge._suppress_agent_input_echoes(
            100,
            "Please open openai.com and send a screenshot\nDone: output/playwright/page.png",
        )
        == "Done: output/playwright/page.png"
    )


def test_agent_output_strips_repeated_sent_text_echo_prefix(tmp_path: Path) -> None:
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    prompt = "Improve documentation in @filename"
    bridge._remember_agent_input_echo(100, prompt)

    assert (
        bridge._suppress_agent_input_echoes(
            100,
            f"{prompt}{prompt}{prompt}platform linux -- Python 3.11.13",
        )
        == "platform linux -- Python 3.11.13"
    )
    assert bridge._suppress_agent_input_echoes(100, f"{prompt}{prompt}") == ""


async def test_unknown_slash_command_is_forwarded_raw_to_agent(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    await bot._handle_command(parse_command("/model gpt-5.5"), FakeMessage(), FakeContext())

    assert state.active_agent.sent[-1] == "/model gpt-5.5"


async def test_restart_bridge_command_launches_detached_cli_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
    calls = {}

    class FakePopen:
        def __init__(self, command, **kwargs) -> None:
            calls["command"] = command
            calls["kwargs"] = kwargs

    monkeypatch.setattr("cli_courier.telegram_bot.runtime.subprocess.Popen", FakePopen)
    message = FakeMessage()

    await bot._handle_command(parse_command("/restart"), message, FakeContext())

    assert calls["command"][-3:] == ["restart", "--detach", "--open-terminal"]
    assert calls["kwargs"]["cwd"] == str(tmp_path)
    assert calls["kwargs"]["start_new_session"] is True
    assert message.replies == [
        "Restarting CliCourier with Codex resume. The bot will reconnect shortly.\n"
        "Opening local terminal for: tmux attach -t clicourier"
    ]


async def test_restart_bridge_command_can_disable_resume(tmp_path: Path, monkeypatch) -> None:
    bot = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    calls = {}

    class FakePopen:
        def __init__(self, command, **kwargs) -> None:
            calls["command"] = command

    monkeypatch.setattr("cli_courier.telegram_bot.runtime.subprocess.Popen", FakePopen)

    await bot._handle_command(parse_command("/restart --no-resume"), FakeMessage(), FakeContext())

    assert calls["command"][-4:] == ["restart", "--detach", "--open-terminal", "--no-resume"]


async def test_resume_command_starts_agent_with_required_resume(tmp_path: Path, monkeypatch) -> None:
    bot = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    captured = {}

    async def fake_start_agent_session(**kwargs) -> str:
        captured.update(kwargs)
        return "Agent resumed: codex resume --last"

    monkeypatch.setattr(bot, "_start_agent_session", fake_start_agent_session)
    message = FakeMessage()

    await bot._handle_command(parse_command("/resume"), message, FakeContext())

    assert captured["resume"] is True
    assert captured["resume_required"] is True
    assert message.replies == ["Agent resumed: codex resume --last"]


async def test_restart_agent_resumes_by_default(tmp_path: Path, monkeypatch) -> None:
    bot = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    captured = {}

    async def fake_start_agent_session(**kwargs) -> str:
        captured.update(kwargs)
        return "Agent resumed: codex resume --last"

    monkeypatch.setattr(bot, "_start_agent_session", fake_start_agent_session)

    await bot._handle_command(parse_command("/restart_agent"), FakeMessage(), FakeContext())

    assert captured["resume"] is True


async def test_status_slash_command_is_forwarded_to_agent(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    await bot._handle_command(parse_command("/status"), FakeMessage(), FakeContext())

    assert state.active_agent.sent[-1] == "/status"


async def test_botstatus_is_handled_locally(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()

    await bot._handle_command(parse_command("/botstatus"), message, FakeContext())

    assert message.replies == [
        "agent: stopped\n"
        "workspace: /\n"
        "pending approval: no\n"
        "output mode: final\n"
        "muted: no"
    ]


async def test_post_init_registers_telegram_command_menu(tmp_path: Path) -> None:
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
    fake_bot = FakeBot()
    application = SimpleNamespace(bot=fake_bot)

    await bot._post_init(application)

    assert fake_bot.commands is not None
    command_names = [command.command for command in fake_bot.commands]
    assert "botstatus" in command_names
    assert "status" not in command_names
    assert "bothelp" in command_names
    assert "help" not in command_names


async def test_post_init_autostarts_agent_without_sending_startup_messages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_settings = settings(
        tmp_path,
        AUTO_START_AGENT=True,
        DEFAULT_TELEGRAM_CHAT_ID="100",
    )
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
    captured = {}

    async def fake_start_agent_session(**kwargs) -> str:
        captured.update(kwargs)
        return "Agent started: codex"

    monkeypatch.setattr(bot, "_start_agent_session", fake_start_agent_session)
    fake_bot = FakeBot()
    application = SimpleNamespace(bot=fake_bot)

    await bot._post_init(application)

    assert captured["chat_id"] == 100
    assert captured["application"] is application
    assert fake_bot.messages == []


async def test_choice_reply_is_not_inferred_without_pending_action(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()

    handled = await bot._maybe_handle_choice_reply("3", message, FakeContext())

    assert handled is False
    assert state.active_agent.keys == []
    assert message.replies == []


async def test_choice_request_renders_all_options_and_sends_choice_value(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = FakeBot()

    await bot._handle_agent_event(
        bot_api,
        100,
        state.active_agent,
        AgentEvent(
            kind=AgentEventKind.CHOICE_REQUEST,
            text="Select model",
            session_id="generic",
            data={
                "choices": [
                    {"id": "1", "label": "gpt-5.5", "value": "gpt-5.5"},
                    {"id": "2", "label": "gpt-5", "value": "gpt-5"},
                ]
            },
        ),
    )

    assert non_dashboard_messages(bot_api.messages) == [
        "Select model\n\n1. gpt-5.5\n2. gpt-5\n\nReply with a number."
    ]
    assert [
        button.text
        for row in bot_api.reply_markups[-1].inline_keyboard
        for button in row
    ] == ["1. gpt-5.5", "2. gpt-5"]

    message = FakeMessage()
    handled = await bot._maybe_handle_choice_reply("2", message, FakeContext())

    assert handled is True
    assert state.active_agent.sent == ["gpt-5"]
    assert state.active_pending_action("choice_request") is None
    assert message.replies == ["Sent option 2: gpt-5"]


async def test_choice_reply_clears_cached_progress_before_sending_slash_value(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    state.add_pending_action(
        pending_action(
            kind="choice_request",
            session_id="codex",
            chat_id=100,
            choices=(
                PendingActionChoice(
                    id="2",
                    label="Review uncommitted changes",
                    value="/review uncommitted changes",
                ),
            ),
        )
    )
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot._terminal_progress(100).replace_lines(["OpenAI Codex startup banner"])
    message = FakeMessage()

    handled = await bot._maybe_handle_choice_reply("1", message, FakeContext())

    assert handled is True
    assert state.active_agent.sent == ["/review uncommitted changes"]
    assert 100 not in bot._terminal_progress_by_chat
    assert 100 in bot._interactive_output_chats
    assert message.replies == ["Sent option 1: Review uncommitted changes"]


async def test_terminal_model_choice_reply_sends_navigation_keys(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    state.add_pending_action(
        pending_action(
            kind="choice_request",
            session_id="codex",
            chat_id=100,
            choices=(
                PendingActionChoice(id="1", label="gpt-5.5 xhigh", value="1"),
                PendingActionChoice(id="2", label="gpt-5.4 high", value="2"),
                PendingActionChoice(id="3", label="gpt-5.3-codex medium", value="3"),
            ),
            data={
                "input_mode": "terminal_navigation",
                "selected_index": 0,
            },
        )
    )
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    message = FakeMessage()

    handled = await bot._maybe_handle_choice_reply("3", message, FakeContext())

    assert handled is True
    assert state.active_agent.sent == []
    assert state.active_agent.keys == ["Down", "Down", "Enter"]
    assert state.active_pending_action("choice_request") is None
    assert message.replies == ["Sent option 3: gpt-5.3-codex medium"]


async def test_terminal_model_menu_emits_pending_number_choices(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = FakeBot()
    session = FakeFlushSession()

    await bot._maybe_emit_terminal_choice_request(
        bot_api,
        100,
        session,
        "Select Model\n"
        "› gpt-5.5 xhigh\n"
        "  gpt-5.4 high\n"
        "  gpt-5.3-codex medium\n",
    )

    assert non_dashboard_messages(bot_api.messages) == [
        "Select Model\n"
        "\n"
        "1. gpt-5.5 xhigh\n"
        "2. gpt-5.4 high\n"
        "3. gpt-5.3-codex medium\n"
        "\n"
        "Reply with a number."
    ]
    pending = state.active_pending_action("choice_request", chat_id=100)
    assert pending is not None
    assert pending.data["input_mode"] == "terminal_navigation"
    assert pending.data["selected_index"] == 0
    assert [
        button.text
        for row in bot_api.reply_markups[-1].inline_keyboard
        for button in row
    ] == ["1. gpt-5.5 xhigh", "2. gpt-5.4 high", "3. gpt-5.3-codex medium"]


async def test_terminal_model_menu_button_sends_navigation_keys(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = FakeBot()
    session = FakeFlushSession()

    await bot._maybe_emit_terminal_choice_request(
        bot_api,
        100,
        session,
        "Select Model\n"
        "› gpt-5.5 xhigh\n"
        "  gpt-5.4 high\n"
        "  gpt-5.3-codex medium\n",
    )
    pending = state.active_pending_action("choice_request", chat_id=100)
    assert pending is not None
    query = FakeCallbackQuery(f"cc:{pending.id}:3")

    await bot.handle_callback(FakeCallbackUpdate(query), FakeContext())

    assert state.active_agent.sent == []
    assert state.active_agent.keys == ["Down", "Down", "Enter"]
    assert state.active_pending_action("choice_request") is None
    assert query.edits == ["Sent option: gpt-5.3-codex medium"]


async def test_choice_request_ignores_placeholder_only_prompt(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = FakeBot()

    await bot._handle_agent_event(
        bot_api,
        100,
        state.active_agent,
        AgentEvent(
            kind=AgentEventKind.CHOICE_REQUEST,
            text="› {{prompt}}\n  Write the answer here",
            session_id="generic",
            data={"choices": [{"id": "1", "label": "Write the answer here", "value": "1"}]},
        ),
    )

    assert non_dashboard_messages(bot_api.messages) == []
    assert state.active_pending_action("choice_request") is None


async def test_choice_request_ignores_explain_codebase_placeholder_prompt(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    state = RuntimeState.create(tmp_path)
    state.active_agent = FakeAgent()
    bot = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = FakeBot()

    await bot._handle_agent_event(
        bot_api,
        100,
        state.active_agent,
        AgentEvent(
            kind=AgentEventKind.CHOICE_REQUEST,
            text="Explain this codebase",
            session_id="generic",
            data={"choices": [{"id": "1", "label": "Write the answer here", "value": "1"}]},
        ),
    )

    assert non_dashboard_messages(bot_api.messages) == []
    assert state.active_pending_action("choice_request") is None


async def test_final_flush_retains_output_while_approval_pending(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="1",
        FINAL_OUTPUT_MAX_WAIT_MS="20",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    state.add_pending_action(
        pending_approval_action(
            session_id="generic",
            chat_id=100,
            source_event_id="evt_1",
            prompt="Approve?",
            now=datetime.now(UTC),
            ttl=timedelta(minutes=5),
        )
    )
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    await session.output_queue.put(
        "Fixed the Codex output parsing issues.\n"
        "\n"
        "Changed:\n"
        "- output_filter.py preserves final lines.\n"
        "- runtime.py keeps buffered output while approval is pending.\n"
        "\n"
        "Verification: pytest passed.\n"
    )

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        await asyncio.sleep(0.03)
        assert non_dashboard_messages(bot_api.messages) == []

        state.clear_pending_actions(kind="approval")
        for _ in range(50):
            if non_dashboard_messages(bot_api.messages):
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    assert non_dashboard_messages(bot_api.messages)[-1] == (
        "Fixed the Codex output parsing issues.\n"
        "\n"
        "Changed:\n"
        "- output_filter.py preserves final lines.\n"
        "- runtime.py keeps buffered output while approval is pending.\n"
        "\n"
        "Verification: pytest passed."
    )


async def test_final_flush_sends_buffered_output_when_session_stops(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="50",
        FINAL_OUTPUT_IDLE_MS="5000",
        FINAL_OUTPUT_MAX_WAIT_MS="5000",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    await session.output_queue.put("Final answer before exit.\n")

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        await asyncio.sleep(0.02)
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)
    finally:
        session.is_running = False
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert non_dashboard_messages(bot_api.messages)[-1] == "Final answer before exit."


async def test_final_mode_idle_does_not_emit_repeated_final_messages(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="1",
        FINAL_OUTPUT_MAX_WAIT_MS="20",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    await session.output_queue.put("line 1\n")
    await session.output_queue.put("line 2\n")

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        await asyncio.sleep(0.05)
        assert non_dashboard_messages(bot_api.messages) == ["line 1\nline 2"]
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    assert non_dashboard_messages(bot_api.messages) == ["line 1\nline 2"]


async def test_idle_output_is_cached_until_sixty_line_page_is_ready(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="1",
        FINAL_OUTPUT_MAX_WAIT_MS="1",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    first_half = "".join(f"line {index}\n" for index in range(1, 31))
    second_half = "".join(f"line {index}\n" for index in range(31, 61))
    second_page = "".join(f"line {index}\n" for index in range(61, 121))

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        await session.output_queue.put(first_half)
        await asyncio.sleep(0.05)
        assert non_dashboard_messages(bot_api.messages) == [first_half.strip()]

        await session.output_queue.put(second_half)
        for _ in range(50):
            if non_dashboard_messages(bot_api.messages):
                break
            await asyncio.sleep(0.005)

        await session.output_queue.put(second_page)
        for _ in range(50):
            if bot_api.edits:
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    expected = "\n".join(f"line {index}" for index in range(61, 121))
    assert non_dashboard_messages(bot_api.messages) == [expected]
    assert progress_body(bot_api.edits[-1][1]) == expected


async def test_line_by_line_deltas_publish_and_edit_sixty_line_pages(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="1",
        FINAL_OUTPUT_MAX_WAIT_MS="1",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        for index in range(1, 61):
            await session.output_queue.put(f"line {index}\n")
        for _ in range(50):
            if non_dashboard_messages(bot_api.messages):
                break
            await asyncio.sleep(0.005)

        for index in range(61, 121):
            await session.output_queue.put(f"line {index}\n")
        second_expected = "\n".join(f"line {index}" for index in range(61, 121))
        for _ in range(50):
            if non_dashboard_messages(bot_api.messages) == [second_expected]:
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    agent_sends = non_dashboard_send_calls(bot_api)
    assert len(agent_sends) == 1
    assert agent_sends[0][2] == "line 1"
    assert (bot_api.edits[-1][0], progress_body(bot_api.edits[-1][1])) == (
        agent_sends[0][0],
        second_expected,
    )
    assert non_dashboard_messages(bot_api.messages) == [second_expected]


async def test_short_final_tail_after_line_deltas_keeps_full_buffered_output(
    tmp_path: Path,
) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="1",
        FINAL_OUTPUT_MAX_WAIT_MS="1",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        for index in range(1, 76):
            await session.output_queue.put(f"line {index}\n")
        expected = "\n".join(f"line {index}" for index in range(16, 76))
        await session.output_queue.put(
            AgentEvent(
                kind=AgentEventKind.FINAL_MESSAGE,
                text="line 75",
                session_id="generic",
            )
        )
        for _ in range(100):
            if non_dashboard_messages(bot_api.messages) == [expected]:
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    assert non_dashboard_messages(bot_api.messages) == [expected]


async def test_progress_edits_single_agent_message_with_dashboard_present(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1"),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    first_page = "".join(f"line {index}\n" for index in range(1, 61))
    second_page = "".join(f"line {index}\n" for index in range(61, 121))
    second_expected = "\n".join(f"line {index}" for index in range(61, 121))

    await bridge._maybe_update_dashboard(bot_api, 100, session, force=True)
    await bridge._update_terminal_progress(bot_api, 100, first_page)
    await asyncio.sleep(0.002)
    await bridge._update_terminal_progress(bot_api, 100, second_page)

    agent_sends = non_dashboard_send_calls(bot_api)
    assert len(agent_sends) == 1
    progress_message_id = agent_sends[0][0]
    assert progress_message_id == 2
    assert [(message_id, progress_body(text)) for message_id, text in bot_api.edits] == [
        (progress_message_id, second_expected)
    ]
    assert len(bot_api.messages) == 2
    assert bot_api.messages[0].startswith("Agent:")
    assert progress_body(bot_api.messages[1]) == second_expected


async def test_complete_output_edits_existing_progress_message_not_new_message(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1"),
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    first_page = "".join(f"line {index}\n" for index in range(1, 61))
    final_text = "".join(f"line {index}\n" for index in range(1, 126))
    final_expected = "\n".join(f"line {index}" for index in range(66, 126))

    await bridge._maybe_update_dashboard(bot_api, 100, session, force=True)
    await bridge._update_terminal_progress(bot_api, 100, first_page)
    await bridge._send_agent_output(bot_api, 100, final_text, complete_request=True)

    agent_sends = non_dashboard_send_calls(bot_api)
    assert len(agent_sends) == 1
    progress_message_id = agent_sends[0][0]
    assert progress_message_id == 2
    assert (bot_api.edits[-1][0], progress_body(bot_api.edits[-1][1])) == (
        progress_message_id,
        final_expected,
    )
    assert len(bot_api.messages) == 2
    assert non_dashboard_messages(bot_api.messages) == [final_expected]


async def test_progress_logging_records_send_and_edit(tmp_path: Path, capsys) -> None:
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1"),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = FakeBot()
    first_page = "".join(f"line {index}\n" for index in range(1, 61))
    second_page = "".join(f"line {index}\n" for index in range(61, 121))

    await bridge._update_terminal_progress(bot_api, 100, first_page)
    await asyncio.sleep(0.002)
    await bridge._update_terminal_progress(bot_api, 100, second_page)

    log_output = capsys.readouterr().out
    assert "clicourier agent_output action=progress_send_ok chat_id=100 message_id=1" in log_output
    assert "clicourier agent_output action=progress_edit_ok chat_id=100 message_id=1" in log_output


async def test_progress_edit_failure_is_logged_without_sending_extra_message(
    tmp_path: Path,
    capsys,
) -> None:
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1"),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bot_api = EditFailBot()
    first_page = "".join(f"line {index}\n" for index in range(1, 61))
    second_page = "".join(f"line {index}\n" for index in range(61, 121))

    await bridge._update_terminal_progress(bot_api, 100, first_page)
    await asyncio.sleep(0.002)
    await bridge._update_terminal_progress(bot_api, 100, second_page)

    log_output = capsys.readouterr().out
    assert "clicourier agent_output action=progress_edit_failed chat_id=100 message_id=1" in log_output
    assert "RuntimeError: edit rejected" in log_output
    assert len(bot_api.messages) == 1
    assert len(non_dashboard_send_calls(bot_api)) == 1


async def test_structured_mode_waits_for_turn_completion_before_short_final_output(
    tmp_path: Path,
) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="1",
        FINAL_OUTPUT_MAX_WAIT_MS="20",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    session.backend = "structured"
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        await session.output_queue.put("line 1\n")
        await asyncio.sleep(0.05)
        assert non_dashboard_messages(bot_api.messages) == ["line 1"]

        await session.output_queue.put(
            AgentEvent(
                kind=AgentEventKind.TURN_COMPLETED,
                text="Turn completed.",
                session_id="generic",
            )
        )
        for _ in range(50):
            if non_dashboard_messages(bot_api.messages):
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    assert non_dashboard_messages(bot_api.messages) == ["line 1"]


async def test_structured_mode_edits_one_progress_message_for_output_pages(
    tmp_path: Path,
) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="1",
        FINAL_OUTPUT_MAX_WAIT_MS="20",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    session.backend = "structured"
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    first_page = "".join(f"line {index}\n" for index in range(1, 61))
    second_page = "".join(f"line {index}\n" for index in range(61, 121))
    final_text = "".join(f"line {index}\n" for index in range(1, 126))
    expected = "\n".join(f"line {index}" for index in range(66, 126))

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        await session.output_queue.put(first_page)
        await asyncio.sleep(0.02)
        await session.output_queue.put(second_page)
        await asyncio.sleep(0.02)
        await session.output_queue.put(
            AgentEvent(
                kind=AgentEventKind.FINAL_MESSAGE,
                text=final_text,
                session_id="generic",
            )
        )
        for _ in range(100):
            if non_dashboard_messages(bot_api.messages) == [expected]:
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    assert non_dashboard_messages(bot_api.messages) == [expected]
    assert bot_api.edits


async def test_stream_mode_progress_keeps_rolling_sixty_lines_across_flushes(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path,
        AGENT_OUTPUT_MODE="stream",
        OUTPUT_FLUSH_INTERVAL_MS="1",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    first_page = "".join(f"line {index}\n" for index in range(1, 61))
    second_page = "".join(f"line {index}\n" for index in range(61, 121))

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        await session.output_queue.put(first_page)
        await asyncio.sleep(0.02)
        await session.output_queue.put(second_page)
        await asyncio.sleep(0.05)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    expected = "\n".join(f"line {index}" for index in range(61, 121))

    assert non_dashboard_messages(bot_api.messages)[-1] == expected


async def test_final_message_prefers_buffered_output_when_final_event_is_only_tail(tmp_path: Path) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="5000",
        FINAL_OUTPUT_MAX_WAIT_MS="5000",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    await session.output_queue.put("Fixed startup and Telegram forwarding.\n\nVerification:\n")
    await session.output_queue.put(
        AgentEvent(
            kind=AgentEventKind.FINAL_MESSAGE,
            text="Verification:",
            session_id="generic",
        )
    )

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        for _ in range(50):
            if non_dashboard_messages(bot_api.messages):
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    assert non_dashboard_messages(bot_api.messages)[-1] == (
        "Fixed startup and Telegram forwarding.\n\nVerification:"
    )


async def test_flush_progress_filters_trace_lines_and_edits_to_full_final_output(
    tmp_path: Path,
) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="5000",
        FINAL_OUTPUT_MAX_WAIT_MS="5000",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    await session.output_queue.put("Working (14s • esc to interrupt)\n")
    await session.output_queue.put("⠋ Reading files\n")
    await session.output_queue.put("Fixed startup and Telegram forwarding.\n")
    await session.output_queue.put("\nVerification:\n")
    await session.output_queue.put(
        AgentEvent(
            kind=AgentEventKind.FINAL_MESSAGE,
            text="Verification:",
            session_id="generic",
        )
    )

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        for _ in range(50):
            if non_dashboard_messages(bot_api.messages):
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    assert non_dashboard_messages(bot_api.messages)[-1] == (
        "Fixed startup and Telegram forwarding.\n\nVerification:"
    )
    assert "Working" not in non_dashboard_messages(bot_api.messages)[-1]
    assert "Reading files" not in non_dashboard_messages(bot_api.messages)[-1]


async def test_flush_prefers_full_buffered_multiline_output_over_short_final_tail(
    tmp_path: Path,
) -> None:
    app_settings = settings(
        tmp_path,
        OUTPUT_FLUSH_INTERVAL_MS="1",
        FINAL_OUTPUT_IDLE_MS="5000",
        FINAL_OUTPUT_MAX_WAIT_MS="5000",
    )
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    await session.output_queue.put(
        "Current state is Telegram mode.\n\n"
        "/home/pmk/.local/state/clicourier/muted does not exist, so proactive output is not muted.\n\n"
        "One caveat: final replies still get through.\n"
    )
    await session.output_queue.put(
        AgentEvent(
            kind=AgentEventKind.FINAL_MESSAGE,
            text="One caveat: final replies still get through.",
            session_id="generic",
        )
    )

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        for _ in range(50):
            if non_dashboard_messages(bot_api.messages):
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    assert non_dashboard_messages(bot_api.messages)[-1] == (
        "Current state is Telegram mode.\n\n"
        "/home/pmk/.local/state/clicourier/muted does not exist, so proactive output is not muted.\n\n"
        "One caveat: final replies still get through."
    )


async def test_terminal_progress_edits_current_sixty_line_page(tmp_path: Path) -> None:
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1"),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    await bridge._update_terminal_progress(bot_api, 100, "line 1\n")
    await asyncio.sleep(0.002)
    await bridge._update_terminal_progress(bot_api, 100, "line 2\n")

    assert [progress_body(message) for message in bot_api.messages] == ["line 1\nline 2"]
    assert [(message_id, progress_body(text)) for message_id, text in bot_api.edits] == [
        (1, "line 1\nline 2")
    ]


async def test_terminal_progress_replaces_tmux_snapshots(tmp_path: Path) -> None:
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1"),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    await bridge._replace_terminal_progress(bot_api, 100, "old prompt\nold output\n")
    await asyncio.sleep(0.002)
    await bridge._replace_terminal_progress(bot_api, 100, "new output\n")

    assert [progress_body(message) for message in bot_api.messages] == ["new output"]
    assert [(message_id, progress_body(text)) for message_id, text in bot_api.edits] == [
        (1, "new output")
    ]


async def test_flush_agent_output_replaces_tmux_snapshot_progress(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1")
    state = RuntimeState.create(tmp_path)
    session = FakeFlushSession()
    session.replaces_output_snapshots = True
    state.active_agent = session
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=app_settings,
        state=state,
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    task = asyncio.create_task(bridge._flush_agent_output(bot_api, 100, session))
    try:
        await session.output_queue.put("old prompt\nold output\n")
        await asyncio.sleep(0.01)
        await session.output_queue.put("new output\n")
        for _ in range(50):
            if non_dashboard_messages(bot_api.messages) == ["new output"]:
                break
            await asyncio.sleep(0.005)
    finally:
        session.is_running = False
        await asyncio.wait_for(task, timeout=1)

    assert non_dashboard_messages(bot_api.messages) == ["new output"]


async def test_terminal_progress_keeps_one_message_after_sixty_lines(tmp_path: Path) -> None:
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    text = "".join(f"line {number}\n" for number in range(1, 62))

    await bridge._update_terminal_progress(bot_api, 100, text)

    assert len(bot_api.messages) == 1
    assert progress_body(bot_api.messages[0]).splitlines() == [
        f"line {number}" for number in range(1, 61)
    ]


async def test_large_initial_chunk_sends_first_page_then_final_latest_page(tmp_path: Path) -> None:
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    text = "".join(f"LINE {number:03d}\n" for number in range(1, 151))

    await bridge._update_terminal_progress(bot_api, 100, text)

    assert len(bot_api.messages) == 1
    assert progress_body(bot_api.messages[0]).splitlines() == [
        f"LINE {number:03d}" for number in range(1, 61)
    ]

    await bridge._send_agent_output(bot_api, 100, text, complete_request=True)

    final_lines = progress_body(bot_api.messages[0]).splitlines()
    assert final_lines == [f"LINE {number:03d}" for number in range(91, 151)]
    assert "LINE 001" not in progress_body(bot_api.messages[0])
    assert {message_id for message_id, _text in bot_api.edits} == {1}


async def test_nonfinal_agent_output_uses_single_progress_message(tmp_path: Path) -> None:
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path, OUTPUT_FLUSH_INTERVAL_MS="1"),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    first_page = "".join(f"line {index}\n" for index in range(1, 61))
    second_page = "".join(f"line {index}\n" for index in range(61, 121))

    await bridge._send_agent_output(bot_api, 100, first_page)
    await asyncio.sleep(0.002)
    await bridge._send_agent_output(bot_api, 100, second_page)

    assert [progress_body(message) for message in bot_api.messages] == [second_page.strip()]
    assert [(message_id, progress_body(text)) for message_id, text in bot_api.edits] == [
        (1, second_page.strip())
    ]


async def test_agent_output_sends_referenced_screenshot_as_document_fallback(tmp_path: Path) -> None:
    screenshot_dir = tmp_path / "output" / "playwright"
    screenshot_dir.mkdir(parents=True)
    screenshot = screenshot_dir / "openai-com-2026-04-25.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    bot_api = FakeBot()
    bot_api.fail_photo = True
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )

    await bridge._send_agent_output(
        bot_api,
        100,
        "• Screenshot saved to output/playwright/openai-com-2026-04-25.png",
    )

    assert bot_api.photos == 1
    assert bot_api.documents == ["openai-com-2026-04-25.png"]


async def test_new_screenshot_artifact_is_sent_without_agent_text_reference(tmp_path: Path) -> None:
    screenshot_dir = tmp_path / ".playwright-cli"
    screenshot_dir.mkdir()
    screenshot = screenshot_dir / "page-2026-04-25T13-34-27-916Z.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    os.utime(screenshot, (10, 10))
    bot_api = FakeBot()
    bridge = TelegramBridgeBot(
        settings=settings(tmp_path),
        state=RuntimeState.create(tmp_path),
        sandbox=Sandbox(tmp_path, cat_max_bytes=1024, sendfile_max_bytes=1024),
        screenshot_service=ScreenshotService(
            workspace_root=tmp_path,
            screenshot_dir=None,
            max_bytes=1024,
        ),
        transcriber=DisabledTranscriber(),
    )
    bridge._screenshot_watch_since_by_chat[100] = screenshot.stat().st_mtime - 1

    await bridge._send_new_screenshots(bot_api, 100)

    assert bot_api.photos == 1
    assert bot_api.messages == ["Screenshot sent: page-2026-04-25T13-34-27-916Z.png"]
