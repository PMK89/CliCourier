from __future__ import annotations

import os
import shutil
import subprocess
import sys
from getpass import getpass
from pathlib import Path

from cli_courier.local_config import (
    default_config_path,
    default_data_dir,
    default_mute_file,
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
    suffix = f" [{default}]" if default else ""
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
    if config_path.exists() and not force:
        if not interactive or not yes_no(f"{config_path} exists. Overwrite it", default=False):
            raise FileExistsError(f"config already exists: {config_path}")

    if not interactive:
        values = default_config_values()
        write_env_file(config_path, values)
        return config_path

    print("CliCourier init")
    print(f"Config file: {config_path.expanduser()}")
    token = prompt("Telegram bot token", secret=True)
    user_ids = prompt("Allowed Telegram user id(s), comma-separated")
    default_chat = prompt("Default private chat id", default=user_ids.split(",", 1)[0].strip())
    workspace = prompt("Workspace root", default=str(Path.cwd()))
    agent_command = prompt("CLI tool command", default="codex")
    adapter = prompt("Agent adapter", default=infer_adapter(agent_command))
    auto_start = "true" if yes_no("Start the CLI tool automatically when the daemon starts") else "false"
    mute_file = prompt("Mute/block file", default=str(default_mute_file()))

    backend = prompt("Whisper backend (local/none/openai/whisper_cpp)", default="local")
    values = {
        "TELEGRAM_BOT_TOKEN": token,
        "ALLOWED_TELEGRAM_USER_IDS": user_ids,
        "DEFAULT_TELEGRAM_CHAT_ID": default_chat,
        "WORKSPACE_ROOT": str(Path(workspace).expanduser().resolve()),
        "DEFAULT_AGENT_COMMAND": agent_command,
        "DEFAULT_AGENT_ADAPTER": adapter,
        "AUTO_START_AGENT": auto_start,
        "AGENT_OUTPUT_MODE": "final",
        "SUPPRESS_AGENT_TRACE_LINES": "true",
        "NOTIFICATION_BLOCK_FILE": str(Path(mute_file).expanduser()),
        "WHISPER_BACKEND": backend,
        "WHISPER_MODEL": prompt("Local Whisper model", default="small"),
        "WHISPER_COMPUTE_TYPE": "int8",
        "WHISPER_DEVICE": "cpu",
        "WHISPER_MODEL_DIR": "",
        "FFMPEG_BINARY": "ffmpeg",
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
        values["TRANSCRIPTION_OPENAI_API_KEY"] = prompt("OpenAI API key", secret=True)
        values["OPENAI_TRANSCRIPTION_MODEL"] = prompt(
            "OpenAI transcription model",
            default="gpt-4o-mini-transcribe",
        )

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
        "WORKSPACE_ROOT": str(Path.cwd().resolve()),
        "DEFAULT_AGENT_COMMAND": "codex",
        "DEFAULT_AGENT_ADAPTER": "codex",
        "AUTO_START_AGENT": "false",
        "AGENT_OUTPUT_MODE": "final",
        "SUPPRESS_AGENT_TRACE_LINES": "true",
        "NOTIFICATION_BLOCK_FILE": str(default_mute_file()),
        "WHISPER_BACKEND": "local",
        "WHISPER_MODEL": "small",
        "WHISPER_COMPUTE_TYPE": "int8",
        "WHISPER_DEVICE": "cpu",
        "WHISPER_MODEL_DIR": "",
        "FFMPEG_BINARY": "ffmpeg",
    }


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
