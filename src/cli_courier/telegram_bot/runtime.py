from __future__ import annotations

import asyncio
import html
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
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
from cli_courier.config import (
    AgentOutputMode,
    Settings,
    TranscriptionBackend,
    WhisperBackend,
    settings_summary_lines,
)
from cli_courier.filesystem import Sandbox, SandboxViolation
from cli_courier.screenshots import ScreenshotError, ScreenshotService
from cli_courier.security.terminal import safe_excerpt, sanitize_terminal_text
from cli_courier.state import (
    PendingActionChoice,
    PendingAction,
    RuntimeState,
    pending_action,
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
from cli_courier.telegram_bot.output_renderer import (
    StreamingMessageRenderer,
    TELEGRAM_SAFE_LIMIT,
    is_message_too_long_error,
    telegram_text_size,
)
from cli_courier.telegram_bot.router import RouteKind, route_text
from cli_courier.chat_history import ChatHistory
from cli_courier.local_config import default_config_path, default_data_dir
from cli_courier.update import run_update
from cli_courier.voice import (
    DisabledTranscriber,
    FasterWhisperTranscriber,
    OpenAITranscriber,
    Transcriber,
    WhisperCppTranscriber,
    transcribe_with_cleanup,
)

TERMINAL_PROGRESS_PAGE_LINES = 60
TELEGRAM_PROGRESS_SAFE_LIMIT = TELEGRAM_SAFE_LIMIT


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
        self._configured_adapter_id = settings.default_agent_adapter
        self._flush_tasks: set[asyncio.Task[None]] = set()
        self._typing_tasks: dict[int, asyncio.Task[None]] = {}
        self._last_approval_signature: str | None = None
        self._last_choice_signature: str | None = None
        self._chat_histories: dict[int, ChatHistory] = {}
        self._screenshot_watch_since_by_chat: dict[int, float] = {}
        self._sent_screenshot_paths: set[Path] = set()
        self._agent_input_echoes_by_chat: dict[int, list[str]] = {}
        self._interactive_output_chats: set[int] = set()
        self._agent_context_sent = False
        self._dashboard_message_ids: dict[int, int] = {}
        self._dashboard_last_update: dict[int, float] = {}
        self._dashboard_last_text: dict[int, str] = {}
        self._terminal_progress_by_chat: dict[int, StreamingMessageRenderer] = {}
        self._agent_output_task: asyncio.Task[None] | None = None
        self._agent_output_session: AgentSession | None = None
        self._agent_output_chat_id: int | None = None

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
            try:
                await self._start_agent_session(
                    chat_id=None,
                    application=application,
                )
            except Exception as exc:  # noqa: BLE001 - daemon log should capture startup failures
                print(f"failed to auto-start terminal-only agent: {exc}", flush=True)
            return
        if not self._notifications_muted():
            reachable = await self._safe_send_chat_action(
                application.bot,
                chat_id=self.settings.default_telegram_chat_id,
                action="typing",
            )
            if not reachable:
                print(
                    "auto-start skipped: DEFAULT_TELEGRAM_CHAT_ID is not reachable. "
                    "Starting the local terminal session without Telegram output.",
                    flush=True,
                )
                try:
                    await self._start_agent_session(
                        chat_id=None,
                        application=application,
                    )
                except Exception as exc:  # noqa: BLE001 - daemon log should capture startup failures
                    print(f"failed to auto-start terminal-only agent: {exc}", flush=True)
                return
        try:
            await self._start_agent_session(
                chat_id=self.settings.default_telegram_chat_id,
                application=application,
            )
        except Exception as exc:  # noqa: BLE001 - daemon log should capture startup failures
            print(f"failed to auto-start agent: {exc}", flush=True)
            return

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
        image_paths = await self._download_prompt_images(message, context)
        if image_paths is None:
            return
        file_paths = await self._download_prompt_files(message, context)
        if file_paths is None:
            return
        text = self._message_text(message)
        if text is None and image_paths:
            text = "Please inspect the attached image."
        if text is None and file_paths:
            text = "Please inspect the attached file." if len(file_paths) == 1 else "Please inspect the attached files."
        if text is None:
            return
        if await self._maybe_handle_voice_correction(text, message):
            return
        if await self._maybe_handle_choice_reply(text, message, context):
            return

        route = route_text(
            text,
            has_pending_approval=self.state.active_pending_action("approval", chat_id=message.chat_id) is not None,
        )
        if route.kind == RouteKind.EMPTY:
            return
        if route.kind == RouteKind.COMMAND and route.command is not None:
            await self._handle_command(
                route.command,
                message,
                context,
                image_paths=image_paths,
                file_paths=file_paths,
            )
        elif route.kind == RouteKind.APPROVAL and route.approval_decision is not None:
            await self._handle_approval(route.approval_decision, message, context)
        elif route.kind == RouteKind.BLOCKED_APPROVAL:
            await message.reply_text("No approval is pending. Use /agent yes to send that text anyway.")
        elif route.kind == RouteKind.AGENT_TEXT:
            await self._send_to_agent(route.text, message, context, image_paths=image_paths, file_paths=file_paths)

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
        message = getattr(query, "message", None)
        if action.chat_id is not None and getattr(message, "chat_id", None) != action.chat_id:
            await query.edit_message_text("This action belongs to a different chat.")
            return
        if action.choice(choice_id) is None:
            await query.edit_message_text("This action choice is no longer valid.")
            return
        if action.kind == "approval":
            await self._handle_pending_approval_callback(action, choice_id, query, context)
        elif action.kind == "voice_transcript":
            await self._handle_pending_voice_callback(action, choice_id, query, context)
        elif action.kind == "choice_request":
            await self._handle_pending_choice_callback(action, choice_id, query, context)

    async def handle_reaction(self, update, context) -> None:
        reaction_update = update.message_reaction
        if reaction_update is None:
            return
        identity = self._identity(update)
        if not is_authorized(identity, self.settings):
            return

        pending = self.state.active_pending_action("approval", chat_id=reaction_update.chat.id)
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

    async def _handle_command(
        self,
        command: ParsedCommand,
        message,
        context,
        *,
        image_paths: tuple[Path, ...] = (),
        file_paths: tuple[Path, ...] = (),
    ) -> None:
        handlers = {
            "botstatus": self._cmd_status,
            "restart": self._cmd_restart_bridge,
            "update": self._cmd_update,
            "start_agent": self._cmd_start_agent,
            "stop_agent": self._cmd_stop_agent,
            "restart_agent": self._cmd_restart_agent,
            "resume": self._cmd_resume_agent,
            "resume_agent": self._cmd_resume_agent,
            "agent": self._cmd_agent,
            "agents": self._cmd_agents,
            "switch": self._cmd_switch,
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
            "recheck": self._cmd_recheck,
            "bothelp": self._cmd_help,
            "start": self._cmd_help,
        }
        handler = handlers.get(command.name)
        if handler is None:
            forwarded = f"/{command.name}"
            if command.args:
                forwarded = f"{forwarded} {command.args}"
            await self._send_to_agent(
                forwarded,
                message,
                context,
                image_paths=image_paths,
                file_paths=file_paths,
                preserve_leading_slash=True,
            )
            return
        if command.name == "agent":
            await handler(command.args, message, context, image_paths=image_paths, file_paths=file_paths)
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
        pending = "yes" if self.state.active_pending_action("approval", chat_id=message.chat_id) else "no"
        muted = "yes" if self._notifications_muted() else "no"
        config_summary = "\n".join(
            settings_summary_lines(
                self.settings,
                config_path=_active_config_path(),
            )
        )
        await self._reply_code(
            message,
            f"{config_summary}\n"
            f"{agent_status}\n"
            f"workspace: {self.sandbox.display_path(self.state.cwd)}\n"
            f"pending approval: {pending}\n"
            f"output mode: {self.settings.agent_output_mode.value}\n"
            f"muted: {muted}",
        )

    async def _cmd_start_agent(self, args: str, message, context) -> None:
        if self.state.active_agent is not None and self.state.active_agent.is_running:
            await message.reply_text("Agent is already running.")
            return
        resume = _resume_requested(args)
        try:
            reply = await self._start_agent_session(
                chat_id=message.chat_id,
                application=context.application,
                resume=resume,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to trusted operator
            await message.reply_text(f"Failed to start agent: {exc}")
            return
        await message.reply_text(reply)

    async def _start_agent_session(
        self,
        *,
        chat_id: int | None,
        application=None,
        bot=None,
        resume: bool = False,
        resume_required: bool = False,
    ) -> str:
        adapter = get_adapter(self.settings.default_agent_adapter)
        if resume_required and not adapter.capabilities.supports_resume:
            raise RuntimeError(f"{adapter.display_name} does not support session resume.")
        resume_last = (resume or self.settings.agent_resume_last) and adapter.capabilities.supports_resume
        configured_cmd = self.settings.default_agent_command if adapter.id == self._configured_adapter_id else None
        command = adapter.build_command(configured_cmd)
        session = AgentSession(
            adapter=adapter,
            command=command,
            cwd=self.settings.workspace_root,
            recent_output_max_chars=self.settings.recent_output_max_chars,
            env_allowlist=self.settings.agent_env_allowlist,
            terminal_backend=self.settings.agent_terminal_backend.value,
            tmux_session_name=self.settings.agent_tmux_session,
            tmux_history_lines=self.settings.agent_tmux_history_lines,
            resume_last=resume_last,
        )
        await session.start()
        self.state.active_agent = session
        self.state.agent_chat_id = chat_id
        self._last_approval_signature = None
        self._last_choice_signature = None
        self._agent_context_sent = False
        self.state.clear_pending_actions()
        if chat_id is not None:
            self._ensure_agent_output_forwarding(
                chat_id=chat_id,
                session=session,
                application=application,
                bot=bot,
            )
        action = "resumed" if resume_last else "started"
        return f"Agent {action}: {' '.join(session.command)}"

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
        resume = not _no_resume_requested(args)
        try:
            reply = await self._start_agent_session(
                chat_id=message.chat_id,
                application=context.application,
                resume=resume,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to trusted operator
            await message.reply_text(f"Failed to restart agent: {exc}")
            return
        await message.reply_text(reply)

    async def _cmd_resume_agent(self, args: str, message, context) -> None:
        if self.state.active_agent is not None:
            await self.state.active_agent.stop()
            self.state.active_agent = None
        try:
            reply = await self._start_agent_session(
                chat_id=message.chat_id,
                application=context.application,
                resume=True,
                resume_required=True,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to trusted operator
            await message.reply_text(f"Failed to resume agent: {exc}")
            return
        await message.reply_text(reply)

    async def _cmd_restart_bridge(self, args: str, message, context) -> None:
        no_resume = _no_resume_requested(args)
        active = self.state.active_agent
        adapter_id = active.adapter.id if active is not None else self.settings.default_agent_adapter
        adapter_name = active.adapter.display_name if active is not None else get_adapter(adapter_id).display_name
        restart_env = {
            **os.environ,
            "DEFAULT_AGENT_ADAPTER": adapter_id,
            "DEFAULT_TELEGRAM_CHAT_ID": str(message.chat_id),
        }
        active_command = tuple(
            getattr(active, "base_command", ()) or getattr(active, "command", ()) or ()
        ) if active is not None else ()
        strip_resume = getattr(getattr(active, "adapter", None), "strip_resume_command", None)
        if callable(strip_resume):
            active_command = tuple(strip_resume(list(active_command)))
        if active_command:
            restart_env["DEFAULT_AGENT_COMMAND"] = shlex.join(str(part) for part in active_command)
        command = _bridge_restart_command(no_resume=no_resume)
        try:
            subprocess.Popen(
                command,
                cwd=str(self.settings.workspace_root),
                env=restart_env,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to trusted operator
            await message.reply_text(f"Failed to schedule CliCourier restart: {exc}")
            return
        suffix = f"without resume" if no_resume else f"resuming {adapter_name}"
        session_name = self.settings.agent_tmux_session or "clicourier"
        await message.reply_text(
            f"Restarting CliCourier {suffix}. The bot will reconnect shortly.\n"
            f"Agent terminal: tmux attach -t {session_name}"
        )

    async def _cmd_update(self, args: str, message, context) -> None:
        await message.reply_text("Updating CliCourier…")
        result = await asyncio.to_thread(run_update)
        await message.reply_text(result.summary())

    async def _cmd_agent(
        self,
        args: str,
        message,
        context,
        *,
        image_paths: tuple[Path, ...] = (),
        file_paths: tuple[Path, ...] = (),
    ) -> None:
        if not args:
            await message.reply_text("Usage: /agent <text>")
            return
        await self._send_to_agent(args, message, context, image_paths=image_paths, file_paths=file_paths)

    async def _cmd_agents(self, args: str, message, context) -> None:
        active = self.state.active_agent
        active_id = active.adapter.id if active is not None else self.settings.default_agent_adapter
        lines = []
        for adapter_id, adapter in sorted(list_adapters().items()):
            marker = "* " if adapter_id == active_id else "  "
            lines.append(f"{marker}{adapter_id}: {adapter.display_name}")
        await self._reply_code(message, "\n".join(lines))

    async def _cmd_switch(self, args: str, message, context) -> None:
        target = args.strip().lower()
        adapters = list_adapters()
        active = self.state.active_agent
        active_id = active.adapter.id if active is not None else self.settings.default_agent_adapter
        if not target:
            lines = []
            for adapter_id, adapter in sorted(adapters.items()):
                marker = "* " if adapter_id == active_id else "  "
                lines.append(f"{marker}{adapter_id}: {adapter.display_name}")
            await self._reply_code(message, "Available adapters (* = active):\n" + "\n".join(lines))
            return
        if target not in adapters:
            names = ", ".join(sorted(adapters.keys()))
            await message.reply_text(f"Unknown adapter '{target}'. Available: {names}")
            return
        if target == active_id:
            await message.reply_text(f"Already using {adapters[target].display_name}.")
            return
        self.settings.default_agent_adapter = target
        new_adapter = adapters[target]
        if active is not None and active.is_running:
            await active.stop()
            self.state.active_agent = None
        try:
            reply = await self._start_agent_session(
                chat_id=message.chat_id,
                application=context.application,
                resume=False,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to operator
            await message.reply_text(f"Switched to {new_adapter.display_name} but failed to start: {exc}")
            return
        await message.reply_text(f"Switched to {new_adapter.display_name}. {reply}")

    async def _cmd_pwd(self, args: str, message, context) -> None:
        await self._reply_code(message, self.sandbox.display_path(self.state.cwd))

    async def _cmd_ls(self, args: str, message, context) -> None:
        try:
            entries = self.sandbox.list_dir(args or ".", cwd=self.state.cwd)
        except SandboxViolation as exc:
            await message.reply_text(str(exc))
            return
        body = "\n".join(entry.display_name for entry in entries) or "(empty)"
        await self._reply_code(message, body)

    async def _cmd_tree(self, args: str, message, context) -> None:
        try:
            body = self.sandbox.tree(args or ".", cwd=self.state.cwd)
        except SandboxViolation as exc:
            await message.reply_text(str(exc))
            return
        await self._reply_code(message, body)

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
        await self._reply_code(message, self.sandbox.display_path(path))

    async def _cmd_cat(self, args: str, message, context) -> None:
        if not args:
            await message.reply_text("Usage: /cat <path>")
            return
        try:
            body = self.sandbox.cat_file(args, cwd=self.state.cwd)
        except SandboxViolation as exc:
            await message.reply_text(str(exc))
            return
        await self._reply_code(message, body or "(empty)")

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
        await self._reply_code(message, "\n".join(lines))

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
        await self._reply_code(message, agent.recent_output(max(1, limit)) or "(empty)")

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
        await message.reply_text("Editable 60-line progress output enabled.")

    async def _cmd_final(self, args: str, message, context) -> None:
        self.settings.agent_output_mode = AgentOutputMode.FINAL
        await message.reply_text("Editable 60-line progress output enabled.")

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
        pending = self.state.active_pending_action("voice_transcript", chat_id=message.chat_id)
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
        self.state.clear_pending_actions(kind="voice_transcript", chat_id=message.chat_id)
        await message.reply_text("Transcript discarded.")

    async def _cmd_voice_edit(self, args: str, message, context) -> None:
        if not args:
            await message.reply_text("Usage: /voice_edit <text>")
            return
        self.state.clear_pending_actions(kind="voice_transcript", chat_id=message.chat_id)
        self.state.add_pending_action(
            pending_voice_action_from_transcript(args, chat_id=message.chat_id)
        )
        await message.reply_text("Transcript updated. Use /voice_approve to send it.")

    async def _maybe_handle_voice_correction(self, text: str, message) -> bool:
        pending = self.state.active_pending_action("voice_transcript", chat_id=message.chat_id)
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

    async def _cmd_recheck(self, args: str, message, context) -> None:
        session = self.state.active_agent
        if session is None or not session.is_running:
            await message.reply_text("Agent is not running.")
            return
        if session.adapter.capabilities.supports_approval_events:
            await message.reply_text("Adapter uses native approval events; recheck is only for fallback detection.")
            return
        self.state.clear_pending_actions(kind="approval", chat_id=message.chat_id)
        self._last_approval_signature = None
        recent = session.recent_output(4000)
        from cli_courier.agent.approval import detect_pending_approval
        pending = detect_pending_approval(recent, session.adapter)
        if pending is None:
            await message.reply_text("Recheck complete. No pending approval detected in recent output.")
            return
        await self._send_approval_event(
            context.bot,
            message.chat_id,
            session,
            AgentEvent(
                kind=AgentEventKind.APPROVAL_REQUESTED,
                text=pending.prompt_excerpt,
                session_id=session.adapter.id,
            ),
        )

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

            self.state.clear_pending_actions(kind="voice_transcript", chat_id=message.chat_id)
            self.state.add_pending_action(
                pending_voice_action_from_transcript(transcript, chat_id=message.chat_id)
            )
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
        return None

    async def _send_voice_confirmation(self, message, transcript: str) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        pending = self.state.active_pending_action("voice_transcript", chat_id=message.chat_id)
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

    async def _send_to_agent(
        self,
        text: str,
        message,
        context,
        *,
        image_paths: tuple[Path, ...] = (),
        file_paths: tuple[Path, ...] = (),
        preserve_leading_slash: bool = False,
    ) -> None:
        self._start_typing(context.bot, message.chat_id)
        try:
            await self._send_to_agent_text(
                text,
                chat_id=message.chat_id,
                application=getattr(context, "application", None),
                bot=getattr(context, "bot", None),
                image_paths=image_paths,
                file_paths=file_paths,
                preserve_leading_slash=preserve_leading_slash,
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
        image_paths: tuple[Path, ...] = (),
        file_paths: tuple[Path, ...] = (),
        preserve_leading_slash: bool = False,
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
        prompt_text = self._prompt_text_with_attachments(
            text,
            image_paths=image_paths,
            file_paths=file_paths,
        )
        user_text = prompt_text if preserve_leading_slash else self._agent_user_text(prompt_text, chat_id=chat_id)
        if chat_id is not None:
            if isinstance(agent, AgentSession):
                self._ensure_agent_output_forwarding(
                    chat_id=chat_id,
                    session=agent,
                    application=application,
                    bot=bot,
                )
            history = self._chat_history(chat_id)
            if history is not None:
                history.append(role="user", text=text)
            self._clear_terminal_progress(chat_id)
            self._interactive_output_chats.add(chat_id)
            self._screenshot_watch_since_by_chat[chat_id] = time.time()
            self._remember_agent_input_echo(chat_id, prompt_text)
            self._remember_agent_input_echo(chat_id, user_text)
        self._last_approval_signature = None
        self._last_choice_signature = None
        await agent.send_text(user_text)

    def _ensure_agent_output_forwarding(
        self,
        *,
        chat_id: int,
        session: AgentSession,
        application=None,
        bot=None,
    ) -> None:
        if (
            self._agent_output_task is not None
            and not self._agent_output_task.done()
            and self._agent_output_session is session
            and self._agent_output_chat_id == chat_id
        ):
            return
        if self._agent_output_task is not None and not self._agent_output_task.done():
            self._agent_output_task.cancel()
        target_bot = application.bot if application is not None else bot
        if target_bot is None:
            raise RuntimeError("Telegram bot context is not available.")
        task = self._create_background_task(
            application,
            self._flush_agent_output(target_bot, chat_id, session),
        )
        self._agent_output_task = task
        self._agent_output_session = session
        self._agent_output_chat_id = chat_id
        self.state.agent_chat_id = chat_id
        self._flush_tasks.add(task)

        def _forget_task(done_task: asyncio.Task[None]) -> None:
            self._flush_tasks.discard(done_task)
            if self._agent_output_task is done_task:
                self._agent_output_task = None
                self._agent_output_session = None
                self._agent_output_chat_id = None

        task.add_done_callback(_forget_task)

    async def _handle_approval(self, decision: ApprovalDecision, message, context) -> None:
        pending = self.state.active_pending_action("approval", chat_id=message.chat_id)
        if pending is None:
            await message.reply_text("No approval is pending.")
            return
        self._start_typing(context.bot, message.chat_id)
        await self._apply_approval_action(pending, decision)

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
            try:
                await query.message.delete()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
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

    async def _handle_pending_choice_callback(
        self,
        action: PendingAction,
        choice_id: str,
        query,
        context,
    ) -> None:
        choice = action.choice(choice_id)
        if choice is None:
            await query.edit_message_text("This action choice is no longer valid.")
            return
        try:
            await self._apply_choice_action(action, choice)
        except RuntimeError as exc:
            await query.edit_message_text(str(exc))
            return
        await query.edit_message_text(f"Sent option: {choice.label}")

    async def _flush_agent_output(self, bot, chat_id: int, session: AgentSession) -> None:
        pending_text = ""
        interval = self.settings.output_flush_interval_ms / 1000
        last_output_at: float | None = None
        completed_terminal_signature: str | None = None
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
                    was_interactive = chat_id in self._interactive_output_chats
                    await self._send_agent_output(
                        bot,
                        chat_id,
                        final_text,
                        complete_request=True,
                    )
                    self._stop_typing(chat_id)
                    pending_text = ""
                    if was_interactive:
                        await self._send_turn_done_notification(bot, chat_id, allow_muted=True)
                    continue
                await self._handle_agent_event(bot, chat_id, session, event)
                if event.kind == AgentEventKind.TURN_COMPLETED and not self._approval_pending(chat_id):
                    was_interactive = chat_id in self._interactive_output_chats
                    if pending_text:
                        final_text = prepare_agent_output(
                            pending_text,
                            suppress_trace_lines=self.settings.suppress_agent_trace_lines,
                        )
                        final_text = self._suppress_agent_input_echoes(chat_id, final_text)
                        await self._send_agent_output(
                            bot,
                            chat_id,
                            final_text,
                            complete_request=True,
                        )
                    self._stop_typing(chat_id)
                    pending_text = ""
                    if was_interactive:
                        await self._send_turn_done_notification(bot, chat_id, allow_muted=True)
                if event.kind in {AgentEventKind.TURN_COMPLETED, AgentEventKind.TURN_FAILED, AgentEventKind.ERROR}:
                    if not self._approval_pending(chat_id):
                        self._interactive_output_chats.discard(chat_id)
                if event.kind != AgentEventKind.ASSISTANT_DELTA:
                    await self._maybe_update_dashboard(bot, chat_id, session)
                    continue
                last_output_at = asyncio.get_running_loop().time()
                if session.replaces_output_snapshots:
                    pending_text = event.text
                else:
                    pending_text += event.text
                progress_text = self._prepare_progress_delta(event.text)
                progress_text = self._suppress_agent_input_echoes(chat_id, progress_text)
                progress_signature = progress_text.strip()
                is_interactive = chat_id in self._interactive_output_chats
                should_forward_progress = is_interactive or not session.replaces_output_snapshots
                if completed_terminal_signature and progress_signature == completed_terminal_signature:
                    if should_forward_progress:
                        await self._maybe_emit_fallback_approval(bot, chat_id, session)
                    continue
                if progress_signature:
                    completed_terminal_signature = None
                if is_interactive:
                    await self._maybe_emit_terminal_choice_request(bot, chat_id, session, pending_text)
                if progress_text.strip() and not self._approval_pending(chat_id) and should_forward_progress:
                    if session.replaces_output_snapshots:
                        await self._replace_terminal_progress(bot, chat_id, progress_text)
                    else:
                        await self._update_terminal_progress(bot, chat_id, progress_text)
                if should_forward_progress:
                    await self._maybe_emit_fallback_approval(bot, chat_id, session)
            except asyncio.TimeoutError:
                completed_signature = await self._maybe_complete_idle_terminal_output(
                    bot,
                    chat_id,
                    session,
                    pending_text=pending_text,
                    last_output_at=last_output_at,
                    completed_signature=completed_terminal_signature,
                )
                if completed_signature is not None:
                    completed_terminal_signature = completed_signature
                    pending_text = ""
                    last_output_at = None
                await self._render_terminal_progress(bot, chat_id)
                if chat_id in self._interactive_output_chats or not session.replaces_output_snapshots:
                    await self._maybe_emit_fallback_approval(bot, chat_id, session)
            await self._send_new_screenshots(bot, chat_id)
            await self._maybe_update_dashboard(bot, chat_id, session)
        if pending_text and (chat_id in self._interactive_output_chats or not session.replaces_output_snapshots):
            final_text = prepare_agent_output(
                pending_text,
                suppress_trace_lines=self.settings.suppress_agent_trace_lines,
            )
            final_text = self._suppress_agent_input_echoes(chat_id, final_text)
            await self._send_agent_output(
                bot,
                chat_id,
                final_text,
                complete_request=True,
            )
            self._stop_typing(chat_id)

    async def _maybe_complete_idle_terminal_output(
        self,
        bot,
        chat_id: int,
        session: AgentSession,
        *,
        pending_text: str,
        last_output_at: float | None,
        completed_signature: str | None,
    ) -> str | None:
        if session.backend == "structured":
            return None
        if chat_id not in self._interactive_output_chats:
            return None
        if last_output_at is None or not pending_text.strip():
            return None
        if self._approval_pending(chat_id):
            return None
        now = asyncio.get_running_loop().time()
        if now - last_output_at < self.settings.final_output_idle_ms / 1000:
            return None
        if agent_output_in_progress(pending_text):
            return None
        final_text = prepare_agent_output(
            pending_text,
            suppress_trace_lines=self.settings.suppress_agent_trace_lines,
        )
        final_text = self._suppress_agent_input_echoes(chat_id, final_text)
        signature = final_text.strip()
        if not signature or signature == completed_signature:
            return None
        await self._send_agent_output(
            bot,
            chat_id,
            final_text,
            complete_request=True,
        )
        self._stop_typing(chat_id)
        await self._send_turn_done_notification(bot, chat_id, allow_muted=True)
        return signature

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
        if event.kind == AgentEventKind.CHOICE_REQUEST:
            await self._send_choice_request_event(bot, chat_id, session, event)
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
        if event.kind == AgentEventKind.TOOL_STARTED and event.text.strip():
            prefix = event.tool_name or "tool"
            await self._send_agent_output(bot, chat_id, f"[{prefix}] {event.text}\n")
        if event.kind == AgentEventKind.TOOL_COMPLETED and event.text.strip():
            await self._send_agent_output(bot, chat_id, _truncate_tool_result(event.text) + "\n")

    async def _maybe_emit_fallback_approval(self, bot, chat_id: int, session: AgentSession) -> None:
        if self._chat_notifications_suppressed(chat_id):
            return
        if session.adapter.capabilities.supports_approval_events:
            return
        if session.backend == "structured":
            return
        recent_output = session.recent_output(4000)
        if has_auto_approval_marker(recent_output):
            self.state.clear_pending_actions(kind="approval", chat_id=chat_id)
            self._last_approval_signature = None
            return
        current = self.state.active_pending_action("approval", chat_id=chat_id)
        if current is not None:
            if current.message_id is not None:
                return
            # Pending action exists but Telegram message never sent — clear and retry.
            print(
                f"clicourier approval_retry chat_id={chat_id} action_id={current.id}",
                flush=True,
            )
            self.state.clear_pending_action(current.id)
            self._last_approval_signature = None
        pending = detect_pending_approval(recent_output, session.adapter)
        if pending is None:
            print(
                f"clicourier approval_scan chat_id={chat_id} detected=false"
                f" tail_len={len(recent_output)} sig={self._last_approval_signature!r:.60}",
                flush=True,
            )
            return
        if pending.prompt_excerpt == self._last_approval_signature:
            return
        print(
            f"clicourier approval_scan chat_id={chat_id} detected=true"
            f" excerpt={pending.prompt_excerpt!r:.80}",
            flush=True,
        )
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
        if self.state.active_pending_action("approval", session_id=event.session_id, chat_id=chat_id) is not None:
            return
        action = pending_approval_action(
            session_id=event.session_id or session.adapter.id,
            chat_id=chat_id,
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

    async def _send_turn_done_notification(
        self,
        bot,
        chat_id: int,
        *,
        allow_muted: bool = False,
    ) -> None:
        if self._notifications_muted() and not allow_muted:
            return
        if self._approval_pending(chat_id):
            return
        sent = await self._safe_send_message(bot, chat_id=chat_id, text="Done.")
        delete_after = self.settings.done_notification_delete_after_seconds
        if sent is not None and delete_after > 0:
            self._create_background_task(
                None,
                self._delete_message_after_delay(bot, chat_id, sent.message_id, delete_after),
            )

    async def _delete_message_after_delay(
        self,
        bot,
        chat_id: int,
        message_id: int,
        delay_seconds: int,
    ) -> None:
        await asyncio.sleep(delay_seconds)
        delete_message = getattr(bot, "delete_message", None)
        if delete_message is None:
            return
        try:
            await delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:  # noqa: BLE001 - cleanup should not crash the bridge
            print(f"telegram delete failed: {exc}", flush=True)

    async def _send_choice_request_event(
        self,
        bot,
        chat_id: int,
        session: AgentSession,
        event: AgentEvent,
    ) -> None:
        raw_choices = event.data.get("choices")
        if not isinstance(raw_choices, list):
            return
        choices: list[PendingActionChoice] = []
        option_lines: list[str] = []
        for index, raw_choice in enumerate(raw_choices, start=1):
            if not isinstance(raw_choice, dict):
                continue
            label = _clean_choice_text(raw_choice.get("label") or raw_choice.get("text"))
            if not label:
                continue
            choice_id = _clean_choice_text(raw_choice.get("id")) or str(index)
            value = _clean_choice_text(raw_choice.get("value")) or choice_id
            choices.append(PendingActionChoice(id=choice_id, label=label, value=value))
            option_lines.append(f"{len(choices)}. {label}")
        if not choices:
            return
        prompt = _choice_prompt_text(event)
        if prompt is None:
            return
        prompt = safe_excerpt(prompt, 1200)
        signature = f"{prompt}\n" + "\n".join(option_lines)
        if signature == self._last_choice_signature:
            return
        self._last_choice_signature = signature
        self.state.clear_pending_actions(kind="choice_request", chat_id=chat_id)
        action = pending_action(
            kind="choice_request",
            session_id=event.session_id or session.adapter.id,
            chat_id=chat_id,
            source_event_id=event.event_id,
            choices=tuple(choices),
            data={"prompt": prompt},
        )
        self.state.add_pending_action(action)
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard_rows = []
        for index, choice in enumerate(choices, start=1):
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        _choice_button_label(index, choice.label),
                        callback_data=f"cc:{action.id}:{choice.id}",
                    )
                ]
            )
        text = f"{prompt}\n\n" + "\n".join(option_lines) + "\n\nReply with a number."
        sent = await self._safe_send_message(
            bot,
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )
        self._stop_typing(chat_id)
        if sent is not None:
            action.message_id = sent.message_id

    async def _maybe_emit_terminal_choice_request(
        self,
        bot,
        chat_id: int,
        session: AgentSession,
        text: str,
    ) -> None:
        if self._chat_notifications_suppressed(chat_id):
            return
        if session.backend == "structured":
            return
        detected = detect_interactive_choices(text)
        if detected is None:
            return
        prompt, labels, selected_index = detected
        choices = tuple(
            PendingActionChoice(id=str(index), label=label, value=str(index))
            for index, label in enumerate(labels, start=1)
        )
        option_lines = [f"{index}. {label}" for index, label in enumerate(labels, start=1)]
        signature = f"terminal:{selected_index}:{prompt}\n" + "\n".join(option_lines)
        if signature == self._last_choice_signature:
            return
        self._last_choice_signature = signature
        self.state.clear_pending_actions(kind="choice_request", chat_id=chat_id)
        action = pending_action(
            kind="choice_request",
            session_id=session.adapter.id,
            chat_id=chat_id,
            source_event_id=None,
            choices=choices,
            data={
                "prompt": prompt,
                "input_mode": "terminal_navigation",
                "selected_index": selected_index,
            },
        )
        self.state.add_pending_action(action)
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard_rows = []
        for index, choice in enumerate(choices, start=1):
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        _choice_button_label(index, choice.label),
                        callback_data=f"cc:{action.id}:{choice.id}",
                    )
                ]
            )
        text = f"{prompt}\n\n" + "\n".join(option_lines) + "\n\nReply with a number."
        sent = await self._safe_send_message(
            bot,
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
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
            if status.state == "starting":
                return
            sent = await self._safe_send_message(
                bot,
                chat_id=chat_id,
                text=text,
                disable_notification=True,
            )
            if sent is not None:
                self._dashboard_message_ids[chat_id] = sent.message_id
                self._dashboard_last_update[chat_id] = loop_now
                self._dashboard_last_text[chat_id] = text
            return
        if self._dashboard_last_text.get(chat_id) == text:
            self._dashboard_last_update[chat_id] = loop_now
            return
        edit_message_text = getattr(bot, "edit_message_text", None)
        if edit_message_text is None:
            return
        try:
            await edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            self._dashboard_last_update[chat_id] = loop_now
            self._dashboard_last_text[chat_id] = text
        except Exception as exc:  # noqa: BLE001 - Telegram edit failures should not crash bridge
            if _is_telegram_message_not_modified(exc):
                self._dashboard_last_update[chat_id] = loop_now
                self._dashboard_last_text[chat_id] = text
                return
            print(f"telegram dashboard edit failed: {exc}", flush=True)

    async def _send_agent_output(
        self,
        bot,
        chat_id: int,
        text: str,
        *,
        complete_request: bool = False,
    ) -> None:
        if not text.strip():
            return
        if complete_request:
            history = self._chat_history(chat_id)
            if history is not None:
                history.append(role="agent", text=text.strip())
        if self._chat_notifications_suppressed(chat_id):
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
        if not complete_request:
            await self._update_terminal_progress(bot, chat_id, sanitize_terminal_text(text))
            return
        if await self._publish_complete_output(bot, chat_id, text):
            self._clear_terminal_progress(chat_id)
            self._interactive_output_chats.discard(chat_id)
            return
        if complete_request:
            self._clear_terminal_progress(chat_id)
            self._interactive_output_chats.discard(chat_id)

    async def _update_terminal_progress(self, bot, chat_id: int, text: str) -> None:
        if self._chat_notifications_suppressed(chat_id) or not text:
            return
        renderer = self._terminal_progress(chat_id)
        if renderer.message_id is None:
            await self._append_initial_terminal_progress(bot, renderer, text)
            return
        renderer.append_chunk(text)
        await renderer.render(bot, running=True, disable_notification=True)

    async def _append_initial_terminal_progress(
        self,
        bot,
        renderer: StreamingMessageRenderer,
        text: str,
    ) -> None:
        segments = text.splitlines(keepends=True)
        for index, segment in enumerate(segments):
            renderer.append_chunk(segment)
            if len(renderer.latest_lines()) >= TERMINAL_PROGRESS_PAGE_LINES:
                await renderer.render(bot, running=True, disable_notification=True)
                remainder = "".join(segments[index + 1 :])
                if remainder:
                    renderer.append_chunk(remainder)
                    await renderer.render(bot, running=True, disable_notification=True)
                return
        await renderer.render(bot, running=True, disable_notification=True)

    async def _render_terminal_progress(self, bot, chat_id: int) -> None:
        renderer = self._terminal_progress_by_chat.get(chat_id)
        if renderer is None:
            return
        await renderer.render(bot, running=True, disable_notification=True)

    async def _replace_terminal_progress(self, bot, chat_id: int, text: str) -> None:
        lines = sanitize_terminal_text(text).splitlines()
        if not lines and text.strip():
            lines = [text.strip()]
        if not lines:
            return
        renderer = self._terminal_progress(chat_id)
        renderer.replace_lines(lines)
        await renderer.render(bot, running=True, disable_notification=True)

    async def _publish_complete_output(self, bot, chat_id: int, text: str) -> bool:
        lines = sanitize_terminal_text(text).splitlines()
        if not lines and text.strip():
            lines = [text.strip()]
        renderer = self._terminal_progress(chat_id)
        renderer.flush_partial()
        progress_lines = renderer.latest_lines(include_partial=False)
        lines = self._select_complete_lines(progress_lines, lines)
        if not lines:
            return False
        renderer.replace_lines(lines)
        self._log_agent_output(
            "complete_publish",
            chat_id,
            message_id=renderer.message_id,
            source_lines=len(lines),
            visible_lines=len(renderer.latest_lines()),
        )
        await renderer.render(
            bot,
            running=False,
            force=True,
            disable_notification=True,
        )
        return True

    def _terminal_progress(self, chat_id: int) -> StreamingMessageRenderer:
        renderer = self._terminal_progress_by_chat.get(chat_id)
        if renderer is None:
            renderer = StreamingMessageRenderer(
                chat_id=chat_id,
                max_lines=TERMINAL_PROGRESS_PAGE_LINES,
                safe_char_limit=TELEGRAM_PROGRESS_SAFE_LIMIT,
                min_edit_interval_seconds=self.settings.output_flush_interval_ms / 1000,
                log=lambda action, **fields: self._log_agent_output(action, chat_id, **fields),
            )
            self._terminal_progress_by_chat[chat_id] = renderer
        return renderer

    def _clear_terminal_progress(self, chat_id: int) -> None:
        self._terminal_progress_by_chat.pop(chat_id, None)

    def _prepare_progress_delta(self, text: str) -> str:
        prepared = prepare_agent_output(
            text,
            suppress_trace_lines=self.settings.suppress_agent_trace_lines,
        )
        if not prepared.strip():
            return ""
        if text.endswith(("\n", "\r")) and not prepared.endswith(("\n", "\r")):
            return f"{prepared}\n"
        return prepared

    def _log_agent_output(self, action: str, chat_id: int, **fields) -> None:
        parts = [f"action={action}", f"chat_id={chat_id}"]
        for key, value in fields.items():
            if value is None:
                continue
            rendered = str(value).replace("\n", "\\n")
            parts.append(f"{key}={rendered}")
        print("clicourier agent_output " + " ".join(parts), flush=True)

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
        pending = self.state.active_pending_action("choice_request", chat_id=message.chat_id)
        if pending is None:
            return False
        stripped = text.strip()
        if not stripped.isdigit():
            return False
        await self._apply_choice(int(stripped), message, context)
        return True

    async def _apply_choice(self, number: int, message, context) -> None:
        pending = self.state.active_pending_action("choice_request", chat_id=message.chat_id)
        if pending is None:
            await message.reply_text("No choice is pending.")
            return
        if number < 1 or number > len(pending.choices):
            await message.reply_text(f"Valid options: 1-{len(pending.choices)}.")
            return
        choice = pending.choices[number - 1]
        await self._apply_choice_action(pending, choice)
        await message.reply_text(f"Sent option {number}: {choice.label}")

    async def _reply_chunks(self, message, text: str) -> None:
        for chunk in chunk_text(text, self.settings.max_telegram_chunk_chars):
            await message.reply_text(chunk)

    async def _reply_code(self, message, text: str) -> None:
        text = text.strip()
        if not text:
            return
        limit = max(200, min(self.settings.max_telegram_chunk_chars, TELEGRAM_SAFE_LIMIT))
        for chunk in _html_pre_chunks(text, limit):
            escaped = html.escape(chunk, quote=False)
            await message.reply_text(f"<pre>{escaped}</pre>", parse_mode="HTML")

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

    def _approval_pending(self, chat_id: int | None = None) -> bool:
        return self.state.active_pending_action("approval", chat_id=chat_id) is not None

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

    def _agent_user_text(self, text: str, *, chat_id: int | None = None) -> str:
        if (
            self._agent_context_sent
            or not self.settings.agent_initial_prompt_enabled
            or not self.settings.agent_initial_prompt.strip()
        ):
            return text
        self._agent_context_sent = True
        initial = self._agent_initial_prompt()
        history_section = self._agent_history_section(chat_id)
        if history_section:
            return f"{initial}\n\n{history_section}\n\nUser request:\n{text}"
        return f"{initial}\n\nUser request:\n{text}"

    def _chat_history(self, chat_id: int) -> ChatHistory | None:
        if not self.settings.chat_history_enabled:
            return None
        if chat_id not in self._chat_histories:
            history_dir = self.settings.chat_history_dir or (default_data_dir() / "chats")
            self._chat_histories[chat_id] = ChatHistory(history_dir / f"{chat_id}.jsonl")
        return self._chat_histories[chat_id]

    def _agent_history_section(self, chat_id: int | None) -> str:
        if not self.settings.chat_history_enabled or chat_id is None:
            return ""
        history = self._chat_history(chat_id)
        if history is None:
            return ""
        tail = history.tail(self.settings.chat_history_tail_lines)
        lines = [
            f"Chat history log: {history.path}",
            "Read this file for the full conversation (persists after Telegram deletion).",
        ]
        if tail:
            lines.append(f"Recent {len(tail)} entries:")
            for entry in tail:
                snippet = entry.get("text", "")
                if len(snippet) > 300:
                    snippet = snippet[:300] + "…"
                lines.append(f"  [{entry.get('ts', '')}] {entry.get('role', '?')}: {snippet}")
        return "\n".join(lines)

    def _prompt_text_with_images(self, text: str, *, image_paths: tuple[Path, ...] = ()) -> str:
        cleaned = text.strip()
        if not image_paths:
            return cleaned
        lines = [cleaned, "", "Attached image files from the bridge (including WhatsApp media):"]
        for path in image_paths:
            resolved = path.resolve()
            lines.append(f"- {resolved} (workspace path: {self.sandbox.display_path(path)})")
        lines.append("Inspect those local image files as part of this request.")
        return "\n".join(line for line in lines if line is not None).strip()

    def _prompt_text_with_attachments(
        self,
        text: str,
        *,
        image_paths: tuple[Path, ...] = (),
        file_paths: tuple[Path, ...] = (),
    ) -> str:
        prompt = self._prompt_text_with_images(text, image_paths=image_paths)
        if not file_paths:
            return prompt
        lines = [prompt, "", "Attached files from the bridge:"]
        for path in file_paths:
            resolved = path.resolve()
            lines.append(f"- {resolved} (workspace path: {self.sandbox.display_path(path)})")
        lines.append("Read those local files as part of this request.")
        return "\n".join(line for line in lines if line is not None).strip()

    def _message_text(self, message) -> str | None:
        text = getattr(message, "text", None)
        if text is not None:
            text = text.strip()
            if text:
                return text
        caption = getattr(message, "caption", None)
        if caption is None:
            return None
        caption = caption.strip()
        return caption or None

    async def _download_prompt_images(self, message, context) -> tuple[Path, ...] | None:
        attachments = self._image_attachments(message)
        if not attachments:
            return ()
        paths: list[Path] = []
        for attachment, suffix in attachments:
            file_size = getattr(attachment, "file_size", None)
            if file_size and file_size > self.settings.screenshot_max_bytes:
                await message.reply_text("Image is too large.")
                return None
            telegram_file = await context.bot.get_file(attachment.file_id)
            target = self._prompt_image_path(
                getattr(attachment, "file_unique_id", None) or attachment.file_id,
                suffix,
            )
            await telegram_file.download_to_drive(custom_path=target)
            if target.stat().st_size > self.settings.screenshot_max_bytes:
                target.unlink(missing_ok=True)
                await message.reply_text("Image is too large.")
                return None
            paths.append(target)
        return tuple(paths)

    async def _download_prompt_files(self, message, context) -> tuple[Path, ...] | None:
        attachments = self._file_attachments(message)
        if not attachments:
            return ()
        paths: list[Path] = []
        for attachment in attachments:
            file_size = getattr(attachment, "file_size", None)
            if file_size and file_size > self.settings.sendfile_max_bytes:
                await message.reply_text("File is too large.")
                return None
            telegram_file = await context.bot.get_file(attachment.file_id)
            target = self._prompt_file_path(attachment)
            await telegram_file.download_to_drive(custom_path=target)
            if target.stat().st_size > self.settings.sendfile_max_bytes:
                target.unlink(missing_ok=True)
                await message.reply_text("File is too large.")
                return None
            paths.append(target)
        return tuple(paths)

    def _image_attachments(self, message) -> list[tuple[object, str]]:
        attachments: list[tuple[object, str]] = []
        photos = getattr(message, "photo", None) or ()
        if photos:
            photo = photos[-1]
            attachments.append((photo, ".jpg"))
        document = getattr(message, "document", None)
        if document is not None and _document_is_image(document):
            attachments.append(
                (
                    document,
                    _image_suffix(
                        getattr(document, "file_name", None),
                        getattr(document, "mime_type", None),
                    ),
                )
            )
        return attachments

    def _file_attachments(self, message) -> list[object]:
        document = getattr(message, "document", None)
        if document is None:
            return []
        if _document_is_image(document):
            return []
        return [document]

    def _prompt_image_path(self, file_unique_id: str, suffix: str) -> Path:
        target_dir = self.settings.workspace_root / ".clicourier" / "incoming-media"
        target_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        return target_dir / f"{timestamp}-{file_unique_id}{suffix}"

    def _prompt_file_path(self, attachment) -> Path:
        target_dir = self.settings.workspace_root / ".clicourier" / "incoming-files"
        target_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        file_unique_id = getattr(attachment, "file_unique_id", None) or getattr(attachment, "file_id", "file")
        file_name = _safe_incoming_file_name(getattr(attachment, "file_name", None))
        return target_dir / f"{timestamp}-{_safe_path_component(file_unique_id)}-{file_name}"

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
            cleaned = _strip_agent_input_echo_prefixes(line, echoes)
            normalized = normalize_echo_text(cleaned)
            if not normalized:
                continue
            if any(_echo_matches(normalized, echo) for echo in echoes):
                continue
            lines.append(cleaned)
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
            if is_message_too_long_error(exc) and isinstance(kwargs.get("text"), str):
                return await self._safe_send_message_chunks(bot, **kwargs)
            print(f"telegram send failed: {exc}", flush=True)
            return None

    async def _safe_send_message_chunks(self, bot, **kwargs):
        text = kwargs.pop("text")
        limit = max(200, min(self.settings.max_telegram_chunk_chars, TELEGRAM_SAFE_LIMIT))
        chunks = list(_telegram_plain_chunks(text, limit))
        sent = None
        for index, chunk in enumerate(chunks):
            chunk_kwargs = dict(kwargs)
            if index > 0:
                chunk_kwargs.pop("reply_markup", None)
            try:
                sent = await bot.send_message(text=chunk, **chunk_kwargs)
            except Exception as exc:  # noqa: BLE001 - Telegram errors should not crash the bridge
                print(f"telegram send chunk failed: {exc}", flush=True)
                return sent
        return sent

    async def _safe_send_chat_action(self, bot, **kwargs) -> bool:
        try:
            await bot.send_chat_action(**kwargs)
        except Exception as exc:  # noqa: BLE001 - Telegram errors should not crash the bridge
            print(f"telegram chat action failed: {exc}", flush=True)
            return False
        return True

    def _record_session_event(self, session: AgentSession, event: AgentEvent) -> None:
        record = getattr(session, "_record_event", None)
        if callable(record):
            record(event)

    async def _apply_choice_action(self, action: PendingAction, choice: PendingActionChoice) -> None:
        agent = self.state.active_agent
        if agent is None:
            raise RuntimeError("Agent is not running.")
        self._prepare_choice_output(action)
        if action.data.get("input_mode") == "terminal_navigation":
            await self._apply_terminal_choice_action(action, choice)
            return
        value = choice.value or choice.id or choice.label
        send_choice = getattr(agent, "send_choice", None)
        if callable(send_choice):
            await send_choice(value)
        else:
            send_approval = getattr(agent, "send_approval", None)
            if callable(send_approval):
                await send_approval(value)
            else:
                await agent.send_text(value)
        self.state.clear_pending_action(action.id)

    async def _apply_terminal_choice_action(
        self,
        action: PendingAction,
        choice: PendingActionChoice,
    ) -> None:
        agent = self.state.active_agent
        if agent is None:
            raise RuntimeError("Agent is not running.")
        try:
            target_index = int(choice.value or choice.id) - 1
            selected_index = int(action.data.get("selected_index", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Choice is no longer valid.") from exc
        if target_index < 0 or target_index >= len(action.choices):
            raise RuntimeError("Choice is no longer valid.")
        send_key = getattr(agent, "send_key", None)
        if not callable(send_key):
            raise RuntimeError("Agent does not support terminal menu navigation.")
        direction = "Down" if target_index >= selected_index else "Up"
        for _ in range(abs(target_index - selected_index)):
            await send_key(direction)
        await send_key("Enter")
        self.state.clear_pending_action(action.id)

    def _prepare_choice_output(self, action: PendingAction) -> None:
        if action.chat_id is None:
            return
        self._clear_terminal_progress(action.chat_id)
        self._interactive_output_chats.add(action.chat_id)

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
        buffered_lines = [line for line in buffered.splitlines() if line.strip()]
        final_lines = [line for line in final.splitlines() if line.strip()]
        if self._final_is_shortened_tail(buffered_lines, final_lines):
            return buffered
        return final

    def _select_complete_lines(
        self,
        progress_lines: list[str],
        final_lines: list[str],
    ) -> list[str]:
        if not progress_lines:
            return final_lines
        if not final_lines:
            return progress_lines
        progress_text = "\n".join(progress_lines).strip()
        final_text = "\n".join(final_lines).strip()
        if not progress_text:
            return final_lines
        if not final_text:
            return progress_lines
        if progress_text == final_text:
            return final_lines
        if len(progress_lines) > len(final_lines) and (
            progress_text.endswith(final_text) or final_text in progress_text
        ):
            return progress_lines
        if self._final_is_shortened_tail(progress_lines, final_lines):
            return progress_lines
        return final_lines

    def _final_is_shortened_tail(
        self,
        buffered_lines: list[str],
        final_lines: list[str],
    ) -> bool:
        if len(buffered_lines) < 2 or not final_lines or len(final_lines) >= len(buffered_lines):
            return False
        tail = buffered_lines[-len(final_lines):]
        if tail == final_lines:
            return True
        return buffered_lines[-1].strip() == final_lines[-1].strip()


def approval_decision_from_reactions(reactions) -> ApprovalDecision | None:
    for reaction in reversed(reactions):
        decision = interpret_approval_text(getattr(reaction, "emoji", ""))
        if decision is not None:
            return decision
    return None


def _active_config_path() -> Path:
    configured = os.environ.get("CLICOURIER_CONFIG")
    return Path(configured).expanduser() if configured else default_config_path()


def _bridge_restart_command(*, no_resume: bool = False) -> list[str]:
    command = [sys.executable, "-m", "cli_courier.cli", "restart", "--detach"]
    if no_resume:
        command.append("--no-resume")
    return command


def _resume_requested(args: str) -> bool:
    tokens = {token.lower() for token in args.split()}
    return bool(tokens & {"resume", "--resume"})


def _no_resume_requested(args: str) -> bool:
    tokens = {token.lower() for token in args.split()}
    return bool(tokens & {"fresh", "--fresh", "no-resume", "--no-resume"})


def detect_interactive_choices(text: str) -> tuple[str, list[str], int] | None:
    """Detect narrow Codex terminal menus that require key navigation."""
    lines = [line.rstrip() for line in sanitize_terminal_text(text).splitlines()]
    for selected_line_index in range(len(lines) - 1, -1, -1):
        if not _terminal_choice_line_has_marker(lines[selected_line_index]):
            continue
        selected_label = _terminal_choice_label(lines[selected_line_index])
        if not selected_label:
            continue
        prompt_index = _find_terminal_choice_prompt(lines, selected_line_index)
        if prompt_index is None:
            continue
        labels: list[str] = []
        selected_index = 0
        for line in lines[prompt_index + 1 : prompt_index + 30]:
            label = _terminal_choice_label(line)
            if label:
                if _terminal_choice_line_has_marker(line):
                    selected_index = len(labels)
                labels.append(label)
                continue
            if labels and not line.strip():
                break
        if len(labels) >= 2:
            return lines[prompt_index].strip(), labels, selected_index
    return None


PROMPT_PLACEHOLDERS = {"{{prompt}}", "{prompt}", "<prompt>", "[prompt]", "explain this codebase"}
TERMINAL_CHOICE_PROMPTS = {
    "select reasoning effort",
    "select model",
    "select model and effort",
}
TERMINAL_CHOICE_PROMPT_RE = re.compile(
    r"^(?:select|choose|pick|switch|enable|apply|import|configure)\b.*",
    re.IGNORECASE,
)
TERMINAL_NUMBERED_CHOICE_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*(.+?)\s*$")
TERMINAL_CHOICE_INSTRUCTION_PREFIXES = (
    "access legacy models",
    "current selected",
    "loading ",
    "no additional ",
    "pick a quick ",
    "press enter ",
    "this updates ",
    "type to search ",
    "uses fewer ",
)


def _choice_prompt_text(event: AgentEvent) -> str | None:
    saw_placeholder = False
    for value in (
        event.text,
        event.data.get("prompt"),
        event.data.get("title"),
    ):
        text = _clean_choice_text(value)
        if text:
            return text
        saw_placeholder = saw_placeholder or _looks_like_choice_placeholder(value)
    if saw_placeholder:
        return None
    return "Select an option."


def _clean_choice_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or _looks_like_choice_placeholder(value):
        return ""
    return text


def _looks_like_choice_placeholder(value: object) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    first_line = text.splitlines()[0].strip().lstrip("›>").strip()
    return first_line.lower() in PROMPT_PLACEHOLDERS


def _find_terminal_choice_prompt(lines: list[str], selected_line_index: int) -> int | None:
    for index in range(selected_line_index - 1, max(-1, selected_line_index - 12), -1):
        if _looks_like_terminal_choice_prompt(lines[index]):
            return index
    return None


def _looks_like_terminal_choice_prompt(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if any(lowered.startswith(prefix) for prefix in TERMINAL_CHOICE_INSTRUCTION_PREFIXES):
        return False
    return lowered in TERMINAL_CHOICE_PROMPTS or bool(TERMINAL_CHOICE_PROMPT_RE.match(stripped))


def _terminal_choice_line_has_marker(line: str) -> bool:
    return line.lstrip().startswith(("›", ">"))


def _terminal_choice_label(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith(("›", ">")):
        label = stripped[1:].strip()
    elif line.startswith(("  ", "\t")):
        label = stripped
    elif TERMINAL_NUMBERED_CHOICE_RE.match(stripped):
        label = stripped
    else:
        return ""
    label = _strip_terminal_choice_number(label)
    if not _looks_like_terminal_choice_label(label):
        return ""
    return label


def _strip_terminal_choice_number(label: str) -> str:
    match = TERMINAL_NUMBERED_CHOICE_RE.match(label)
    if match is None:
        return label
    return match.group(2).strip()


def _looks_like_terminal_choice_label(label: str) -> bool:
    lowered = label.lower()
    if not label or len(label) > 240:
        return False
    if "·" in label or "~/" in label:
        return False
    if lowered in PROMPT_PLACEHOLDERS:
        return False
    if any(lowered.startswith(prefix) for prefix in TERMINAL_CHOICE_INSTRUCTION_PREFIXES):
        return False
    if lowered.startswith(("tip:", "warning:", "error:")):
        return False
    if lowered.startswith(("›", ">")):
        return False
    return True


def _choice_button_label(index: int, label: str) -> str:
    text = f"{index}. {label}"
    if len(text) <= 64:
        return text
    return f"{text[:61].rstrip()}..."


def normalize_echo_text(text: str) -> str:
    cleaned = sanitize_terminal_text(text)
    return " ".join(cleaned.split()).strip()


def _echo_matches(line: str, echo: str) -> bool:
    if line == echo:
        return True
    if echo and line.replace(echo, "").strip() == "":
        return True
    if len(echo) >= 80 and (line in echo or echo in line):
        return True
    return False


def _strip_agent_input_echo_prefixes(line: str, echoes: list[str]) -> str:
    cleaned = line
    for echo in echoes:
        cleaned = _strip_repeated_echo_prefix(cleaned, echo)
    return cleaned


def _strip_repeated_echo_prefix(line: str, echo: str) -> str:
    if not echo:
        return line
    working = line.lstrip()
    removed = 0
    while True:
        working = _strip_agent_input_prompt_marker(working)
        if not working.startswith(echo):
            break
        working = working[len(echo) :]
        removed += 1
    if removed == 0:
        return line
    if removed == 1 and working.strip():
        return line
    return working.lstrip()


def _strip_agent_input_prompt_marker(line: str) -> str:
    working = line.lstrip()
    while working.startswith(("›", ">")):
        working = working[1:].lstrip()
    return re.sub(r"^[\u2580-\u259f\u25a0-\u25ff]\s*", "", working).lstrip()


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
IMAGE_DOCUMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_MIME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
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


def _document_is_image(document) -> bool:
    mime_type = (getattr(document, "mime_type", None) or "").lower()
    if mime_type.startswith("image/"):
        return True
    file_name = getattr(document, "file_name", None)
    if not file_name:
        return False
    return Path(file_name).suffix.lower() in IMAGE_DOCUMENT_EXTENSIONS


def _image_suffix(file_name: str | None, mime_type: str | None) -> str:
    if file_name:
        suffix = Path(file_name).suffix.lower()
        if suffix in IMAGE_DOCUMENT_EXTENSIONS:
            return suffix
    if mime_type:
        suffix = IMAGE_MIME_SUFFIXES.get(mime_type.lower())
        if suffix is not None:
            return suffix
    return ".jpg"


def _safe_incoming_file_name(file_name: str | None) -> str:
    if file_name:
        name = Path(file_name).name
    else:
        name = "file"
    safe_name = _safe_path_component(name).strip("._-")
    return safe_name or "file"


def _safe_path_component(value: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-")
    return cleaned or "file"


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


def _is_telegram_message_not_modified(exc: Exception) -> bool:
    text = str(exc).lower()
    return "message is not modified" in text or "message not modified" in text


def _telegram_plain_chunks(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for char in text:
        char_size = telegram_text_size(char)
        if current and current_size + char_size > limit:
            chunks.append("".join(current))
            current = []
            current_size = 0
        current.append(char)
        current_size += char_size
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def _html_pre_chunks(text: str, limit: int) -> list[str]:
    wrapper_size = telegram_text_size("<pre></pre>")
    chunks: list[str] = []
    current: list[str] = []
    current_size = wrapper_size
    for char in text:
        char_size = telegram_text_size(html.escape(char, quote=False))
        if current and current_size + char_size > limit:
            chunks.append("".join(current))
            current = []
            current_size = wrapper_size
        current.append(char)
        current_size += char_size
    if current:
        chunks.append("".join(current))
    return chunks


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


_TOOL_RESULT_MAX_LINES = 30


def _truncate_tool_result(text: str) -> str:
    lines = text.splitlines()
    if len(lines) <= _TOOL_RESULT_MAX_LINES:
        return text
    kept = lines[:_TOOL_RESULT_MAX_LINES]
    omitted = len(lines) - _TOOL_RESULT_MAX_LINES
    kept.append(f"... ({omitted} more lines)")
    return "\n".join(kept)
