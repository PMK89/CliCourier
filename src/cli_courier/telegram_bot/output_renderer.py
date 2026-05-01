from __future__ import annotations

import asyncio
import html
import re
import time
from collections.abc import Awaitable, Callable, Sequence

TELEGRAM_HARD_LIMIT = 4096
TELEGRAM_SAFE_LIMIT = 3900
DEFAULT_MAX_LINES = 60
TELEGRAM_CLI_PARSE_MODE = "HTML"

FieldLogCallback = Callable[..., None]
SleepCallback = Callable[[float], Awaitable[None]]


class OutputLineBuffer:
    """Rolling line buffer that preserves an incomplete trailing line."""

    def __init__(self, *, max_lines: int = DEFAULT_MAX_LINES) -> None:
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")
        self.max_lines = max_lines
        self._lines: list[str] = []
        self._partial_line = ""
        self.completed_line_count = 0

    @property
    def partial_line(self) -> str:
        return self._partial_line

    def append_chunk(self, text: str) -> int:
        if not text:
            return 0
        appended = 0
        for segment in text.splitlines(keepends=True):
            if segment.endswith(("\n", "\r")):
                self._append_line((self._partial_line + segment).rstrip("\r\n"))
                self._partial_line = ""
                appended += 1
            else:
                self._partial_line += segment
        return appended

    def flush_partial(self) -> bool:
        if self._partial_line == "":
            return False
        self._append_line(self._partial_line.rstrip("\r\n"))
        self._partial_line = ""
        return True

    def replace_lines(self, lines: Sequence[str]) -> None:
        self._lines = [str(line).rstrip("\r\n") for line in lines][-self.max_lines :]
        self._partial_line = ""
        self.completed_line_count = len(lines)

    def latest_lines(self) -> list[str]:
        return list(self._lines[-self.max_lines :])

    def latest_lines_with_partial(self) -> list[str]:
        lines = self.latest_lines()
        if self._partial_line:
            lines.append(self._partial_line.rstrip("\r\n"))
        return lines[-self.max_lines :]

    def has_output(self) -> bool:
        return bool(self._lines or self._partial_line)

    def _append_line(self, line: str) -> None:
        self._lines.append(line)
        self.completed_line_count += 1
        del self._lines[:-self.max_lines]


class TelegramEditableOutputMessage:
    """Send once, then edit the same Telegram message for rolling output."""

    def __init__(
        self,
        *,
        chat_id: int,
        max_lines: int = DEFAULT_MAX_LINES,
        safe_char_limit: int = TELEGRAM_SAFE_LIMIT,
        min_edit_interval_seconds: float = 1.0,
        sleep: SleepCallback = asyncio.sleep,
        log: FieldLogCallback | None = None,
    ) -> None:
        if safe_char_limit <= 0:
            raise ValueError("safe_char_limit must be positive")
        self.chat_id = chat_id
        self.max_lines = max_lines
        self.safe_char_limit = min(safe_char_limit, TELEGRAM_HARD_LIMIT)
        self.min_edit_interval_seconds = max(0.0, min_edit_interval_seconds)
        self._sleep = sleep
        self._log = log or (lambda _action, **_fields: None)
        self._lock = asyncio.Lock()
        self.message_id: int | None = None
        self.last_text: str | None = None
        self.last_render_at = 0.0

    async def render(
        self,
        bot,
        lines: Sequence[str],
        *,
        running: bool,
        force: bool = False,
        disable_notification: bool = False,
    ) -> bool:
        if not lines:
            return False
        text = render_output_window(
            lines,
            running=running,
            max_lines=self.max_lines,
            limit=self.safe_char_limit,
        )
        if not text.strip():
            return False

        async with self._lock:
            if self.last_text == text:
                self._log("progress_render_skipped", message_id=self.message_id, reason="unchanged")
                return False
            now = time.monotonic()
            if (
                self.message_id is not None
                and not force
                and self.last_render_at
                and now - self.last_render_at < self.min_edit_interval_seconds
            ):
                self._log(
                    "progress_render_skipped",
                    message_id=self.message_id,
                    reason="throttled",
                    wait_seconds=round(self.min_edit_interval_seconds - (now - self.last_render_at), 3),
                )
                return False
            if self.message_id is None:
                return await self._send(
                    bot,
                    lines,
                    running=running,
                    disable_notification=disable_notification,
                )
            return await self._edit(bot, lines, running=running)

    async def _send(
        self,
        bot,
        lines: Sequence[str],
        *,
        running: bool,
        disable_notification: bool,
    ) -> bool:
        limit = self.safe_char_limit
        last_error: Exception | None = None
        for attempt in range(1, 4):
            text = render_output_window(lines, running=running, max_lines=self.max_lines, limit=limit)
            self._log(
                "progress_send_attempt",
                chars=len(text),
                lines=count_output_lines(text),
                attempt=attempt,
            )
            try:
                sent = await bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=TELEGRAM_CLI_PARSE_MODE,
                    disable_notification=disable_notification,
                )
                message_id = getattr(sent, "message_id", None)
                if message_id is None:
                    raise RuntimeError("Telegram send_message returned no message_id")
                self.message_id = int(message_id)
                self.last_text = text
                self.last_render_at = time.monotonic()
                self._log(
                    "progress_send_ok",
                    message_id=self.message_id,
                    chars=len(text),
                    lines=count_output_lines(text),
                )
                return True
            except Exception as exc:  # noqa: BLE001 - Telegram API shapes vary by version
                last_error = exc
                retry = await self._handle_retryable_error(
                    action="progress_send",
                    exc=exc,
                    limit=limit,
                )
                if retry is None:
                    break
                limit = retry
        self._log("progress_send_failed", error=_format_error(last_error))
        return False

    async def _edit(self, bot, lines: Sequence[str], *, running: bool) -> bool:
        edit_message_text = getattr(bot, "edit_message_text", None)
        if edit_message_text is None:
            self._log("progress_edit_unavailable", message_id=self.message_id)
            return False

        limit = self.safe_char_limit
        last_error: Exception | None = None
        for attempt in range(1, 4):
            text = render_output_window(lines, running=running, max_lines=self.max_lines, limit=limit)
            if self.last_text == text:
                self._log("progress_render_skipped", message_id=self.message_id, reason="unchanged")
                return False
            self._log(
                "progress_edit_attempt",
                message_id=self.message_id,
                chars=len(text),
                lines=count_output_lines(text),
                attempt=attempt,
            )
            try:
                await edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                    parse_mode=TELEGRAM_CLI_PARSE_MODE,
                )
                self.last_text = text
                self.last_render_at = time.monotonic()
                self._log(
                    "progress_edit_ok",
                    message_id=self.message_id,
                    chars=len(text),
                    lines=count_output_lines(text),
                )
                return True
            except Exception as exc:  # noqa: BLE001 - Telegram API shapes vary by version
                if _is_message_not_modified(exc):
                    self.last_text = text
                    self.last_render_at = time.monotonic()
                    self._log("progress_edit_ignored", message_id=self.message_id, reason="not_modified")
                    return False
                last_error = exc
                retry = await self._handle_retryable_error(
                    action="progress_edit",
                    exc=exc,
                    limit=limit,
                    message_id=self.message_id,
                )
                if retry is None:
                    break
                limit = retry
        self._log(
            "progress_edit_failed",
            message_id=self.message_id,
            error=_format_error(last_error),
        )
        return False

    async def _handle_retryable_error(
        self,
        *,
        action: str,
        exc: Exception,
        limit: int,
        message_id: int | None = None,
    ) -> int | None:
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            self._log(
                f"{action}_retry_after",
                message_id=message_id,
                retry_after=retry_after,
                error=_format_error(exc),
            )
            await self._sleep(retry_after)
            return limit
        if _is_message_too_long(exc) and limit > 500:
            next_limit = max(500, min(limit - 500, int(limit * 0.8)))
            self._log(
                f"{action}_shrink_retry",
                message_id=message_id,
                old_limit=limit,
                new_limit=next_limit,
                error=_format_error(exc),
            )
            return next_limit
        return None


class StreamingMessageRenderer:
    """Coordinates rolling output lines with one editable Telegram message."""

    def __init__(
        self,
        *,
        chat_id: int,
        max_lines: int = DEFAULT_MAX_LINES,
        safe_char_limit: int = TELEGRAM_SAFE_LIMIT,
        min_edit_interval_seconds: float = 1.0,
        sleep: SleepCallback = asyncio.sleep,
        log: FieldLogCallback | None = None,
    ) -> None:
        self.buffer = OutputLineBuffer(max_lines=max_lines)
        self.finished = False
        self.message = TelegramEditableOutputMessage(
            chat_id=chat_id,
            max_lines=max_lines,
            safe_char_limit=safe_char_limit,
            min_edit_interval_seconds=min_edit_interval_seconds,
            sleep=sleep,
            log=log,
        )

    @property
    def message_id(self) -> int | None:
        return self.message.message_id

    def append_chunk(self, text: str) -> int:
        if text:
            self.finished = False
        return self.buffer.append_chunk(text)

    def flush_partial(self) -> bool:
        return self.buffer.flush_partial()

    def replace_lines(self, lines: Sequence[str]) -> None:
        self.buffer.replace_lines(lines)

    def latest_lines(self, *, include_partial: bool = False) -> list[str]:
        if include_partial:
            return self.buffer.latest_lines_with_partial()
        return self.buffer.latest_lines()

    async def render(
        self,
        bot,
        *,
        running: bool,
        force: bool = False,
        disable_notification: bool = False,
    ) -> bool:
        if running and self.finished:
            return False
        rendered = await self.message.render(
            bot,
            self.latest_lines(),
            running=running,
            force=force,
            disable_notification=disable_notification,
        )
        if not running:
            self.finished = True
        return rendered


def render_output_window(
    lines: Sequence[str],
    *,
    running: bool,
    max_lines: int = DEFAULT_MAX_LINES,
    limit: int = TELEGRAM_SAFE_LIMIT,
) -> str:
    if limit <= 0:
        return ""
    visible_lines = [str(line).rstrip("\r\n") for line in lines][-max_lines:]
    header = (
        f"Running.\nShowing latest {max_lines} lines"
        if running
        else f"Finished.\nShowing final {max_lines} lines"
    )
    kept = list(visible_lines)
    while kept:
        text = _compose(header, kept)
        if len(text) <= limit:
            return text
        if len(kept) == 1:
            return _compose(header, [_trim_single_line_to_fit(header, kept[0], limit)])
        kept = kept[1:]
    return header[:limit]


def count_output_lines(text: str) -> int:
    return len(text.splitlines())


def _compose(header: str, lines: Sequence[str]) -> str:
    body = "\n".join(lines).strip("\n")
    if not body:
        return header
    return f"{header}\n\n<pre>{html.escape(body, quote=False)}</pre>"


def _trim_single_line_to_fit(header: str, line: str, limit: int) -> str:
    if len(_compose(header, [""])) > limit:
        return ""
    low = 0
    high = len(line)
    best = ""
    while low <= high:
        size = (low + high) // 2
        candidate = line[-size:] if size else ""
        if len(_compose(header, [candidate])) <= limit:
            best = candidate
            low = size + 1
        else:
            high = size - 1
    return best


def _format_error(exc: Exception | None) -> str:
    if exc is None:
        return "unknown"
    return f"{type(exc).__name__}: {exc}"


def _is_message_not_modified(exc: Exception) -> bool:
    text = str(exc).lower()
    return "message is not modified" in text or "message not modified" in text


def _is_message_too_long(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "message is too long" in text
        or "message_too_long" in text
        or "text is too long" in text
        or "message text is too long" in text
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            return None
    match = re.search(r"retry (?:after|in) (\d+(?:\.\d+)?)", str(exc), flags=re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None
