from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_courier.screenshots import ScreenshotError, ScreenshotService


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_latest_screenshot_returns_newest_valid_image(tmp_path: Path) -> None:
    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    old.write_bytes(PNG_BYTES)
    new.write_bytes(PNG_BYTES + b"new")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))

    service = ScreenshotService(
        workspace_root=tmp_path,
        screenshot_dir=tmp_path,
        max_bytes=1024,
    )

    assert service.latest().path == new.resolve()


def test_latest_screenshot_rejects_bad_mime(tmp_path: Path) -> None:
    (tmp_path / "bad.png").write_text("not an image", encoding="utf-8")
    service = ScreenshotService(
        workspace_root=tmp_path,
        screenshot_dir=tmp_path,
        max_bytes=1024,
    )

    with pytest.raises(ScreenshotError):
        service.latest()


def test_screenshot_dir_cannot_escape_workspace(tmp_path: Path) -> None:
    with pytest.raises(ScreenshotError):
        ScreenshotService(
            workspace_root=tmp_path / "workspace",
            screenshot_dir=tmp_path,
            max_bytes=1024,
        )
