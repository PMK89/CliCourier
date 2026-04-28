from __future__ import annotations

from types import SimpleNamespace

from cli_courier.telegram_bot.output_renderer import (
    StreamingMessageRenderer,
    render_output_window,
)


class FakeTelegramBot:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.send_calls: list[tuple[int, int, str]] = []
        self.edit_calls: list[tuple[int, int, str]] = []

    async def send_message(self, *, chat_id: int, text: str, **kwargs):
        message_id = len(self.messages) + 1
        self.messages.append(text)
        self.send_calls.append((message_id, chat_id, text))
        return SimpleNamespace(message_id=message_id)

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, **kwargs):
        self.edit_calls.append((message_id, chat_id, text))
        if 1 <= message_id <= len(self.messages):
            self.messages[message_id - 1] = text


def numbered_lines(start: int, stop: int) -> list[str]:
    return [f"LINE {index:03d}" for index in range(start, stop + 1)]


def output_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("LINE ")]


def test_render_fewer_than_sixty_lines() -> None:
    text = render_output_window(numbered_lines(1, 5), running=True)

    assert output_lines(text) == numbered_lines(1, 5)


def test_render_exactly_sixty_lines() -> None:
    text = render_output_window(numbered_lines(1, 60), running=True)

    assert output_lines(text) == numbered_lines(1, 60)


def test_render_more_than_sixty_lines_uses_latest_sixty() -> None:
    text = render_output_window(numbered_lines(1, 150), running=True)

    assert output_lines(text) == numbered_lines(91, 150)
    assert "LINE 001" not in text


def test_long_lines_fit_under_telegram_safe_limit() -> None:
    lines = [f"LINE {index:03d} " + ("x" * 500) for index in range(1, 61)]
    text = render_output_window(lines, running=True, limit=3900)

    assert len(text) <= 3900
    assert output_lines(text)[-1].startswith("LINE 060")


async def test_duplicate_renders_do_not_trigger_edit() -> None:
    bot = FakeTelegramBot()
    renderer = StreamingMessageRenderer(chat_id=100, min_edit_interval_seconds=0)
    renderer.replace_lines(numbered_lines(1, 3))

    await renderer.render(bot, running=True)
    await renderer.render(bot, running=True)

    assert len(bot.send_calls) == 1
    assert bot.edit_calls == []


async def test_first_render_sends_message_once() -> None:
    bot = FakeTelegramBot()
    renderer = StreamingMessageRenderer(chat_id=100, min_edit_interval_seconds=0)
    renderer.append_chunk("LINE 001\nLINE 002\n")

    await renderer.render(bot, running=True)

    assert len(bot.send_calls) == 1
    assert bot.send_calls[0][1] == 100
    assert renderer.message_id == 1


async def test_subsequent_renders_edit_same_message_id() -> None:
    bot = FakeTelegramBot()
    renderer = StreamingMessageRenderer(chat_id=100, min_edit_interval_seconds=0)
    renderer.append_chunk("LINE 001\n")
    await renderer.render(bot, running=True)

    renderer.append_chunk("LINE 002\n")
    await renderer.render(bot, running=True)

    assert len(bot.send_calls) == 1
    assert len(bot.edit_calls) == 1
    assert bot.edit_calls[0][0] == 1
    assert bot.edit_calls[0][1] == 100
    assert output_lines(bot.messages[0]) == numbered_lines(1, 2)


async def test_final_render_forces_edit_when_output_changed() -> None:
    bot = FakeTelegramBot()
    renderer = StreamingMessageRenderer(chat_id=100, min_edit_interval_seconds=3600)
    renderer.append_chunk("LINE 001\n")
    await renderer.render(bot, running=True)

    renderer.append_chunk("".join(f"LINE {index:03d}\n" for index in range(2, 151)))
    await renderer.render(bot, running=True)
    await renderer.render(bot, running=False, force=True)

    assert len(bot.send_calls) == 1
    assert len(bot.edit_calls) == 1
    assert bot.edit_calls[0][0] == 1
    assert "Finished." in bot.messages[0]
    assert output_lines(bot.messages[0]) == numbered_lines(91, 150)


async def test_running_render_after_final_does_not_revert_status() -> None:
    bot = FakeTelegramBot()
    renderer = StreamingMessageRenderer(chat_id=100, min_edit_interval_seconds=0)
    renderer.append_chunk("LINE 001\nLINE 002\n")
    await renderer.render(bot, running=True)
    await renderer.render(bot, running=False, force=True)
    final_text = bot.messages[0]

    await renderer.render(bot, running=True)

    assert bot.messages[0] == final_text
    assert bot.messages[0].startswith("Finished.")


async def test_one_hundred_fifty_lines_keep_one_message_and_final_latest_sixty() -> None:
    bot = FakeTelegramBot()
    renderer = StreamingMessageRenderer(chat_id=100, min_edit_interval_seconds=0)

    for index in range(1, 151):
        renderer.append_chunk(f"LINE {index:03d}\n")
        await renderer.render(bot, running=True)
    await renderer.render(bot, running=False, force=True)

    assert len(bot.send_calls) == 1
    assert bot.send_calls[0][0] == 1
    assert bot.edit_calls
    assert {message_id for message_id, _chat_id, _text in bot.edit_calls} == {1}
    assert output_lines(bot.messages[0]) == numbered_lines(91, 150)
    assert "LINE 001" not in bot.messages[0]
