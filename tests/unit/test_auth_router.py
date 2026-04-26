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


def test_interactive_choices_are_not_detected_from_raw_output() -> None:
    assert (
        detect_interactive_choices(
            "Select reasoning effort\n"
            "› low\n"
            "  medium\n"
            "  high\n"
            "  xhigh\n"
        )
        is None
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


def test_pending_action_creation_and_lookup(tmp_path: Path) -> None:
    state = RuntimeState.create(tmp_path)
    action = pending_action(
        kind="approval",
        session_id="codex",
        choices=(PendingActionChoice(id="approve", label="Approve"),),
        source_event_id="evt_1",
    )

    state.add_pending_action(action)

    assert state.pending_action(action.id) is action
    assert state.active_pending_action("approval") is action


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
        self.voice = None
        self.audio = None
        self.document = None

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.photos = 0
        self.documents: list[str | None] = []
        self.messages: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self.fail_photo = False
        self.commands = None

    async def send_chat_action(self, *, chat_id: int, action: str) -> None:
        return None

    async def send_message(self, *, chat_id: int, text: str, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, **kwargs) -> None:
        self.edits.append((message_id, text))
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
    return [message for message in messages if not message.startswith("Agent:")]


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
    def __init__(self, query: FakeCallbackQuery) -> None:
        self.callback_query = query
        self.effective_user = SimpleNamespace(id=42)
        self.effective_chat = SimpleNamespace(id=100, type="private")


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

    assert bot_api.messages == ["Done."]


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

    first_page = "".join(f"line {index}\n" for index in range(1, 61))
    second_page = "".join(f"line {index}\n" for index in range(61, 121))

    await bridge._update_terminal_progress(bot_api, 100, first_page)

    assert bot_api.messages == [first_page.strip()]
    assert bot_api.edits == []

    await bridge._update_terminal_progress(bot_api, 100, second_page)

    assert bot_api.messages == [second_page.strip()]
    assert bot_api.edits == [(1, second_page.strip())]


async def test_terminal_progress_final_output_updates_same_message_with_last_lines(
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
    final_text = "".join(f"line {index}\n" for index in range(1, 76))

    await bridge._update_terminal_progress(bot_api, 100, progress)
    await bridge._send_agent_output(bot_api, 100, final_text, complete_request=True)

    expected = "\n".join(f"line {index}" for index in range(16, 76))
    assert bot_api.messages == [expected]
    assert bot_api.edits[-1] == (1, expected)


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

    pending_voice = state.active_pending_action("voice_transcript")
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
    pending = state.add_pending_action(pending_voice_action_from_transcript("send wrong text"))
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

    assert state.active_pending_action("voice_transcript") is None
    assert context.bot.requested_file_id is None
    assert message.replies == []


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


async def test_unknown_slash_command_is_forwarded_to_agent(tmp_path: Path) -> None:
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

    assert state.active_agent.sent[-1].endswith("User request:\n/model gpt-5.5")


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

    assert state.active_agent.sent[-1].endswith("User request:\n/status")


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


async def test_terminal_progress_edits_current_sixty_line_page(tmp_path: Path) -> None:
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

    await bridge._update_terminal_progress(bot_api, 100, "line 1\n")
    await bridge._update_terminal_progress(bot_api, 100, "line 2\n")

    assert bot_api.messages == []
    assert bot_api.edits == []


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
    assert bot_api.messages[0].splitlines() == [f"line {number}" for number in range(1, 61)]


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
