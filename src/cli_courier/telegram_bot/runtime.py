from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

from cli_courier.agent.adapters import get_adapter, list_adapters
from cli_courier.agent.approval import ApprovalDecision, detect_pending_approval, interpret_approval_text
from cli_courier.agent.chunking import chunk_text
from cli_courier.agent.output_filter import agent_output_in_progress, prepare_agent_output
from cli_courier.agent.session import AgentSession
from cli_courier.config import AgentOutputMode, Settings, TranscriptionBackend, WhisperBackend
from cli_courier.filesystem import Sandbox, SandboxViolation
from cli_courier.screenshots import ScreenshotError, ScreenshotService
from cli_courier.state import RuntimeState, pending_voice_from_transcript
from cli_courier.telegram_bot.auth import TelegramIdentity, is_authorized, unauthorized_reply
from cli_courier.telegram_bot.commands import COMMAND_HELP, ParsedCommand
from cli_courier.telegram_bot.router import RouteKind, route_text
from cli_courier.voice import (
    DisabledTranscriber,
    FasterWhisperTranscriber,
    OpenAITranscriber,
    Transcriber,
    WhisperCppTranscriber,
    transcribe_with_cleanup,
)


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
        self._agent_context_sent = False

    def build_application(self):
        from telegram.ext import (
            ApplicationBuilder,
            CallbackQueryHandler,
            MessageHandler,
            MessageReactionHandler,
            filters,
        )

        builder = ApplicationBuilder().token(self.settings.telegram_bot_token.get_secret_value())
        if self.settings.auto_start_agent:
            builder = builder.post_init(self._post_init)
        builder = builder.post_shutdown(self._post_shutdown)
        application = builder.build()
        application.add_handler(CallbackQueryHandler(self.handle_callback))
        application.add_handler(MessageReactionHandler(self.handle_reaction))
        application.add_handler(MessageHandler(filters.ALL, self.handle_update))
        return application

    async def _post_init(self, application) -> None:
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
        if message.voice is not None:
            await self._handle_voice(message, context)
            return
        if message.text is None:
            return

        route = route_text(
            message.text,
            has_pending_approval=self.state.pending_approval is not None
            and not self.state.pending_approval.is_expired(),
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
        kind, action, nonce = parts
        if kind == "approval":
            pending = self.state.pending_approval
            if pending is None or pending.nonce != nonce or pending.is_expired():
                await query.edit_message_text("Approval request expired.")
                self.state.clear_pending_approval()
                return
            decision: ApprovalDecision = "approve" if action == "approve" else "reject"
            try:
                await self._apply_approval(decision)
            except RuntimeError as exc:
                await query.edit_message_text(str(exc))
                return
            await query.edit_message_text(f"Sent {decision}.")
        elif kind == "voice":
            pending_voice = self.state.pending_voice
            if pending_voice is None or pending_voice.nonce != nonce or pending_voice.is_expired():
                await query.edit_message_text("Voice transcript expired.")
                self.state.clear_pending_voice()
                return
            if action == "send":
                try:
                    await self._send_to_agent_text(pending_voice.transcript)
                except RuntimeError as exc:
                    await query.edit_message_text(str(exc))
                    return
                self.state.clear_pending_voice()
                await query.edit_message_text("Transcript sent.")
            else:
                self.state.clear_pending_voice()
                await query.edit_message_text("Transcript discarded.")

    async def handle_reaction(self, update, context) -> None:
        reaction_update = update.message_reaction
        if reaction_update is None:
            return
        identity = self._identity(update)
        if not is_authorized(identity, self.settings):
            return

        pending = self.state.pending_approval
        if pending is None or pending.is_expired():
            self.state.clear_pending_approval()
            return
        if pending.message_id is not None and reaction_update.message_id != pending.message_id:
            return

        decision = approval_decision_from_reactions(reaction_update.new_reaction)
        if decision is None:
            return
        try:
            await self._apply_approval(decision)
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
            "status": self._cmd_status,
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
            "help": self._cmd_help,
            "start": self._cmd_help,
        }
        handler = handlers.get(command.name)
        if handler is None:
            await message.reply_text("Unknown command. Use /help.")
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
                f"command: {' '.join(status.command)}"
            )
        pending = "yes" if self.state.pending_approval else "no"
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

    async def _start_agent_session(self, *, chat_id: int, application) -> str:
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
        self._agent_context_sent = False
        task = self._create_background_task(
            application,
            self._flush_agent_output(application.bot, chat_id, session),
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

    async def _cmd_approve(self, args: str, message, context) -> None:
        await self._handle_approval("approve", message, context)

    async def _cmd_reject(self, args: str, message, context) -> None:
        await self._handle_approval("reject", message, context)

    async def _cmd_voice_approve(self, args: str, message, context) -> None:
        pending = self.state.pending_voice
        if pending is None or pending.is_expired():
            self.state.clear_pending_voice()
            await message.reply_text("No voice transcript is pending.")
            return
        try:
            self._start_typing(context.bot, message.chat_id)
            await self._send_to_agent_text(pending.transcript)
        except RuntimeError as exc:
            await message.reply_text(str(exc))
            return
        self.state.clear_pending_voice()
        await message.reply_text("Transcript sent.")

    async def _cmd_voice_reject(self, args: str, message, context) -> None:
        self.state.clear_pending_voice()
        await message.reply_text("Transcript discarded.")

    async def _cmd_voice_edit(self, args: str, message, context) -> None:
        if not args:
            await message.reply_text("Usage: /voice_edit <text>")
            return
        self.state.pending_voice = pending_voice_from_transcript(args)
        await message.reply_text("Transcript updated. Use /voice_approve to send it.")

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

    async def _handle_voice(self, message, context) -> None:
        voice = message.voice
        if voice.file_size and voice.file_size > self.settings.voice_max_bytes:
            await message.reply_text("Voice message is too large.")
            return
        if isinstance(self.transcriber, DisabledTranscriber):
            await message.reply_text("Voice transcription is disabled.")
            return

        try:
            with tempfile.TemporaryDirectory(prefix="cli-courier-voice-") as temp_dir:
                temp_path = Path(temp_dir) / f"{voice.file_unique_id}.oga"
                telegram_file = await context.bot.get_file(voice.file_id)
                await telegram_file.download_to_drive(custom_path=temp_path)
                if temp_path.stat().st_size > self.settings.voice_max_bytes:
                    await message.reply_text("Voice message is too large.")
                    return
                transcript = await transcribe_with_cleanup(self.transcriber, temp_path)
        except Exception as exc:  # noqa: BLE001 - surfaced to trusted operator
            await message.reply_text(f"Voice transcription failed: {exc}")
            return

        self.state.pending_voice = pending_voice_from_transcript(transcript)
        await self._send_voice_confirmation(message, transcript)

    async def _send_voice_confirmation(self, message, transcript: str) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        pending = self.state.pending_voice
        assert pending is not None
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Send", callback_data=f"voice:send:{pending.nonce}"),
                    InlineKeyboardButton("Reject", callback_data=f"voice:reject:{pending.nonce}"),
                ]
            ]
        )
        await message.reply_text(
            f"Transcript:\n{transcript}\n\nUse /voice_edit <text> to change it.",
            reply_markup=keyboard,
        )

    async def _send_to_agent(self, text: str, message, context) -> None:
        self._start_typing(context.bot, message.chat_id)
        try:
            await self._send_to_agent_text(text)
        except RuntimeError as exc:
            self._stop_typing(message.chat_id)
            await message.reply_text(str(exc))

    async def _send_to_agent_text(self, text: str) -> None:
        agent = self.state.active_agent
        if agent is None or not agent.is_running:
            raise RuntimeError("Agent is not running. Use /start_agent first.")
        await agent.send_text(self._agent_user_text(text))

    async def _handle_approval(self, decision: ApprovalDecision, message, context) -> None:
        pending = self.state.pending_approval
        if pending is None or pending.is_expired():
            self.state.clear_pending_approval()
            await message.reply_text("No approval is pending.")
            return
        self._start_typing(context.bot, message.chat_id)
        await self._apply_approval(decision)
        await message.reply_text(f"Sent {decision}.")

    async def _apply_approval(self, decision: ApprovalDecision) -> None:
        agent = self.state.active_agent
        pending = self.state.pending_approval
        if agent is None or pending is None:
            raise RuntimeError("No approval is pending.")
        text = agent.adapter.approve_input if decision == "approve" else agent.adapter.reject_input
        await agent.send_text(text)
        self.state.clear_pending_approval()

    async def _flush_agent_output(self, bot, chat_id: int, session: AgentSession) -> None:
        pending_text = ""
        first_output_at: float | None = None
        last_output_at: float | None = None
        interval = self.settings.output_flush_interval_ms / 1000
        while self.state.active_agent is session and session.is_running:
            try:
                output = await asyncio.wait_for(session.output_queue.get(), timeout=interval)
                if session.replaces_output_snapshots:
                    pending_text = output
                else:
                    pending_text += output
                now = asyncio.get_running_loop().time()
                first_output_at = first_output_at or now
                last_output_at = now
                await self._maybe_send_approval_prompt(bot, chat_id, session)
            except asyncio.TimeoutError:
                pass
            if not pending_text:
                continue
            if self.settings.agent_output_mode == AgentOutputMode.STREAM:
                if len(pending_text) < self.settings.max_telegram_chunk_chars and not session.output_queue.empty():
                    continue
                if not self._approval_pending() and not agent_output_in_progress(pending_text):
                    stream_text = prepare_agent_output(
                        pending_text,
                        suppress_trace_lines=self.settings.suppress_agent_trace_lines,
                    )
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
                if not self._approval_pending():
                    final_text = prepare_agent_output(
                        pending_text,
                        suppress_trace_lines=self.settings.suppress_agent_trace_lines,
                    )
                    await self._send_agent_output(bot, chat_id, final_text)
                    self._stop_typing(chat_id)
                pending_text = ""
                first_output_at = None
                last_output_at = None

    async def _maybe_send_approval_prompt(self, bot, chat_id: int, session: AgentSession) -> None:
        if self._notifications_muted():
            return
        current = self.state.pending_approval
        if current is not None and not current.is_expired():
            return
        pending = detect_pending_approval(session.recent_output(4000), session.adapter)
        if pending is None:
            return
        if pending.prompt_excerpt == self._last_approval_signature:
            return
        self._last_approval_signature = pending.prompt_excerpt
        self.state.pending_approval = pending
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"approval:approve:{pending.nonce}"),
                    InlineKeyboardButton("Reject", callback_data=f"approval:reject:{pending.nonce}"),
                ]
            ]
        )
        sent = await self._safe_send_message(
            bot,
            chat_id=chat_id,
            text=f"Approval requested:\n{pending.prompt_excerpt}",
            reply_markup=keyboard,
        )
        self._stop_typing(chat_id)
        if sent is not None:
            pending.message_id = sent.message_id

    async def _send_agent_output(self, bot, chat_id: int, text: str) -> None:
        if self._notifications_muted() or not text.strip():
            return
        if looks_like_screenshot_summary(text) and await self._send_latest_screenshot(bot, chat_id):
            return
        for chunk in chunk_text(text, self.settings.max_telegram_chunk_chars):
            await self._safe_send_message(bot, chat_id=chat_id, text=chunk)

    async def _send_latest_screenshot(self, bot, chat_id: int) -> bool:
        try:
            artifact = self.screenshot_service.latest()
        except ScreenshotError:
            return False
        try:
            with artifact.path.open("rb") as file_obj:
                if artifact.mime_type in {"image/png", "image/jpeg"}:
                    await bot.send_photo(chat_id=chat_id, photo=file_obj)
                else:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=file_obj,
                        filename=artifact.path.name,
                    )
        except Exception as exc:  # noqa: BLE001 - Telegram errors should not crash the bridge
            print(f"telegram screenshot send failed: {exc}", flush=True)
            return False
        return True

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

    def _approval_pending(self) -> bool:
        pending = self.state.pending_approval
        return pending is not None and not pending.is_expired()

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


def approval_decision_from_reactions(reactions) -> ApprovalDecision | None:
    for reaction in reversed(reactions):
        decision = interpret_approval_text(getattr(reaction, "emoji", ""))
        if decision is not None:
            return decision
    return None


SCREENSHOT_SUMMARY_RE = re.compile(
    r"^\s*(?:Size:\s*)?\d{2,5}\s*x\s*\d{2,5}\s+(?:PNG|JPE?G|WEBP)\.?\s*$",
    re.IGNORECASE,
)


def looks_like_screenshot_summary(text: str) -> bool:
    return SCREENSHOT_SUMMARY_RE.fullmatch(text.strip()) is not None


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
