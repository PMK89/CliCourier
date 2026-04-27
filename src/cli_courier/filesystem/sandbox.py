from __future__ import annotations

import fnmatch
import stat
from dataclasses import dataclass
from pathlib import Path


class SandboxViolation(ValueError):
    """Raised when a requested path violates the workspace sandbox."""


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: Path
    is_dir: bool
    size: int
    sensitive: bool

    @property
    def display_name(self) -> str:
        suffix = "/" if self.is_dir else ""
        marker = " [sensitive]" if self.sensitive else ""
        return f"{self.name}{suffix}{marker}"


SENSITIVE_DIR_NAMES = {".ssh", ".aws", ".kube", ".gnupg", ".docker"}
SENSITIVE_PATH_PART_NAMES = {
    "secret",
    "secrets",
    "private",
    "credential",
    "credentials",
    "token",
    "tokens",
    "key",
    "keys",
}
SENSITIVE_EXACT_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "pip.conf",
    "credentials",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".asc", ".gpg"}
SENSITIVE_NAME_GLOBS = (
    ".env.*",
    "*secret*",
    "*secrets*",
    "*credential*",
    "*credentials*",
    "*token*",
    "*password*",
    "*passwd*",
)


class Sandbox:
    def __init__(
        self,
        root: Path,
        *,
        cat_max_bytes: int,
        sendfile_max_bytes: int,
        allow_sensitive_file_send: bool = False,
    ) -> None:
        self.root = root.expanduser().resolve()
        if not self.root.exists() or not self.root.is_dir():
            raise SandboxViolation(f"workspace root must be an existing directory: {self.root}")
        self.cat_max_bytes = cat_max_bytes
        self.sendfile_max_bytes = sendfile_max_bytes
        self.allow_sensitive_file_send = allow_sensitive_file_send

    def resolve(self, user_path: str | None = None, *, cwd: Path | None = None) -> Path:
        raw_path = (user_path or ".").strip() or "."
        base = (cwd or self.root).resolve()
        if not self._is_under_root(base):
            raise SandboxViolation("current directory is outside the workspace")

        if raw_path.startswith("/"):
            candidate = self.root / raw_path.lstrip("/")
        else:
            candidate = base / raw_path

        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SandboxViolation(f"path does not exist: {raw_path}") from exc

        if not self._is_under_root(resolved):
            raise SandboxViolation("path escapes WORKSPACE_ROOT")
        return resolved

    def display_path(self, path: Path) -> str:
        resolved = path.resolve()
        if resolved == self.root:
            return "/"
        return "/" + resolved.relative_to(self.root).as_posix()

    def list_dir(self, user_path: str | None = None, *, cwd: Path | None = None) -> list[FileEntry]:
        path = self.resolve(user_path, cwd=cwd)
        if not path.is_dir():
            raise SandboxViolation("path is not a directory")
        entries = [self._entry(child) for child in path.iterdir()]
        return sorted(entries, key=lambda entry: (not entry.is_dir, entry.name.lower()))

    def tree(
        self,
        user_path: str | None = None,
        *,
        cwd: Path | None = None,
        max_entries: int = 200,
        max_depth: int = 3,
    ) -> str:
        root = self.resolve(user_path, cwd=cwd)
        if not root.is_dir():
            raise SandboxViolation("path is not a directory")
        lines = [self.display_path(root)]
        count = 0

        def walk(path: Path, prefix: str, depth: int) -> None:
            nonlocal count
            if count >= max_entries or depth >= max_depth:
                return
            children = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            for index, child in enumerate(children):
                if count >= max_entries:
                    lines.append(f"{prefix}...")
                    return
                entry = self._entry(child)
                connector = "`-- " if index == len(children) - 1 else "|-- "
                lines.append(f"{prefix}{connector}{entry.display_name}")
                count += 1
                if child.is_dir() and not entry.sensitive:
                    extension = "    " if index == len(children) - 1 else "|   "
                    walk(child, prefix + extension, depth + 1)

        walk(root, "", 0)
        return "\n".join(lines)

    def cat_file(self, user_path: str, *, cwd: Path | None = None) -> str:
        path = self.resolve(user_path, cwd=cwd)
        if not path.is_file():
            raise SandboxViolation("path is not a file")
        if self.is_sensitive(path):
            raise SandboxViolation("refusing to read sensitive file")
        size = path.stat().st_size
        if size > self.cat_max_bytes:
            raise SandboxViolation(f"file is too large for /cat ({size} bytes)")
        data = path.read_bytes()
        if b"\x00" in data:
            raise SandboxViolation("refusing to cat binary file")
        return data.decode("utf-8", errors="replace")

    def validate_sendfile(self, user_path: str, *, cwd: Path | None = None) -> Path:
        path = self.resolve(user_path, cwd=cwd)
        if not path.is_file():
            raise SandboxViolation("path is not a file")
        if self.is_sensitive(path) and not self.allow_sensitive_file_send:
            raise SandboxViolation("refusing to send sensitive file")
        size = path.stat().st_size
        if size > self.sendfile_max_bytes:
            raise SandboxViolation(f"file is too large for /sendfile ({size} bytes)")
        return path

    def is_sensitive(self, path: Path) -> bool:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError:
            return True
        parts = [part.lower() for part in relative.parts]
        if any(part in SENSITIVE_DIR_NAMES for part in parts):
            return True
        if any(part in SENSITIVE_PATH_PART_NAMES for part in parts[:-1]):
            return True
        name = resolved.name.lower()
        if name in SENSITIVE_EXACT_NAMES:
            return True
        if name.startswith(".env."):
            return True
        if any(name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
            return True
        return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_NAME_GLOBS)

    def _entry(self, path: Path) -> FileEntry:
        info = path.lstat()
        mode = info.st_mode
        return FileEntry(
            name=path.name,
            path=path,
            is_dir=stat.S_ISDIR(mode),
            size=info.st_size,
            sensitive=self.is_sensitive(path),
        )

    def _is_under_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True
