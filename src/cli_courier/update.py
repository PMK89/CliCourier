from __future__ import annotations

import subprocess
import shutil
from dataclasses import dataclass, field
from pathlib import Path


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
        return UpdateResult(success=False, before_hash="?", after_hash="?", changed=False, error=str(exc))

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )

    before_result = git("rev-parse", "--short", "HEAD")
    before_hash = before_result.stdout.strip() or "?"

    lines: list[str] = []

    fetch = git("fetch", "origin", "main")
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

    merge = git("merge", "--ff-only", "origin/main")
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
            error="uv not found on PATH; run `uv tool install --force --editable .` manually",
        )
    install = subprocess.run(
        [uv, "tool", "install", "--force", "--editable", str(repo)],
        capture_output=True,
        text=True,
        check=False,
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
