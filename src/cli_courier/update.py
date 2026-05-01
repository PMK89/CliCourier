from __future__ import annotations

import os
import subprocess
import shutil
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGE_NAME = "cli-courier"
DEFAULT_REPO_URL = "https://github.com/PMK89/CliCourier.git"


@dataclass
class UpdateResult:
    success: bool
    before_hash: str
    after_hash: str
    changed: bool
    lines: list[str] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> str:
        parts: list[str] = []
        if self.changed:
            parts.append(f"Updated {self.before_hash} -> {self.after_hash}")
        else:
            parts.append(f"Already up to date ({self.after_hash})")
        if self.error:
            parts.append(f"Error: {self.error}")
        else:
            parts.append("Dependencies reinstalled.")
            parts.append("Run `clicourier restart` (or /restart) to apply.")
        return "\n".join(parts)


def find_repo_root() -> Path:
    import cli_courier
    package_dir = Path(cli_courier.__file__).resolve().parent
    candidate = package_dir.parents[1]
    if (candidate / "pyproject.toml").exists() and (candidate / ".git").exists():
        return candidate
    raise RuntimeError(f"Cannot locate CliCourier repo root (checked {candidate})")


def run_update() -> UpdateResult:
    try:
        repo = find_repo_root()
    except RuntimeError as exc:
        return run_tool_update(repo_error=str(exc))

    _git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env=_git_env,
            timeout=60,
        )

    before_result = git("rev-parse", "--short", "HEAD")
    before_hash = before_result.stdout.strip() or "?"

    lines: list[str] = []

    try:
        fetch = git("fetch", "origin", "main")
    except subprocess.TimeoutExpired:
        return UpdateResult(
            success=False,
            before_hash=before_hash,
            after_hash=before_hash,
            changed=False,
            lines=lines,
            error="git fetch timed out after 60s",
        )
    if fetch.returncode != 0:
        msg = (fetch.stderr or fetch.stdout).strip()
        return UpdateResult(
            success=False,
            before_hash=before_hash,
            after_hash=before_hash,
            changed=False,
            lines=lines,
            error=f"git fetch failed: {msg}",
        )
    lines.append("Fetched origin/main.")

    try:
        merge = git("merge", "--ff-only", "origin/main")
    except subprocess.TimeoutExpired:
        return UpdateResult(
            success=False,
            before_hash=before_hash,
            after_hash=before_hash,
            changed=False,
            lines=lines,
            error="git merge timed out after 60s",
        )
    if merge.returncode != 0:
        msg = (merge.stderr or merge.stdout).strip()
        return UpdateResult(
            success=False,
            before_hash=before_hash,
            after_hash=before_hash,
            changed=False,
            lines=lines,
            error=f"git merge failed: {msg}",
        )
    lines.append((merge.stdout or merge.stderr).strip())

    after_hash = git("rev-parse", "--short", "HEAD").stdout.strip() or before_hash
    changed = before_hash != after_hash

    uv = shutil.which("uv")
    if uv is None:
        return UpdateResult(
            success=False,
            before_hash=before_hash,
            after_hash=after_hash,
            changed=changed,
            lines=lines,
            error="uv not found on PATH; reinstall with the one-command installer",
        )
    try:
        install = subprocess.run(
            [
                uv,
                "tool",
                "install",
                "--force",
                "--upgrade",
                "--reinstall-package",
                PACKAGE_NAME,
                "--editable",
                str(repo),
            ],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return UpdateResult(
            success=False,
            before_hash=before_hash,
            after_hash=after_hash,
            changed=changed,
            lines=lines,
            error="uv install timed out after 300s",
        )
    if install.returncode != 0:
        msg = (install.stderr or install.stdout).strip()
        return UpdateResult(
            success=False,
            before_hash=before_hash,
            after_hash=after_hash,
            changed=changed,
            lines=lines,
            error=f"uv install failed: {msg}",
        )
    lines.append("uv tool install complete.")

    return UpdateResult(
        success=True,
        before_hash=before_hash,
        after_hash=after_hash,
        changed=changed,
        lines=lines,
    )


def run_tool_update(*, repo_error: str) -> UpdateResult:
    before_version = installed_version()
    uv = shutil.which("uv")
    if uv is None:
        return UpdateResult(
            success=False,
            before_hash=before_version,
            after_hash=before_version,
            changed=False,
            error=(
                f"{repo_error}; uv not found on PATH. Reinstall with: "
                "curl -LsSf https://raw.githubusercontent.com/PMK89/CliCourier/main/install.sh | sh"
            ),
        )
    lines = [repo_error, "No editable checkout found; reinstalling the uv tool from GitHub."]
    target = f"git+{DEFAULT_REPO_URL}"
    try:
        install = subprocess.run(
            [
                uv,
                "tool",
                "install",
                "--force",
                "--upgrade",
                "--reinstall-package",
                PACKAGE_NAME,
                target,
            ],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return UpdateResult(
            success=False,
            before_hash=before_version,
            after_hash=before_version,
            changed=False,
            lines=lines,
            error="uv install timed out after 300s",
        )
    if install.returncode != 0:
        msg = (install.stderr or install.stdout).strip()
        return UpdateResult(
            success=False,
            before_hash=before_version,
            after_hash=before_version,
            changed=False,
            lines=lines,
            error=f"uv install failed: {msg}",
        )
    lines.append("uv tool install complete.")
    return UpdateResult(
        success=True,
        before_hash=before_version,
        after_hash="latest",
        changed=True,
        lines=lines,
    )


def installed_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "?"
