from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time


class ScreenshotError(ValueError):
    """Raised when no safe screenshot artifact can be returned."""


@dataclass(frozen=True)
class ScreenshotArtifact:
    path: Path
    mime_type: str
    size: int


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_SCREENSHOT_DIRS = (
    Path("output/playwright"),
    Path("output"),
    Path(".playwright-cli"),
)


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
        directories = self._search_directories()
        if not directories:
            raise ScreenshotError("SCREENSHOT_DIR is not configured and no default screenshot directory exists")

        candidates = [
            path
            for directory in directories
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        if not candidates:
            raise ScreenshotError("no screenshot image found")
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        return self._validate_artifact(newest)

    def artifact_for_reference(self, reference: str) -> ScreenshotArtifact:
        path = Path(reference).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        return self._validate_artifact(path)

    def artifacts_since(self, since: float, *, min_age_seconds: float = 0.3) -> list[ScreenshotArtifact]:
        directories = self._search_directories()
        if not directories:
            return []
        now = time.time()
        candidates = []
        for directory in directories:
            for path in directory.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                mtime = path.stat().st_mtime
                if mtime >= since and now - mtime >= min_age_seconds:
                    candidates.append(path)
        artifacts = []
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime):
            try:
                artifacts.append(self._validate_artifact(path))
            except ScreenshotError:
                continue
        return artifacts

    def recent_artifacts(self, *, limit: int = 10) -> list[ScreenshotArtifact]:
        directories = self._search_directories()
        if not directories:
            return []
        candidates = [
            path
            for directory in directories
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        artifacts = []
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                artifacts.append(self._validate_artifact(path))
            except ScreenshotError:
                continue
            if len(artifacts) >= limit:
                break
        return artifacts

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
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ScreenshotError("screenshot artifact does not exist") from exc
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

    def _search_directories(self) -> list[Path]:
        if self.screenshot_dir is not None:
            if not self.screenshot_dir.exists() or not self.screenshot_dir.is_dir():
                raise ScreenshotError(f"SCREENSHOT_DIR is not a directory: {self.screenshot_dir}")
            return [self.screenshot_dir]

        directories = []
        for relative in DEFAULT_SCREENSHOT_DIRS:
            candidate = (self.workspace_root / relative).resolve()
            if candidate.exists() and candidate.is_dir():
                directories.append(candidate)
        return directories


def sniff_image_mime(path: Path) -> str | None:
    header = path.read_bytes()[:16]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None
