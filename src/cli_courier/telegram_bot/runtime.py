from __future__ import annotations

import asyncio
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from cli_courier.agent.events import AgentEvent, AgentEventKind
from cli_courier.agent.adapters import get_adapter, list_adapters
from cli_courier.agent.approval import (
    ApprovalDecision,
    detect_pending_approval,
    has_auto_approval_marker,
    interpret_approval_text,
)
from cli_courier.agent.chunking import chunk_text
from cli_courier.agent.output_filter import agent_output_in_progress, prepare_agent_output
from cli_courier.agent.session import AgentSession
from cli_courier.config import AgentOutputMode, Settings, TranscriptionBackend, WhisperBackend
from cli_courier.filesystem import Sandbox, SandboxViolation
from cli_courier.screenshots import ScreenshotError, ScreenshotService
from cli_courier.security.terminal import safe_excerpt, sanitize_terminal_text
from cli_courier.state import (
    PendingAction,
    RuntimeState,
    pending_approval_action,
    pending_voice_action_from_transcript,
)
from cli_courier.telegram_bot.auth import TelegramIdentity, is_authorized, unauthorized_reply
from cli_courier.telegram_bot.commands import (
    BOT_COMMAND_SPECS,
    COMMAND_HELP,
    ParsedCommand,
    parse_command,
)
from cli_courier.telegram_bot.dashboard import DashboardSnapshot, render_dashboard
from cli_courier.telegram_bot.router import RouteKind, route_text
from cli_courier.voice import (
    DisabledTranscriber,
    FasterWhisperTranscriber,
    OpenAITranscriber,
    Transcriber,
    WhisperCppTranscriber,
    transcribe_with_cleanup,
)

TERMINAL_PROGRESS_PAGE_LINES = 60


@dataclass
class _TerminalProgressState:
    message_id: int | None = None
    lines: list[str] = field(default_factory=list)
    partial_line: str = ""
    completed_line_count: int = 0
    published_line_count: int = 0


class TelegramBridgeBot:
    def __init__(
        self,
        *,
        settings: Settings,
        state: RuntimeState,
        sandbox: Sandbox,
        screenshot_service: ScreenshotService,
        transcriber: Transcriber,
    ) -> None:
        self.settings = settings
        self.state = state
        self.sandbox = sandbox
        self.screenshot_service = screenshot_service
        self.transcriber = transcriber
        self._flush_tasks: set[asyncio.Task[None]] = set()
        self._typing_tasks: dict[int, asyncio.Task[None]] = {}
        self._last_approval_signature: str | None = None
        self._last_choice_signature: str | None = None
        self._screenshot_watch_since_by_chat: dict[int, float] = {}
        self._sent_screenshot_paths: set[Path] = set()
        self._agent_input_echoes_by_chat: dict[int, list[str]] = {}
        self._interactive_output_chats: set[int] = set()
        self._agent_context_sent = False
        self._dashboard_message_ids: dict[int, int] = {}
        self._dashboard_last_update: dict[int, float] = {}
        self._terminal_progress_by_chat: dict[int, _TerminalProgressState] = {}

    def build_application(self):
        from telegram.ext import (
            ApplicationBuilder,
            CallbackQueryHandler,
            MessageHandler,
            MessageReactionHandler,
            filters,
        )

        builder = ApplicationBuilder().token(self.settings.telegram_bot_token.get_secret_value())
        builder = builder.post_init(self._post_init)
        builder = builder.post_shutdown(self._post_shutdown)
        application = builder.build()
        application.add_handler(CallbackQueryHandler(self.handle_callback))
        application.add_handler(MessageReactionHandler(self.handle_reaction))
        application.add_handler(MessageHandler(filters.ALL, self.handle_update))
        return application

    async def _post_init(self, application) -> None:
        from telegram import BotCommand

        await application.bot.set_my_commands(
            [BotCommand(command=name, description=description) for name, description in BOT_COMMAND_SPECS]
        )
        if not self.settings.auto_start_agent:
            return
        if self.settings.default_telegram_chat_id is None:
            return
        if not self._notifications_muted():
            reachable = await self._safe_send_message(
                application.bot,
                chat_id=self.settings.default_telegram_chat_id,
                text="CliCourier connected. Starting agent...",
            )
            if reachable is None:
                print(
                    "auto-start skipped: DEFAULT_TELEGRAM_CHAT_ID is not reachable. "
                    "Open the bot in Telegram and send /start, or clear DEFAULT_TELEGRAM_CHAT_ID.",
                    flush=True,
                )
                return
        try:
            message = await self._start_agent_session(
                chat_id=self.settings.default_telegram_chat_id,
                application=application,
            )
        except Exception as exc:  # noqa: BLE001 - daemon log should capture startup failures
            print(f"failed to auto-start agent: {exc}", flush=True)
            return
        if not self._notifications_muted():
            await self._safe_send_message(
                application.bot,
                chat_id=self.settings.default_telegram_chat_id,
                text=message,
            )

    async def _post_shutdown(self, application) -> None:
        for task in list(self._flush_tasks):
            task.cancel()
        if self._flush_tasks:
            await asyncio.gather(*self._flush_tasks, return_exceptions=True)
            self._flush_tasks.clear()
        for task in list(self._typing_tasks.values()):
            task.cancel()
        if self._typing_tasks:
            await asyncio.gather(*self._typing_tasks.values(), return_exceptions=True)
            self._typing_tasks.clear()
        agent = self.state.active_agent
        if agent is not None:
            await agent.stop()
            self.state.active_agent = None

    async def handle_update(self, update, context) -> None:
        identity = self._identity(update)
        if not is_authorized(identity, self.settings):
            reply = unauthorized_reply(self.settings)
            if reply and update.effective_message is not None:
                await update.effective_message.reply_text(reply)
            return

        message = update.effective_message
        if message is None:
            return
        if self._audio_attachment(message) is not None:
            self._start_typing(context.bot, message.chat_id)
            application = getattr(context, "application", None)
            if application is not None:
                self._create_background_task(
                    application,
                    self._handle_voice(message, context, stop_typing=True),
                )
            else:
                await self._handle_voice(message, context, stop_typing=True)
            return
        if message.text is None:
            return
        if await self._maybe_handle_voice_correction(message.text, message):
            return
        if await self._maybe_handle_choice_reply(message.text, message, context):
            return

        route = route_text(
            message.text,
            has_pending_approval=self.state.active_pending_action("approval") is not None,
        )
        if route.kind == RouteKind.EMPTY:
            return
        if route.kind == RouteKind.COMMAND and route.command is not None:
            await self._handle_command(route.command, message, context)
        elif route.kind == RouteKind.APPROVAL and route.approval_decision is not None:
            await self._handle_approval(route.approval_decision, message, context)
        elif route.kind == RouteKind.BLOCKED_APPROVAL:
            await message.reply_text("No approval is pending. Use /agent yes to send that text anyway.")
        elif route.kind == RouteKind.AGENT_TEXT:
            await self._send_to_agent(route.text, message, context)

    async def handle_callback(self, update, context) -> None:
        query = update.callback_query
        if query is None:
            return
        identity = self._identity(update)
        if not is_authorized(identity, self.settings):
            await query.answer()
            return
        await query.answer()
        data = query.data or ""
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        prefix, action_id, choice_id = parts
        if prefix != "cc":
            return
        action = self.state.pending_action(action_id)
        if action is None:
            await query.edit_message_text("This action expired or is no longer pending.")
            return
        if action.choice(choice_id) is None:
            await query.edit_message_text("This action choice is no longer valid.")
            return
        if action.kind == "approval":
            await self._handle_pending_approval_callback(action, choice_id, query, context)
        elif action.kind == "voice_transcript":
            await self._handle_pending_voice_callback(action, choice_id, query, context)
        elif action.kind == "choice_request":
            await query.edit_message_text("This choice request is no longer supported in Telegram.")

    async def handle_reaction(self, update, context) -> None:
        reaction_update = update.message_reaction
        if reaction_update is None:
            return
        identity = self._identity(update)
        if not is_authorized(identity, self.settings):
            return

        pending = self.state.active_pending_action("approval")
        if pending is None:
            return
        if pending.message_id is not None and reaction_update.message_id != pending.message_id:
            return

        decision = approval_decision_from_reactions(reaction_update.new_reaction)
        if decision is None:
            return
        try:
            await self._apply_approval_action(pending, decision)
        except RuntimeError as exc:
            await self._safe_send_message(
                context.bot,
                chat_id=reaction_update.chat.id,
                text=str(exc),
            )
            return
        await self._safe_send_message(
            context.bot,
            chat_id=reaction_update.chat.id,
            text=f"Sent {decision}.",
        )

    async def _handle_command(self, command: ParsedCommand, message, context) -> None:
        handlers = {
            "botstatus": self._cmd_status,
            "start_agent": self._cmd_start_agent,
            "stop_agent": self._cmd_stop_agent,
            "restart_agent": self._cmd_restart_agent,
            "agent": self._cmd_agent,
            "agents": self._cmd_agents,
            "pwd": self._cmd_pwd,
            "ls": self._cmd_ls,
            "tree": self._cmd_tree,
            "cd": self._cmd_cd,
            "cat": self._cmd_cat,
            "sendfile": self._cmd_sendfile,
            "screenshot": self._cmd_screenshot,
            "artifacts": self._cmd_artifacts,
            "tail": self._cmd_tail,
            "log": self._cmd_tail,
            "sendlog": self._cmd_sendlog,
            "stream": self._cmd_stream,
            "final": self._cmd_final,
            "trace_on": self._cmd_trace_on,
            "trace_off": self._cmd_trace_off,
            "approve": self._cmd_approve,
            "reject": self._cmd_reject,
            "voice_approve": self._cmd_voice_approve,
            "voice_reject": self._cmd_voice_reject,
            "voice_edit": self._cmd_voice_edit,
            "mute": self._cmd_mute,
            "unmute": self._cmd_unmute,
            "desktop": self._cmd_mute,
            "telegram": self._cmd_unmute,
            "mute_status": self._cmd_mute_status,
            "bothelp": self._cmd_help,
            "start": self._cmd_help,
        }
        handler = handlers.get(command.name)
        if handler is None:
            forwarded = f"/{command.name}"
            if command.args:
                forwarded = f"{forwarded} {command.args}"
            await self._send_to_agent(forwarded, message, context)
            return
        await handler(command.args, message, context)

    async def _cmd_status(self, args: str, message, context) -> None:
        agent = self.state.active_agent
        if agent is None:
            agent_status = "agent: stopped"
        else:
            status = agent.status()
            agent_status = (
                f"agent: {'running' if status.running else 'stopped'}\n"
                f"adapter: {status.adapter_name}\n"
                f"mode: {status.mode}\n"
                f"state: {status.state}\n"
                f"command: {' '.join(status.command)}"
            )
        pending = "yes" if self.state.active_pending_action("approval") else "no"
        muted = "yes" if self._notifications_muted() else "no"
        await message.reply_text(
            f"{agent_status}\n"
            f"workspace: {self.sandbox.display_path(self.state.cwd)}\n"
            f"pending approval: {pending}\n"
            f"output mode: {self.settings.agent_output_mode.value}\n"
            f"muted: {muted}"
        )

    async def _cmd_start_agent(self, args: str, message, context) -> None:
        if self.state.active_agent is not None and self.state.active_agent.is_running:
            await message.reply_text("Agent is already running.")
            return
        try:
            reply = await self._start_agent_session(
                chat_id=message.chat_id,
                application=context.application,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to trusted operator
            await message.reply_text(f"Failed to start agent: {exc}")
            return
        await message.reply_text(reply)

    async def _start_agent_session(self, *, chat_id: int, application=None, bot=None) -> str:
        adapter = get_adapter(self.settings.default_agent_adapter)
        command = adapter.build_command(self.settings.default_agent_command)
        session = AgentSession(
            adapter=adapter,
            command=command,
            cwd=self.settings.workspace_root,
            recent_output_max_chars=self.settings.recent_output_max_chars,
            env_allowlist=self.settings.agent_env_allowlist,
            terminal_backend=self.settings.agent_terminal_backend.value,
            tmux_session_name=self.settings.agent_tmux_session,
            tmux_history_lines=self.settings.agent_tmux_history_lines,
        )
        await session.start()
        self.state.active_agent = session
        self.state.agent_chat_id = chat_id
        self._last_approval_signature = None
        self._last_choice_signature = None
        self._agent_context_sent = False
        self.state.clear_pending_actions()
        target_bot = application.bot if application is not None else bot
        if target_bot is None:
            raise RuntimeError("Telegram bot context is not available.")
        task = self._create_background_task(
            application,
            self._flush_agent_output(target_bot, chat_id, session),
        )
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)
        return f"Agent started: {' '.join(command)}"

    async def _cmd_stop_agent(self, args: str, message, context) -> None:
        agent = self.state.active_agent
        if agent is None:
            await message.reply_text("Agent is not running.")
            return
        await agent.stop()
        self.state.active_agent = None
        self.state.clear_pending_approval()
        self.state.clear_pending_choice()
        self.state.clear_pending_actions()
        await message.reply_text("Agent stopped.")

    async def _cmd_restart_agent(self, args: str, message, context) -> None:
        if self.state.active_agent is not None:
            await self.state.active_agent.stop()
            self.state.active_agent = None
        await self._cmd_start_agent(args, message, context)

    async def _cmd_agent(self, args: str, message, context) -> None:
        if not args:
            await message.reply_text("Usage: /agent <text>")
            return
        await self._send_to_agent(args, message, context)

    async def _cmd_agents(self, args: str, message, context) -> None:
        lines = [
            f"{adapter_id}: {adapter.display_name}"
            for adapter_id, adapter in sorted(list_adapters().items())
        ]
        await message.reply_text("\n".join(lines))

    async def _cmd_pwd(self, args: str, message, context) -> None:
        await message.reply_text(self.sandbox.display_path(self.state.cwd))

    async def _cmd_ls(self, args: str, message, context) -> None:
        try:
            entries = self.sandbox.list_dir(args or ".", cwd=self.state.cwd)
        except SandboxViolation as exc:
            await message.reply_text(str(exc))
            return
        body = "\n".join(entry.display_name for entry in entries) or "(empty)"
        await self._reply_chunks(message, body)

    async def _cmd_tree(self, args: str, message, context) -> None:
        try:
            body = self.sandbox.tree(args or ".", cwd=self.state.cwd)
        except SandboxViolation as exc:
            await message.reply_text(str(exc))
            return
        await self._reply_chunks(message, body)

    async def _cmd_cd(self, args: str, message, context) -> None:
        if not args:
            await message.reply_text("Usage: /cd <path>")
            return
        try:
            path = self.sandbox.resolve(args, cwd=self.state.cwd)
            if not path.is_dir():
                raise SandboxViolation("path is not a directory")
        except SandboxViolation as exc:
            await message.reply_text(str(exc))
            return
        self.state.set_cwd(path)
        await message.reply_text(self.sandbox.display_path(path))

    async def _cmd_cat(self, args: str, message, context) -> None:
        if not args:
            await message.reply_text("Usage: /cat <path>")
            return
        try:
            body = self.sandbox.cat_file(args, cwd=self.state.cwd)
        except SandboxViolation as exc:
            await message.reply_text(str(exc))
            return
        await self._reply_chunks(message, body or "(empty)")

    async def _cmd_sendfile(self, args: str, message, context) -> None:
        if not args:
            await message.reply_text("Usage: /sendfile <path>")
            return
        try:
            path = self.sandbox.validate_sendfile(args, cwd=self.state.cwd)
        except SandboxViolation as exc:
            await message.reply_text(str(exc))
            return
        with path.open("rb") as file_obj:
            await message.reply_document(document=file_obj, filename=path.name)

    async def _cmd_screenshot(self, args: str, message, context) -> None:
        try:
            artifact = self.screenshot_service.latest()
        except ScreenshotError as exc:
            await message.reply_text(str(exc))
            return
        with artifact.path.open("rb") as file_obj:
            await message.reply_document(
                document=file_obj,
                filename=artifact.path.name,
                caption="Latest screenshot",
            )

    async def _cmd_artifacts(self, args: str, message, context) -> None:
        try:
            artifacts = self.screenshot_service.recent_artifacts(limit=10)
        except ScreenshotError as exc:
            await message.reply_text(str(exc))
            return
        if not artifacts:
            await message.reply_text("No screenshot artifacts found.")
            return
        lines = []
        for artifact in artifacts:
            try:
                display = artifact.path.relative_to(self.settings.workspace_root)
            except ValueError:
                display = artifact.path
            lines.append(f"{display} ({artifact.size} bytes)")
        await message.reply_text("\n".join(lines))

    async def _cmd_tail(self, args: str, message, context) -> None:
        agent = self.state.active_agent
        if agent is None:
            await message.reply_text("Agent is not running.")
            return
        try:
            limit = int(args.strip()) if args.strip() else 4000
        except ValueError:
            await message.reply_text("Usage: /tail [chars]")
            return
        await self._reply_chunks(message, agent.recent_output(max(1, limit)) or "(empty)")

    async def _cmd_sendlog(self, args: str, message, context) -> None:
        agent = self.state.active_agent
        if agent is None:
            await message.reply_text("Agent is not running.")
            return
        text = agent.recent_output(self.settings.recent_output_max_chars) or "(empty)"
        path = _write_temp_log(text)
        try:
            with path.open("rb") as file_obj:
                await message.reply_document(document=file_obj, filename=path.name)
        finally:
            path.unlink(missing_ok=True)

    async def _cmd_stream(self, args: str, message, context) -> None:
        self.settings.agent_output_mode = AgentOutputMode.STREAM
        await message.reply_text("Agent output streaming enabled.")

    async def _cmd_final(self, args: str, message, context) -> None:
        self.settings.agent_output_mode = AgentOutputMode.FINAL
        await message.reply_text("Agent output final-only mode enabled.")

    async def _cmd_trace_on(self, args: str, message, context) -> None:
        self.settings.suppress_agent_trace_lines = False
        await message.reply_text("Reasoning/tool/status lines will be forwarded.")

    async def _cmd_trace_off(self, args: str, message, context) -> None:
        self.settings.suppress_agent_trace_lines = True
        await message.reply_text("Reasoning/tool/status lines will be suppressed.")

    async def _cmd_approve(self, args: str, message, context) -> None:
        await self._handle_approval("approve", message, context)

    async def _cmd_reject(self, args: str, message, context) -> None:
        await self._handle_approval("reject", message, context)

    async def _cmd_voice_approve(self, args: str, message, context) -> None:
        pending = self.state.active_pending_action("voice_transcript")
        if pending is None:
            await message.reply_text("No voice transcript is pending.")
            return
        try:
            self._start_typing(context.bot, message.chat_id)
            await self._send_to_agent_text(str(pending.data.get("transcript", "")), chat_id=message.chat_id)
        except RuntimeError as exc:
            await message.reply_text(str(exc))
            return
        self.state.clear_pending_action(pending.id)
        await message.reply_text("Transcript sent.")

    async def _cmd_voice_reject(self, args: str, message, context) -> None:
        self.state.clear_pending_actions(kind="voice_transcript")
        await message.reply_text("Transcript discarded.")

    async def _cmd_voice_edit(self, args: str, message, context) -> None:
        if not args:
            await message.reply_text("Usage: /voice_edit <text>")
            return
        self.state.clear_pending_actions(kind="voice_transcript")
        self.state.add_pending_action(pending_voice_action_from_transcript(args))
        await message.reply_text("Transcript updated. Use /voice_approve to send it.")

    async def _maybe_handle_voice_correction(self, text: str, message) -> bool:
        pending = self.state.active_pending_action("voice_transcript")
        if pending is None:
            return False
        if parse_command(text) is not None:
            return False
        corrected = text.strip()
        if not corrected:
            return False
        pending.data["transcript"] = corrected
        await message.reply_text("Transcript updated. Tap Send or use /voice_approve to send it.")
        return True

    async def _cmd_mute(self, args: str, message, context) -> None:
        path = self.settings.notification_block_file.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("muted\n", encoding="utf-8")
        await message.reply_text(f"Muted proactive agent output via {path}.")

    async def _cmd_unmute(self, args: str, message, context) -> None:
        path = self.settings.notification_block_file.expanduser()
        path.unlink(missing_ok=True)
        await message.reply_text("Proactive agent output unmuted.")

    async def _cmd_mute_status(self, args: str, message, context) -> None:
        await message.reply_text("Muted." if self._notifications_muted() else "Not muted.")

    async def _cmd_help(self, args: str, message, context) -> None:
        await message.reply_text(COMMAND_HELP)

    async def _handle_voice(self, message, context, *, stop_typing: bool = False) -> None:
        attachment = self._audio_attachment(message)
        if attachment is None:
            return
        try:
            audio, suffix = attachment
            if audio.file_size and audio.file_size > self.settings.voice_max_bytes:
                await message.reply_text("Audio message is too large.")
                return
            if isinstance(self.transcriber, DisabledTranscriber):
                await message.reply_text("Voice transcription is disabled.")
                return

            try:
                with tempfile.TemporaryDirectory(prefix="cli-courier-voice-") as temp_dir:
                    temp_path = Path(temp_dir) / f"{audio.file_unique_id}{suffix}"
                    telegram_file = await context.bot.get_file(audio.file_id)
                    await telegram_file.download_to_drive(custom_path=temp_path)
                    if temp_path.stat().st_size > self.settings.voice_max_bytes:
                        await message.reply_text("Audio message is too large.")
                        return
                    transcript = await transcribe_with_cleanup(self.transcriber, temp_path)
            except Exception as exc:  # noqa: BLE001 - surfaced to trusted operator
                await message.reply_text(f"Voice transcription failed: {exc}")
                return

            self.state.clear_pending_actions(kind="voice_transcript")
            self.state.add_pending_action(pending_voice_action_from_transcript(transcript))
            await self._send_voice_confirmation(message, transcript)
        finally:
            if stop_typing:
                self._stop_typing(message.chat_id)

    def _audio_attachment(self, message) -> tuple[object, str] | None:
        voice = getattr(message, "voice", None)
        if voice is not None:
            return voice, ".oga"
        audio = getattr(message, "audio", None)
        if audio is not None:
            return audio, _audio_suffix(
                getattr(audio, "file_name", None),
                getattr(audio, "mime_type", None),
            )
        document = getattr(message, "document", None)
        if document is not None and _document_is_audio(document):
            return document, _audio_suffix(
                getattr(document, "file_name", None),
                getattr(document, "mime_type", None),
            )
        return None

    async def _send_voice_confirmation(self, message, transcript: str) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        pending = self.state.active_pending_action("voice_transcript")
        assert pending is not None
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Send", callback_data=f"cc:{pending.id}:send"),
                    InlineKeyboardButton("Cancel", callback_data=f"cc:{pending.id}:cancel"),
                    InlineKeyboardButton("Edit", callback_data=f"cc:{pending.id}:edit"),
                ]
            ]
        )
        sent = await message.reply_text(
            f"Transcript:\n{transcript}\n\nReply with corrected text to change it.",
            reply_markup=keyboard,
        )
        if sent is not None:
            pending.message_id = getattr(sent, "message_id", None)

    async def _send_to_agent(self, text: str, message, context) -> None:
        self._start_typing(context.bot, message.chat_id)
        try:
            await self._send_to_agent_text(
                text,
                chat_id=message.chat_id,
                application=getattr(context, "application", None),
                bot=getattr(context, "bot", None),
            )
        except RuntimeError as exc:
            self._stop_typing(message.chat_id)
            await message.reply_text(str(exc))
            return
        if self._notifications_muted():
            await message.reply_text("Request sent. Proactive agent output is muted; use /telegram to resume.")

    async def _send_to_agent_text(
        self,
        text: str,
        *,
        chat_id: int | None = None,
        application=None,
        bot=None,
    ) -> None:
        agent = self.state.active_agent
        if agent is None or not agent.is_running:
            if chat_id is None:
                raise RuntimeError("Agent is not running.")
            try:
                await self._start_agent_session(chat_id=chat_id, application=application, bot=bot)
            except Exception as exc:  # noqa: BLE001 - surfaced to trusted operator
                raise RuntimeError(f"Failed to start agent: {exc}") from exc
            agent = self.state.active_agent
            if agent is None or not agent.is_running:
                raise RuntimeError("Agent is not running.")
        user_text = self._agent_user_text(text)
        if chat_id is not None:
            self._interactive_output_chats.add(chat_id)
            self._screenshot_watch_since_by_chat[chat_id] = time.time()
            self._remember_agent_input_echo(chat_id, text)
            self._remember_agent_input_echo(chat_id, user_text)
        await agent.send_text(user_text)

    async def _handle_approval(self, decision: ApprovalDecision, message, context) -> None:
        pending = self.state.active_pending_action("approval")
        if pending is None:
            await message.reply_text("No approval is pending.")
            return
        self._start_typing(context.bot, message.chat_id)
        await self._apply_approval_action(pending, decision)
        await message.reply_text(f"Sent {decision}.")

    async def _apply_approval_action(self, action: PendingAction, decision: ApprovalDecision) -> None:
        agent = self.state.active_agent
        if agent is None:
            raise RuntimeError("No approval is pending.")
        text = agent.adapter.approve_input if decision == "approve" else agent.adapter.reject_input
        await agent.send_approval(text)
        self.state.clear_pending_action(action.id)
        await agent.output_queue.put(
            AgentEvent(
                kind=AgentEventKind.APPROVAL_RESOLVED,
                text=f"Approval {decision}.",
                session_id=action.session_id,
                data={"decision": decision},
            )
        )

    async def _handle_pending_approval_callback(
        self,
        action: PendingAction,
        choice_id: str,
        query,
        context,
    ) -> None:
        chat_id = query.message.chat_id
        if choice_id in {"approve", "reject"}:
            decision: ApprovalDecision = "approve" if choice_id == "approve" else "reject"
            try:
                self._start_typing(context.bot, chat_id)
                await self._apply_approval_action(action, decision)
            except RuntimeError as exc:
                await query.edit_message_text(str(exc))
                return
            await query.edit_message_text(f"Sent {decision}.")
            return
        if choice_id == "details":
            await self._safe_send_message(
                context.bot,
                chat_id=chat_id,
                text=f"Approval details:\n{action.data.get('prompt', '')}",
            )
            return
        if choice_id == "sendlog":
            await self._send_recent_log_document(context.bot, chat_id)

    async def _handle_pending_voice_callback(
        self,
        action: PendingAction,
        choice_id: str,
        query,
        context,
    ) -> None:
        chat_id = query.message.chat_id
        transcript = str(action.data.get("transcript", "")).strip()
        if choice_id == "send":
            try:
                self._start_typing(context.bot, chat_id)
                await self._send_to_agent_text(
                    transcript,
                    chat_id=chat_id,
                    application=getattr(context, "application", None),
                    bot=getattr(context, "bot", None),
                )
            except RuntimeError as exc:
                await query.edit_message_text(str(exc))
                return
            self.state.clear_pending_action(action.id)
            await query.edit_message_text("Transcript sent.")
            return
        if choice_id == "cancel":
            self.state.clear_pending_action(action.id)
            await query.edit_message_text("Transcript discarded.")
            return
        if choice_id == "edit":
            await self._safe_send_message(
                context.bot,
                chat_id=chat_id,
                text="Reply with the corrected transcript text.",
            )

    async def _flush_agent_output(self, bot, chat_id: int, session: AgentSession) -> None:
        pending_text = ""
        first_output_at: float | None = None
        last_output_at: float | None = None
        interval = self.settings.output_flush_interval_ms / 1000
        while self.state.active_agent is session and session.is_running:
            try:
                event = await asyncio.wait_for(session.output_queue.get(), timeout=interval)
                if isinstance(event, str):
                    event = AgentEvent(kind=AgentEventKind.ASSISTANT_DELTA, text=event)
                if event.kind == AgentEventKind.FINAL_MESSAGE:
                    final_text = sanitize_terminal_text(event.text).strip()
                    final_text = self._suppress_agent_input_echoes(chat_id, final_text)
                    if pending_text:
                        buffered_text = prepare_agent_output(
                            pending_text,
                            suppress_trace_lines=self.settings.suppress_agent_trace_lines,
                        )
                        buffered_text = self._suppress_agent_input_echoes(chat_id, buffered_text)
                        final_text = self._select_complete_output(buffered_text, final_text)
                    self._record_session_event(session, event)
                    await self._maybe_update_dashboard(bot, chat_id, session, force=True)
                    await self._send_agent_output(bot, chat_id, final_text, complete_request=True)
                    self._stop_typing(chat_id)
                    pending_text = ""
                    first_output_at = None
                    last_output_at = None
                    continue
                await self._handle_agent_event(bot, chat_id, session, event)
                if event.kind in {AgentEventKind.TURN_COMPLETED, AgentEventKind.TURN_FAILED, AgentEventKind.ERROR}:
                    if not self._approval_pending():
                        self._interactive_output_chats.discard(chat_id)
                if event.kind != AgentEventKind.ASSISTANT_DELTA:
                    await self._maybe_update_dashboard(bot, chat_id, session)
                    continue
                progress_text = sanitize_terminal_text(event.text)
                progress_text = self._suppress_agent_input_echoes(chat_id, progress_text)
                await self._update_terminal_progress(bot, chat_id, progress_text)
                if session.replaces_output_snapshots:
                    pending_text = event.text
                else:
                    pending_text += event.text
                now = asyncio.get_running_loop().time()
                first_output_at = first_output_at or now
                last_output_at = now
                await self._maybe_emit_fallback_approval(bot, chat_id, session)
            except asyncio.TimeoutError:
                pass
            await self._send_new_screenshots(bot, chat_id)
            await self._maybe_update_dashboard(bot, chat_id, session)
            if not pending_text:
                continue
            if self.settings.agent_output_mode == AgentOutputMode.STREAM:
                if len(pending_text) < self.settings.max_telegram_chunk_chars and not session.output_queue.empty():
                    continue
                if self._approval_pending():
                    continue
                if not agent_output_in_progress(pending_text):
                    stream_text = prepare_agent_output(
                        pending_text,
                        suppress_trace_lines=self.settings.suppress_agent_trace_lines,
                    )
                    stream_text = self._suppress_agent_input_echoes(chat_id, stream_text)
                    await self._send_agent_output(bot, chat_id, stream_text)
                pending_text = ""
                first_output_at = None
                last_output_at = None
                continue

            loop_now = asyncio.get_running_loop().time()
            idle_seconds = self.settings.final_output_idle_ms / 1000
            max_wait_seconds = self.settings.final_output_max_wait_ms / 1000
            idle = last_output_at is not None and loop_now - last_output_at >= idle_seconds
            waited = first_output_at is not None and loop_now - first_output_at >= max_wait_seconds
            if idle or waited:
                if agent_output_in_progress(pending_text):
                    if waited:
                        first_output_at = loop_now
                    continue
                if self._approval_pending():
                    if waited:
                        first_output_at = loop_now
                    continue
                final_text = prepare_agent_output(
                    pending_text,
                    suppress_trace_lines=self.settings.suppress_agent_trace_lines,
                )
                final_text = self._suppress_agent_input_echoes(chat_id, final_text)
                await self._send_agent_output(bot, chat_id, final_text, complete_request=True)
                self._stop_typing(chat_id)
                pending_text = ""
                first_output_at = None
                last_output_at = None
        if pending_text:
            final_text = prepare_agent_output(
                pending_text,
                suppress_trace_lines=self.settings.suppress_agent_trace_lines,
            )
            final_text = self._suppress_agent_input_echoes(chat_id, final_text)
            await self._send_agent_output(bot, chat_id, final_text, complete_request=True)
            self._stop_typing(chat_id)

    async def _handle_agent_event(
        self,
        bot,
        chat_id: int,
        session: AgentSession,
        event: AgentEvent,
    ) -> None:
        if self._chat_notifications_suppressed(chat_id):
            return
        if event.kind == AgentEventKind.APPROVAL_REQUESTED:
            await self._send_approval_event(bot, chat_id, session, event)
            return
        if event.kind in {AgentEventKind.ERROR, AgentEventKind.TURN_FAILED, AgentEventKind.TOOL_FAILED}:
            await self._safe_send_message(
                bot,
                chat_id=chat_id,
                text=f"Error: {event.display_text()}",
            )
            if event.kind in {AgentEventKind.ERROR, AgentEventKind.TURN_FAILED}:
                self._interactive_output_chats.discard(chat_id)
            self._stop_typing(chat_id)
            return
        if event.kind == AgentEventKind.SCREENSHOT_AVAILABLE:
            await self._send_screenshot_event(bot, chat_id, event)
            return
        if event.kind == AgentEventKind.ARTIFACT_AVAILABLE:
            await self._send_artifact_event(bot, chat_id, event)
            return
        if (
            event.kind in {AgentEventKind.REASONING, AgentEventKind.TOOL_DELTA, AgentEventKind.STATUS}
            and not self.settings.suppress_agent_trace_lines
            and event.text.strip()
        ):
            await self._send_agent_output(bot, chat_id, event.text)

    async def _maybe_emit_fallback_approval(self, bot, chat_id: int, session: AgentSession) -> None:
        if self._chat_notifications_suppressed(chat_id):
            return
        if session.backend == "structured" or session.adapter.capabilities.supports_approval_events:
            return
        recent_output = session.recent_output(4000)
        if has_auto_approval_marker(recent_output):
            self.state.clear_pending_actions(kind="approval")
            self._last_approval_signature = None
            return
        current = self.state.active_pending_action("approval")
        if current is not None:
            return
        pending = detect_pending_approval(recent_output, session.adapter)
        if pending is None:
            return
        if pending.prompt_excerpt == self._last_approval_signature:
            return
        self._last_approval_signature = pending.prompt_excerpt
        await self._send_approval_event(
            bot,
            chat_id,
            session,
            AgentEvent(
                kind=AgentEventKind.APPROVAL_REQUESTED,
                text=pending.prompt_excerpt,
                session_id=session.adapter.id,
            ),
        )

    async def _send_approval_event(
        self,
        bot,
        chat_id: int,
        session: AgentSession,
        event: AgentEvent,
    ) -> None:
        prompt = safe_excerpt(event.text or event.display_text(), 1200)
        if self.state.active_pending_action("approval", session_id=event.session_id) is not None:
            return
        action = pending_approval_action(
            session_id=event.session_id or session.adapter.id,
            source_event_id=event.event_id,
            prompt=prompt,
            data=event.data,
        )
        self.state.add_pending_action(action)
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"cc:{action.id}:approve"),
                    InlineKeyboardButton("Reject", callback_data=f"cc:{action.id}:reject"),
                ],
                [
                    InlineKeyboardButton("Details", callback_data=f"cc:{action.id}:details"),
                    InlineKeyboardButton("Send log", callback_data=f"cc:{action.id}:sendlog"),
                ],
            ]
        )
        sent = await self._safe_send_message(
            bot,
            chat_id=chat_id,
            text=f"Approval required:\n{prompt}",
            reply_markup=keyboard,
        )
        self._stop_typing(chat_id)
        if sent is not None:
            action.message_id = sent.message_id

    async def _send_screenshot_event(self, bot, chat_id: int, event: AgentEvent) -> None:
        reference = event.screenshot_path or event.artifact_path
        if reference:
            sent, error = await self._send_screenshot_for_output(bot, chat_id, reference)
            if sent:
                return
            if error:
                await self._safe_send_message(
                    bot,
                    chat_id=chat_id,
                    text=f"Screenshot detected, but sending failed: {error}",
                )
                return
        await self._send_latest_screenshot(bot, chat_id)

    async def _send_artifact_event(self, bot, chat_id: int, event: AgentEvent) -> None:
        path = event.artifact_path
        if not path:
            await self._safe_send_message(bot, chat_id=chat_id, text=event.display_text())
            return
        try:
            safe_path = self.sandbox.validate_sendfile(path, cwd=self.state.cwd)
        except SandboxViolation as exc:
            await self._safe_send_message(
                bot,
                chat_id=chat_id,
                text=f"Artifact available, but sending failed: {exc}",
            )
            return
        try:
            with safe_path.open("rb") as file_obj:
                await bot.send_document(
                    chat_id=chat_id,
                    document=file_obj,
                    filename=safe_path.name,
                )
        except Exception as exc:  # noqa: BLE001 - Telegram errors should not crash the bridge
            await self._safe_send_message(
                bot,
                chat_id=chat_id,
                text=f"Artifact available, but sending failed: {exc}",
            )

    async def _send_recent_log_document(self, bot, chat_id: int) -> None:
        agent = self.state.active_agent
        if agent is None:
            await self._safe_send_message(bot, chat_id=chat_id, text="Agent is not running.")
            return
        path = _write_temp_log(agent.recent_output(self.settings.recent_output_max_chars) or "(empty)")
        try:
            with path.open("rb") as file_obj:
                await bot.send_document(
                    chat_id=chat_id,
                    document=file_obj,
                    filename=path.name,
                )
        except Exception as exc:  # noqa: BLE001 - Telegram errors should not crash bridge
            await self._safe_send_message(
                bot,
                chat_id=chat_id,
                text=f"Sending log failed: {exc}",
            )
        finally:
            path.unlink(missing_ok=True)

    async def _maybe_update_dashboard(
        self,
        bot,
        chat_id: int,
        session: AgentSession,
        *,
        force: bool = False,
    ) -> None:
        if self._notifications_muted():
            return
        loop_now = asyncio.get_running_loop().time()
        last_update = self._dashboard_last_update.get(chat_id, 0)
        if not force and loop_now - last_update < 1.5:
            return
        status = session.status()
        tail = prepare_agent_output(
            session.recent_visible_output(2200),
            suppress_trace_lines=True,
        )
        text = render_dashboard(
            DashboardSnapshot(
                agent_name=status.adapter_name,
                state=status.state,
                cwd=str(session.cwd),
                current_phase=status.current_tool or status.state,
                last_event=status.last_event,
                output_tail=tail,
            )
        )
        message_id = self._dashboard_message_ids.get(chat_id)
        if message_id is None:
            sent = await self._safe_send_message(bot, chat_id=chat_id, text=text)
            if sent is not None:
                self._dashboard_message_ids[chat_id] = sent.message_id
                self._dashboard_last_update[chat_id] = loop_now
            return
        edit_message_text = getattr(bot, "edit_message_text", None)
        if edit_message_text is None:
            return
        try:
            await edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            self._dashboard_last_update[chat_id] = loop_now
        except Exception as exc:  # noqa: BLE001 - Telegram edit failures should not crash bridge
            print(f"telegram dashboard edit failed: {exc}", flush=True)

    async def _send_agent_output(
        self,
        bot,
        chat_id: int,
        text: str,
        *,
        complete_request: bool = False,
    ) -> None:
        if self._chat_notifications_suppressed(chat_id) or not text.strip():
            return
        if looks_like_screenshot_reference(text):
            sent, error = await self._send_screenshot_for_output(bot, chat_id, text)
            if sent:
                if complete_request:
                    self._clear_terminal_progress(chat_id)
                    self._interactive_output_chats.discard(chat_id)
                return
            if error:
                await self._safe_send_message(
                    bot,
                    chat_id=chat_id,
                    text=f"Screenshot detected, but sending failed: {error}",
                )
        if complete_request:
            if await self._publish_complete_output(bot, chat_id, text):
                self._interactive_output_chats.discard(chat_id)
                return
        for chunk in chunk_text(text, self.settings.max_telegram_chunk_chars):
            await self._safe_send_message(bot, chat_id=chat_id, text=chunk)
        if complete_request:
            self._clear_terminal_progress(chat_id)
            self._interactive_output_chats.discard(chat_id)

    async def _update_terminal_progress(self, bot, chat_id: int, text: str) -> None:
        if self._chat_notifications_suppressed(chat_id) or not text:
            return
        state = self._terminal_progress_by_chat.setdefault(chat_id, _TerminalProgressState())
        for segment in text.splitlines(keepends=True):
            if segment.endswith(("\n", "\r")):
                state.lines.append((state.partial_line + segment).rstrip("\r\n"))
                state.completed_line_count += 1
                del state.lines[:-TERMINAL_PROGRESS_PAGE_LINES]
                state.partial_line = ""
                if state.completed_line_count - state.published_line_count >= TERMINAL_PROGRESS_PAGE_LINES:
                    await self._publish_terminal_progress_page(bot, chat_id, state)
            else:
                state.partial_line += segment

    async def _publish_terminal_progress_page(
        self,
        bot,
        chat_id: int,
        state: _TerminalProgressState,
    ) -> None:
        text = "\n".join(state.lines[-TERMINAL_PROGRESS_PAGE_LINES:]).strip()
        if not text:
            return
        if len(text) > self.settings.max_telegram_chunk_chars:
            text = text[-self.settings.max_telegram_chunk_chars :]
        state.published_line_count = state.completed_line_count
        if state.message_id is None:
            sent = await self._safe_send_message(bot, chat_id=chat_id, text=text)
            if sent is not None:
                state.message_id = sent.message_id
            return
        edit_message_text = getattr(bot, "edit_message_text", None)
        if edit_message_text is None:
            return
        try:
            await edit_message_text(chat_id=chat_id, message_id=state.message_id, text=text)
        except Exception as exc:  # noqa: BLE001 - Telegram edit failures should not crash bridge
            print(f"telegram terminal progress edit failed: {exc}", flush=True)

    async def _publish_complete_output(self, bot, chat_id: int, text: str) -> bool:
        lines = sanitize_terminal_text(text).splitlines()
        if not lines and text.strip():
            lines = [text.strip()]
        if not lines:
            return False
        state = self._terminal_progress_by_chat.setdefault(chat_id, _TerminalProgressState())
        state.lines = lines[-TERMINAL_PROGRESS_PAGE_LINES:]
        state.partial_line = ""
        state.completed_line_count = len(lines)
        state.published_line_count = len(lines)
        await self._publish_terminal_progress_page(bot, chat_id, state)
        return True

    def _clear_terminal_progress(self, chat_id: int) -> None:
        self._terminal_progress_by_chat.pop(chat_id, None)

    async def _send_latest_screenshot(self, bot, chat_id: int) -> bool:
        try:
            artifact = self.screenshot_service.latest()
        except ScreenshotError as exc:
            print(f"telegram screenshot lookup failed: {exc}", flush=True)
            return False
        sent, error = await self._send_screenshot_artifact(bot, chat_id, artifact)
        if error:
            print(f"telegram screenshot send failed: {error}", flush=True)
        return sent

    async def _send_screenshot_for_output(self, bot, chat_id: int, text: str) -> tuple[bool, str | None]:
        reference = extract_screenshot_reference(text)
        try:
            artifact = (
                self.screenshot_service.artifact_for_reference(reference)
                if reference is not None
                else self.screenshot_service.latest()
            )
        except ScreenshotError as exc:
            return False, str(exc)
        sent, error = await self._send_screenshot_artifact(bot, chat_id, artifact)
        if sent:
            self._sent_screenshot_paths.add(artifact.path)
        return sent, error

    async def _send_screenshot_artifact(self, bot, chat_id: int, artifact) -> tuple[bool, str | None]:
        last_error: Exception | None = None
        if artifact.mime_type in {"image/png", "image/jpeg"}:
            try:
                with artifact.path.open("rb") as file_obj:
                    await bot.send_photo(chat_id=chat_id, photo=file_obj)
                return True, None
            except Exception as exc:  # noqa: BLE001 - retry as document below
                last_error = exc
        try:
            with artifact.path.open("rb") as file_obj:
                await bot.send_document(
                    chat_id=chat_id,
                    document=file_obj,
                    filename=artifact.path.name,
                )
            return True, None
        except Exception as exc:  # noqa: BLE001 - Telegram errors should not crash the bridge
            if last_error is not None:
                return False, f"photo upload failed ({last_error}); document upload failed ({exc})"
            return False, str(exc)

    async def _send_new_screenshots(self, bot, chat_id: int) -> None:
        if self._chat_notifications_suppressed(chat_id):
            return
        since = self._screenshot_watch_since_by_chat.get(chat_id)
        if since is None:
            return
        for artifact in self.screenshot_service.artifacts_since(since):
            if artifact.path in self._sent_screenshot_paths:
                continue
            sent, error = await self._send_screenshot_artifact(bot, chat_id, artifact)
            if sent:
                self._sent_screenshot_paths.add(artifact.path)
                self._stop_typing(chat_id)
                await self._safe_send_message(
                    bot,
                    chat_id=chat_id,
                    text=f"Screenshot sent: {artifact.path.name}",
                )
            elif error:
                self._sent_screenshot_paths.add(artifact.path)
                await self._safe_send_message(
                    bot,
                    chat_id=chat_id,
                    text=f"Screenshot detected, but sending failed: {error}",
                )

    async def _maybe_handle_choice_reply(self, text: str, message, context) -> bool:
        pending = self.state.active_pending_action("choice_request")
        if pending is None:
            return False
        stripped = text.strip()
        if not stripped.isdigit():
            return False
        await message.reply_text("Choice replies are only accepted for explicit CliCourier prompts.")
        return True

    async def _apply_choice(self, number: int, message, context) -> None:
        await message.reply_text("No choice is pending.")

    async def _reply_chunks(self, message, text: str) -> None:
        for chunk in chunk_text(text, self.settings.max_telegram_chunk_chars):
            await message.reply_text(chunk)

    def _identity(self, update) -> TelegramIdentity:
        reaction_update = getattr(update, "message_reaction", None)
        if reaction_update is not None:
            user = reaction_update.user
            chat = reaction_update.chat
        else:
            user = update.effective_user
            chat = update.effective_chat
        return TelegramIdentity(
            user_id=user.id if user is not None else None,
            chat_id=chat.id if chat is not None else None,
            chat_type=chat.type if chat is not None else None,
        )

    def _notifications_muted(self) -> bool:
        return self.settings.notification_block_file.expanduser().exists()

    def _chat_notifications_suppressed(self, chat_id: int) -> bool:
        return self._notifications_muted() and chat_id not in self._interactive_output_chats

    def _approval_pending(self) -> bool:
        return self.state.active_pending_action("approval") is not None

    def _agent_initial_prompt(self) -> str:
        prompt = " ".join(self.settings.agent_initial_prompt.split())
        return " ".join(
            (
                prompt,
                f"CliCourier workspace root: {self.settings.workspace_root}.",
                f"CliCourier notification block file: {self.settings.notification_block_file}.",
                "Desktop mode means that block file exists and proactive Telegram output is muted; "
                "Telegram mode means the file is deleted and proactive output resumes. If the user "
                "asks you to switch to desktop/local mode, create the block file. If the user asks "
                "you to switch to Telegram mode, delete the block file.",
                "If the user asks about bridge behavior, mention that Telegram slash commands "
                "control files, screenshots, voice approval, mute/unmute, and agent approvals.",
            )
        )

    def _agent_user_text(self, text: str) -> str:
        if (
            self._agent_context_sent
            or not self.settings.agent_initial_prompt_enabled
            or not self.settings.agent_initial_prompt.strip()
        ):
            return text
        self._agent_context_sent = True
        return f"{self._agent_initial_prompt()}\n\nUser request:\n{text}"

    def _remember_agent_input_echo(self, chat_id: int, text: str) -> None:
        normalized = normalize_echo_text(text)
        if not normalized:
            return
        entries = self._agent_input_echoes_by_chat.setdefault(chat_id, [])
        if normalized not in entries:
            entries.append(normalized)
        del entries[:-8]

    def _suppress_agent_input_echoes(self, chat_id: int, text: str) -> str:
        echoes = self._agent_input_echoes_by_chat.get(chat_id)
        if not echoes or not text.strip():
            return text
        lines = []
        for line in text.splitlines():
            normalized = normalize_echo_text(line)
            if normalized and any(_echo_matches(normalized, echo) for echo in echoes):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def _start_typing(self, bot, chat_id: int) -> None:
        if self._notifications_muted():
            return
        if chat_id in self._typing_tasks:
            return
        task = asyncio.create_task(self._typing_loop(bot, chat_id))
        self._typing_tasks[chat_id] = task
        task.add_done_callback(lambda _task: self._typing_tasks.pop(chat_id, None))

    def _stop_typing(self, chat_id: int) -> None:
        task = self._typing_tasks.pop(chat_id, None)
        if task is not None:
            task.cancel()

    async def _typing_loop(self, bot, chat_id: int) -> None:
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - chat action failures should not crash the bridge
            print(f"telegram typing action failed: {exc}", flush=True)

    def _create_background_task(self, application, coroutine) -> asyncio.Task:
        if getattr(application, "running", False):
            return application.create_task(coroutine)
        return asyncio.create_task(coroutine)

    async def _safe_send_message(self, bot, **kwargs):
        try:
            return await bot.send_message(**kwargs)
        except Exception as exc:  # noqa: BLE001 - Telegram errors should not crash the bridge
            print(f"telegram send failed: {exc}", flush=True)
            return None

    def _record_session_event(self, session: AgentSession, event: AgentEvent) -> None:
        record = getattr(session, "_record_event", None)
        if callable(record):
            record(event)

    def _select_complete_output(self, buffered_text: str, final_text: str) -> str:
        buffered = buffered_text.strip()
        final = final_text.strip()
        if not buffered:
            return final
        if not final:
            return buffered
        if buffered == final:
            return final
        if len(buffered) > len(final) and (buffered.endswith(final) or final in buffered):
            return buffered
        return final


def approval_decision_from_reactions(reactions) -> ApprovalDecision | None:
    for reaction in reversed(reactions):
        decision = interpret_approval_text(getattr(reaction, "emoji", ""))
        if decision is not None:
            return decision
    return None


def detect_interactive_choices(text: str) -> tuple[str, list[str], int] | None:
    """Compatibility shim: raw output is no longer parsed into Telegram choices."""
    return None


def normalize_echo_text(text: str) -> str:
    cleaned = sanitize_terminal_text(text)
    return " ".join(cleaned.split()).strip()


def _echo_matches(line: str, echo: str) -> bool:
    if line == echo:
        return True
    if len(echo) >= 80 and (line in echo or echo in line):
        return True
    return False


AUDIO_DOCUMENT_EXTENSIONS = {".oga", ".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm"}
AUDIO_MIME_SUFFIXES = {
    "audio/ogg": ".oga",
    "audio/opus": ".oga",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
}


def _document_is_audio(document) -> bool:
    mime_type = (getattr(document, "mime_type", None) or "").lower()
    if mime_type.startswith("audio/"):
        return True
    file_name = getattr(document, "file_name", None)
    if not file_name:
        return False
    return Path(file_name).suffix.lower() in AUDIO_DOCUMENT_EXTENSIONS


def _audio_suffix(file_name: str | None, mime_type: str | None) -> str:
    if file_name:
        suffix = Path(file_name).suffix.lower()
        if suffix in AUDIO_DOCUMENT_EXTENSIONS:
            return suffix
    if mime_type:
        suffix = AUDIO_MIME_SUFFIXES.get(mime_type.lower())
        if suffix is not None:
            return suffix
    return ".oga"


SCREENSHOT_SUMMARY_RE = re.compile(
    r"^\s*(?:Size:\s*)?\d{2,5}\s*x\s*\d{2,5}\s+(?:PNG|JPE?G|WEBP)\.?\s*$",
    re.IGNORECASE,
)


def looks_like_screenshot_summary(text: str) -> bool:
    return SCREENSHOT_SUMMARY_RE.fullmatch(text.strip()) is not None


SCREENSHOT_REFERENCE_RE = re.compile(
    r"(?:(?:screenshot|screen shot)[^\n]*\.(?:png|jpe?g|webp)|"
    r"(?:saved|wrote|captured|attached)[^\n]*\.(?:png|jpe?g|webp)|"
    r"[\w./~ -]+\.(?:png|jpe?g|webp))",
    re.IGNORECASE,
)

SCREENSHOT_PATH_RE = re.compile(r"(?P<path>(?:[.~]?/?[\w.-]+/)*[\w .-]+\.(?:png|jpe?g|webp))", re.IGNORECASE)


def looks_like_screenshot_reference(text: str) -> bool:
    return looks_like_screenshot_summary(text) or SCREENSHOT_REFERENCE_RE.search(text) is not None


def extract_screenshot_reference(text: str) -> str | None:
    matches = list(SCREENSHOT_PATH_RE.finditer(sanitize_terminal_text(text)))
    if not matches:
        return None
    return matches[-1].group("path").strip()


def _write_temp_log(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="clicourier-agent-log-",
        suffix=".txt",
        delete=False,
    )
    with handle:
        handle.write(text)
    return Path(handle.name)


def build_transcriber(settings: Settings) -> Transcriber:
    if settings.whisper_backend == WhisperBackend.NONE:
        return DisabledTranscriber()
    if settings.whisper_backend == WhisperBackend.LOCAL:
        return FasterWhisperTranscriber(
            model=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            model_dir=settings.whisper_model_dir,
            ffmpeg_binary=settings.ffmpeg_binary,
        )
    if (
        settings.whisper_backend == WhisperBackend.OPENAI
        or settings.transcription_backend == TranscriptionBackend.OPENAI
    ):
        assert settings.transcription_openai_api_key is not None
        return OpenAITranscriber(
            api_key=settings.transcription_openai_api_key.get_secret_value(),
            model=settings.openai_transcription_model,
        )
    if (
        settings.whisper_backend == WhisperBackend.WHISPER_CPP
        or settings.transcription_backend == TranscriptionBackend.WHISPER_CPP
    ):
        assert settings.whisper_cpp_binary is not None
        assert settings.whisper_cpp_model is not None
        return WhisperCppTranscriber(
            binary=settings.whisper_cpp_binary,
            model=settings.whisper_cpp_model,
            ffmpeg_binary=settings.whisper_cpp_ffmpeg_binary,
            extra_args=settings.whisper_cpp_extra_args,
            timeout_seconds=settings.whisper_cpp_timeout_seconds,
        )
    return DisabledTranscriber()
