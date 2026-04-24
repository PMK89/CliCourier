from __future__ import annotations

import importlib.util
import platform
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from cli_courier.config import Settings, load_settings
from cli_courier.local_config import default_config_path
from cli_courier.model_manager import model_cache_status


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def format(self) -> str:
        marker = "OK" if self.ok else "FAIL"
        return f"{marker:4} {self.name}: {self.detail}"


def is_wsl() -> bool:
    if platform.system() != "Linux":
        return False
    try:
        text = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return "microsoft" in text or "wsl" in text


def run_doctor(config_path: Path | None = None) -> int:
    checks = collect_checks(config_path)
    for check in checks:
        print(check.format())
    return 0 if all(check.ok for check in checks) else 1


def collect_checks(config_path: Path | None = None) -> list[Check]:
    path = (config_path or default_config_path()).expanduser()
    raw = dotenv_values(path) if path.exists() else {}
    settings: Settings | None = None
    settings_error = ""
    try:
        settings = load_settings(path if path.exists() else config_path)
    except Exception as exc:  # noqa: BLE001 - doctor should convert config failures into checks
        settings_error = str(exc)

    checks = [
        Check("platform", platform.system() == "Linux", "Linux/WSL" if platform.system() == "Linux" else platform.system()),
        Check("wsl", platform.system() == "Linux", "yes" if is_wsl() else "no"),
        Check("python", sys.version_info >= (3, 11), platform.python_version()),
        Check("config", path.exists(), str(path) if path.exists() else f"missing: {path}"),
    ]
    if settings is None:
        checks.append(Check("config valid", False, settings_error or "not loadable"))
    else:
        checks.extend(
            [
                Check("config valid", True, "loaded"),
                Check("telegram token", _token_present(raw), "present" if _token_present(raw) else "missing or placeholder"),
                Check(
                    "allowed users",
                    bool(settings.allowed_telegram_user_ids),
                    ",".join(str(user_id) for user_id in settings.allowed_telegram_user_ids),
                ),
                Check(
                    "workspace",
                    settings.workspace_root.exists(),
                    str(settings.workspace_root),
                ),
                _agent_command_check(settings.default_agent_command),
                Check(
                    "ffmpeg",
                    shutil.which(settings.ffmpeg_binary) is not None,
                    shutil.which(settings.ffmpeg_binary) or f"missing: {settings.ffmpeg_binary}",
                ),
                Check(
                    "faster-whisper",
                    importlib.util.find_spec("faster_whisper") is not None,
                    "importable" if importlib.util.find_spec("faster_whisper") else "not installed",
                ),
                _model_check(settings),
            ]
        )
    return checks


def _token_present(raw: dict[str, str | None]) -> bool:
    token = (raw.get("TELEGRAM_BOT_TOKEN") or "").strip()
    return bool(token and token != "replace-me")


def _agent_command_check(command: str) -> Check:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return Check("agent command", False, f"invalid: {exc}")
    if not parts:
        return Check("agent command", False, "empty")
    executable = parts[0]
    if Path(executable).exists():
        return Check("agent command", True, executable)
    found = shutil.which(executable)
    return Check("agent command", found is not None, found or f"not on PATH: {executable}")


def _model_check(settings: Settings) -> Check:
    status = model_cache_status(settings)
    ok = status.status in {"present", "managed by faster-whisper cache (not inspected)"}
    detail = status.status
    if status.cache_dir is not None:
        detail = f"{detail}: {status.cache_dir}"
    return Check("model cache", ok, detail)

