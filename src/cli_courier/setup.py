from __future__ import annotations

import os
import shutil
import subprocess
import sys
from getpass import getpass
from pathlib import Path

from dotenv import dotenv_values

from cli_courier.local_config import (
    default_config_path,
    default_data_dir,
    default_mute_file,
    default_state_dir,
    default_whisper_dir,
    ensure_private_parent,
    write_env_file,
)


def prompt(
    label: str,
    *,
    default: str | None = None,
    secret: bool = False,
    required: bool = True,
) -> str:
    suffix = " [configured, press Enter to keep]" if secret and default else f" [{default}]" if default else ""
    while True:
        if secret:
            value = getpass(f"{label}{suffix}: ")
        else:
            value = input(f"{label}{suffix}: ")
        value = value.strip()
        if value:
            return value
        if default is not None:
            return default
        if not required:
            return ""
        print("This value is required.")


def yes_no(label: str, *, default: bool = True) -> bool:
    marker = "Y/n" if default else "y/N"
    value = input(f"{label} [{marker}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def infer_adapter(command: str) -> str:
    head = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    if Path(head).name == "codex":
        return "codex"
    return "generic"


def init_config(
    config_path: Path = default_config_path(),
    *,
    force: bool = False,
    interactive: bool = True,
    install_launcher: bool = False,
) -> Path:
    config_path = config_path.expanduser()
    existing = read_existing_config(config_path) if config_path.exists() else {}
    if config_path.exists() and not force and not interactive:
        raise FileExistsError(f"config already exists: {config_path}")

    if not interactive:
        values = default_config_values()
        write_env_file(config_path, values)
        return config_path

    print("CliCourier init")
    print(f"Config file: {config_path.expanduser()}")
    if existing:
        print("Existing config found; using current values as defaults.")
    token = prompt("Telegram bot token", default=existing_value(existing, "TELEGRAM_BOT_TOKEN"), secret=True)
    user_ids = prompt(
        "Allowed Telegram user id(s), comma-separated",
        default=existing_value(existing, "ALLOWED_TELEGRAM_USER_IDS"),
    )
    default_chat = prompt(
        "Default private chat id for proactive output",
        default=existing_value(existing, "DEFAULT_TELEGRAM_CHAT_ID", ""),
        required=False,
    )
    workspace = prompt("Workspace root", default=default_workspace_prompt_value(existing))
    agent_command = prompt(
        "CLI tool command",
        default=existing_value(existing, "DEFAULT_AGENT_COMMAND", "codex"),
    )
    previous_agent_command = existing_value(existing, "DEFAULT_AGENT_COMMAND")
    adapter_default = (
        existing_value(existing, "DEFAULT_AGENT_ADAPTER", infer_adapter(agent_command))
        if previous_agent_command == agent_command
        else infer_adapter(agent_command)
    )
    adapter = prompt(
        "Agent adapter",
        default=adapter_default,
    )
    auto_start = "true" if yes_no(
        "Start the CLI tool automatically when the daemon starts",
        default=existing_value(existing, "AUTO_START_AGENT", "false").lower() == "true",
    ) else "false"
    mute_file = prompt(
        "Mute/block file name or path",
        default=default_mute_prompt_value(existing),
    )

    backend = prompt(
        "Whisper backend (local/none/openai/whisper_cpp)",
        default=existing_value(existing, "WHISPER_BACKEND", "local"),
    )
    values = {
        "TELEGRAM_BOT_TOKEN": token,
        "ALLOWED_TELEGRAM_USER_IDS": user_ids,
        "DEFAULT_TELEGRAM_CHAT_ID": default_chat,
        "WORKSPACE_ROOT": workspace,
        "DEFAULT_AGENT_COMMAND": agent_command,
        "DEFAULT_AGENT_ADAPTER": adapter,
        "AUTO_START_AGENT": auto_start,
        "AGENT_OUTPUT_MODE": existing_value(existing, "AGENT_OUTPUT_MODE", "final"),
        "AGENT_TERMINAL_BACKEND": existing_value(existing, "AGENT_TERMINAL_BACKEND", "auto"),
        "AGENT_TMUX_SESSION": existing_value(existing, "AGENT_TMUX_SESSION", "clicourier"),
        "AGENT_TMUX_HISTORY_LINES": existing_value(existing, "AGENT_TMUX_HISTORY_LINES", "300"),
        "SUPPRESS_AGENT_TRACE_LINES": existing_value(existing, "SUPPRESS_AGENT_TRACE_LINES", "true"),
        "AGENT_INITIAL_PROMPT_ENABLED": existing_value(existing, "AGENT_INITIAL_PROMPT_ENABLED", "true"),
        "NOTIFICATION_BLOCK_FILE": mute_file,
        "WHISPER_BACKEND": backend,
        "WHISPER_MODEL": prompt(
            "Local Whisper model (base/small/turbo)",
            default=existing_value(existing, "WHISPER_MODEL", "small"),
        ),
        "WHISPER_COMPUTE_TYPE": existing_value(existing, "WHISPER_COMPUTE_TYPE", "int8"),
        "WHISPER_DEVICE": existing_value(existing, "WHISPER_DEVICE", "cpu"),
        "WHISPER_MODEL_DIR": existing_value(existing, "WHISPER_MODEL_DIR", ""),
        "FFMPEG_BINARY": existing_value(existing, "FFMPEG_BINARY", "ffmpeg"),
    }

    if backend == "whisper_cpp":
        whisper_dir = default_whisper_dir()
        values["WHISPER_CPP_BINARY"] = prompt(
            "whisper.cpp binary",
            default=str(whisper_dir / "main"),
            required=False,
        )
        values["WHISPER_CPP_MODEL"] = prompt(
            "Whisper ggml model path",
            default=str(whisper_dir / "models" / "ggml-turbo.bin"),
            required=False,
        )
        values["WHISPER_CPP_FFMPEG_BINARY"] = prompt("ffmpeg binary", default="ffmpeg")
    elif backend == "openai":
        values["TRANSCRIPTION_OPENAI_API_KEY"] = prompt(
            "OpenAI API key",
            default=existing_value(existing, "TRANSCRIPTION_OPENAI_API_KEY"),
            secret=True,
        )
        values["OPENAI_TRANSCRIPTION_MODEL"] = prompt(
            "OpenAI transcription model",
            default=existing_value(existing, "OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"),
        )

    if existing and not force and not yes_no("Write updated config", default=True):
        raise FileExistsError(f"config already exists: {config_path}")
    write_env_file(config_path, values)
    if backend == "whisper_cpp":
        binary = Path(values["WHISPER_CPP_BINARY"]).expanduser()
        model = Path(values["WHISPER_CPP_MODEL"]).expanduser()
        if not binary.exists() or not model.exists():
            print("Local Whisper paths do not exist yet. Run `clicourier setup-whisper` next.")
    if install_launcher or yes_no("Install/update ~/.local/bin/clicourier launcher", default=False):
        install_user_launcher(config_path)
    print(f"Wrote {config_path}")
    return config_path


def run_setup(config_path: Path = default_config_path()) -> Path:
    return init_config(config_path, force=False, interactive=True)


def default_config_values() -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "replace-me",
        "ALLOWED_TELEGRAM_USER_IDS": "123456789",
        "DEFAULT_TELEGRAM_CHAT_ID": "",
        "WORKSPACE_ROOT": ".",
        "DEFAULT_AGENT_COMMAND": "codex",
        "DEFAULT_AGENT_ADAPTER": "codex",
        "AUTO_START_AGENT": "false",
        "AGENT_OUTPUT_MODE": "final",
        "AGENT_TERMINAL_BACKEND": "auto",
        "AGENT_TMUX_SESSION": "clicourier",
        "AGENT_TMUX_HISTORY_LINES": "300",
        "SUPPRESS_AGENT_TRACE_LINES": "true",
        "AGENT_INITIAL_PROMPT_ENABLED": "true",
        "NOTIFICATION_BLOCK_FILE": str(default_mute_file()),
        "WHISPER_BACKEND": "local",
        "WHISPER_MODEL": "small",
        "WHISPER_COMPUTE_TYPE": "int8",
        "WHISPER_DEVICE": "cpu",
        "WHISPER_MODEL_DIR": "",
        "FFMPEG_BINARY": "ffmpeg",
    }


def read_existing_config(config_path: Path) -> dict[str, str]:
    values = dotenv_values(config_path)
    return {key: value for key, value in values.items() if value is not None}


def existing_value(values: dict[str, str], key: str, default: str | None = None) -> str | None:
    value = values.get(key)
    if value is None:
        return default
    return value


def default_mute_prompt_value(values: dict[str, str]) -> str:
    current = existing_value(values, "NOTIFICATION_BLOCK_FILE")
    legacy_global = str(default_state_dir() / "muted")
    if current and current != legacy_global:
        return current
    return str(default_mute_file())


def default_workspace_prompt_value(values: dict[str, str]) -> str:
    current = existing_value(values, "WORKSPACE_ROOT")
    if not current:
        return "."

    current = current.strip()
    if current == ".":
        return "."

    try:
        if Path(current).expanduser().resolve(strict=False) == Path.home().resolve(strict=False):
            return "."
    except OSError:
        pass

    return current


def install_user_launcher(config_path: Path) -> Path:
    bin_dir = Path.home() / ".local" / "bin"
    launcher = bin_dir / "clicourier"
    bin_dir.mkdir(parents=True, exist_ok=True)
    repo_src = Path(__file__).resolve().parents[1]
    script = f"""#!/bin/sh
export CLICOURIER_CONFIG="{config_path}"
export PYTHONPATH="{repo_src}${{PYTHONPATH:+:$PYTHONPATH}}"
exec "{sys.executable}" -m cli_courier.cli "$@"
"""
    launcher.write_text(script, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def setup_whisper_cpp(config_path: Path = default_config_path()) -> Path:
    target = Path(prompt("whisper.cpp install directory", default=str(default_whisper_dir()))).expanduser()
    model_name = prompt("Whisper model name", default="turbo")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        subprocess.run(
            ["git", "clone", "https://github.com/ggerganov/whisper.cpp.git", str(target)],
            check=True,
        )
    subprocess.run(["make", "-C", str(target)], check=True)
    subprocess.run(
        ["bash", str(target / "models" / "download-ggml-model.sh"), model_name],
        cwd=str(target),
        check=True,
    )
    binary = first_existing(
        [
            target / "main",
            target / "build" / "bin" / "whisper-cli",
            target / "build" / "bin" / "main",
        ]
    )
    model = target / "models" / f"ggml-{model_name}.bin"
    if model_name == "turbo" and not model.exists():
        model = target / "models" / "ggml-large-v3-turbo.bin"
    ensure_private_parent(config_path)
    with config_path.expanduser().open("a", encoding="utf-8") as file_obj:
        file_obj.write("\n")
        file_obj.write('WHISPER_BACKEND="whisper_cpp"\n')
        file_obj.write(f'WHISPER_CPP_BINARY="{binary}"\n')
        file_obj.write(f'WHISPER_CPP_MODEL="{model}"\n')
        file_obj.write('WHISPER_CPP_FFMPEG_BINARY="ffmpeg"\n')
    return target


def first_existing(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def ensure_dirs() -> None:
    default_data_dir().mkdir(parents=True, exist_ok=True)
    shutil.which("ffmpeg")
