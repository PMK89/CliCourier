from __future__ import annotations

from pathlib import Path

from cli_courier.config import Settings, load_settings
from cli_courier.filesystem import Sandbox
from cli_courier.screenshots import ScreenshotService
from cli_courier.state import RuntimeState
from cli_courier.telegram_bot.runtime import TelegramBridgeBot, build_transcriber


def build_bot(settings: Settings | None = None) -> TelegramBridgeBot:
    loaded = settings or load_settings()
    state = RuntimeState.create(loaded.workspace_root)
    sandbox = Sandbox(
        loaded.workspace_root,
        cat_max_bytes=loaded.cat_max_bytes,
        sendfile_max_bytes=loaded.sendfile_max_bytes,
        allow_sensitive_file_send=loaded.allow_sensitive_file_send,
    )
    screenshots = ScreenshotService(
        workspace_root=loaded.workspace_root,
        screenshot_dir=loaded.screenshot_dir,
        max_bytes=loaded.screenshot_max_bytes,
        allow_outside_workspace=loaded.allow_screenshot_dir_outside_workspace,
    )
    return TelegramBridgeBot(
        settings=loaded,
        state=state,
        sandbox=sandbox,
        screenshot_service=screenshots,
        transcriber=build_transcriber(loaded),
    )


def main(*, config_path: Path | None = None) -> None:
    bot = build_bot(load_settings(config_path))
    application = bot.build_application()
    application.run_polling()
