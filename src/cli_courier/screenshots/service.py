from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ScreenshotError(ValueError):
    """Raised when no safe screenshot artifact can be returned."""


@dataclass(frozen=True)
class ScreenshotArtifact:
    path: Path
    mime_type: str
    size: int


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class ScreenshotService:
    def __init__(
        self,
        *,
        workspace_root: Path,
        screenshot_dir: Path | None,
        max_bytes: int,
        allow_outside_workspace: bool = False,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.screenshot_dir = screenshot_dir.resolve() if screenshot_dir else None
        self.max_bytes = max_bytes
        self.allow_outside_workspace = allow_outside_workspace
        self._validate_directory()

    def latest(self) -> ScreenshotArtifact:
        if self.screenshot_dir is None:
            raise ScreenshotError("SCREENSHOT_DIR is not configured")
        if not self.screenshot_dir.exists() or not self.screenshot_dir.is_dir():
            raise ScreenshotError(f"SCREENSHOT_DIR is not a directory: {self.screenshot_dir}")

        candidates = [
            path
            for path in self.screenshot_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        if not candidates:
            raise ScreenshotError("no screenshot image found")
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        return self._validate_artifact(newest)

    def _validate_directory(self) -> None:
        if self.screenshot_dir is None:
            return
        if self.allow_outside_workspace:
            return
        try:
            self.screenshot_dir.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ScreenshotError("SCREENSHOT_DIR escapes WORKSPACE_ROOT") from exc

    def _validate_artifact(self, path: Path) -> ScreenshotArtifact:
        resolved = path.resolve(strict=True)
        if not self.allow_outside_workspace:
            try:
                resolved.relative_to(self.workspace_root)
            except ValueError as exc:
                raise ScreenshotError("screenshot artifact escapes WORKSPACE_ROOT") from exc

        size = resolved.stat().st_size
        if size > self.max_bytes:
            raise ScreenshotError(f"screenshot is too large ({size} bytes)")

        mime_type = sniff_image_mime(resolved)
        if mime_type is None:
            raise ScreenshotError("latest screenshot does not look like a supported image")
        return ScreenshotArtifact(path=resolved, mime_type=mime_type, size=size)


def sniff_image_mime(path: Path) -> str | None:
    header = path.read_bytes()[:16]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None
