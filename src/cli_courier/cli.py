from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from cli_courier.app import main as run_app
from cli_courier.config import load_settings
from cli_courier.daemon import daemon_status, start_daemon, stop_daemon
from cli_courier.doctor import run_doctor
from cli_courier.local_config import default_config_path, default_log_path, default_mute_file
from cli_courier.model_manager import download_model, format_model_list
from cli_courier.setup import init_config, run_setup, setup_whisper_cpp


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "status"
    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else None

    if command == "setup":
        try:
            run_setup(config_path or default_config_path())
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            print("Use `clicourier init --force` to overwrite.", file=sys.stderr)
            return 1
        return 0
    if command == "init":
        try:
            path = init_config(
                config_path or default_config_path(),
                force=args.force,
                interactive=not args.template,
                install_launcher=False,
            )
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            print("Use `clicourier init --force` to overwrite.", file=sys.stderr)
            return 1
        print(f"config: {path}")
        return 0
    if command == "doctor":
        return run_doctor(config_path)
    if command == "config":
        return print_config(config_path)
    if command == "model":
        settings = load_settings(config_path)
        if args.model_command == "download":
            download_model(settings, model_name=args.name)
            print(f"model ready: {args.name or settings.whisper_model}")
            return 0
        if args.model_command == "list":
            print(format_model_list(settings))
            return 0
        print("Use `clicourier model download` or `clicourier model list`.", file=sys.stderr)
        return 2
    if command == "setup-whisper":
        setup_whisper_cpp(config_path or default_config_path())
        return 0
    if command == "run":
        agent = normalize_remainder(args.agent)
        if agent:
            os.environ["DEFAULT_AGENT_COMMAND"] = shlex.join(agent)
            os.environ["AUTO_START_AGENT"] = "true"
        run_app(config_path=config_path)
        return 0
    if command == "start":
        status = start_daemon(config_path=config_path, agent_command=normalize_remainder(args.agent) or None)
        if status.running:
            print(f"clicourier running with pid {status.pid}")
            print(f"log: {status.log_path}")
            return 0
        print("failed to start clicourier", file=sys.stderr)
        return 1
    if command == "stop":
        status = stop_daemon()
        print("clicourier stopped" if not status.running else f"clicourier still running: {status.pid}")
        return 0 if not status.running else 1
    if command == "restart":
        stop_daemon()
        status = start_daemon(config_path=config_path, agent_command=normalize_remainder(args.agent) or None)
        print(f"clicourier running with pid {status.pid}" if status.running else "failed to start")
        return 0 if status.running else 1
    if command == "status":
        status = daemon_status()
        mute_file = configured_mute_file(config_path)
        print(f"running: {'yes' if status.running else 'no'}")
        print(f"pid: {status.pid or '-'}")
        print(f"log: {status.log_path}")
        print(f"muted: {'yes' if mute_file.exists() else 'no'}")
        print(f"config: {config_path or default_config_path()}")
        return 0
    if command == "mute":
        mute_file = Path(args.file).expanduser() if args.file else configured_mute_file(config_path)
        mute_file.parent.mkdir(parents=True, exist_ok=True)
        mute_file.write_text("muted\n", encoding="utf-8")
        print(f"muted via {mute_file}")
        return 0
    if command == "unmute":
        mute_file = Path(args.file).expanduser() if args.file else configured_mute_file(config_path)
        mute_file.unlink(missing_ok=True)
        print(f"unmuted via {mute_file}")
        return 0
    if command == "toggle":
        mute_file = Path(args.file).expanduser() if args.file else configured_mute_file(config_path)
        if mute_file.exists():
            mute_file.unlink()
            print(f"unmuted via {mute_file}")
        else:
            mute_file.parent.mkdir(parents=True, exist_ok=True)
            mute_file.write_text("muted\n", encoding="utf-8")
            print(f"muted via {mute_file}")
        return 0
    if command == "logs":
        log_path = Path(args.log).expanduser() if args.log else default_log_path()
        if not log_path.exists():
            print(f"no log file: {log_path}", file=sys.stderr)
            return 1
        print(log_path.read_text(encoding="utf-8", errors="replace")[-args.chars :])
        return 0

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clicourier")
    parser.add_argument("--config", help="Path to config.env")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Create local config in the user config directory")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing config")
    init_parser.add_argument(
        "--template",
        action="store_true",
        help="Write placeholders without interactive prompts",
    )
    subparsers.add_parser("setup", help="Prompt for config and write local config.env")
    subparsers.add_parser("setup-whisper", help="Clone/build whisper.cpp and configure local voice")
    subparsers.add_parser("doctor", help="Check local configuration and dependencies")
    subparsers.add_parser("config", help="Print config location and non-secret summary")

    model_parser = subparsers.add_parser("model", help="Manage local Whisper models")
    model_subparsers = model_parser.add_subparsers(dest="model_command")
    model_download = model_subparsers.add_parser("download", help="Download/load configured model")
    model_download.add_argument("--name", help="Override WHISPER_MODEL for this download")
    model_subparsers.add_parser("list", help="List configured and known local models")

    run_parser = subparsers.add_parser("run", help="Run the bridge in the foreground")
    run_parser.add_argument("agent", nargs=argparse.REMAINDER, help="Optional CLI command to auto-start")

    start_parser = subparsers.add_parser("start", help="Start the bridge in the background")
    start_parser.add_argument("agent", nargs=argparse.REMAINDER, help="Optional CLI command to auto-start")

    restart_parser = subparsers.add_parser("restart", help="Restart the background bridge")
    restart_parser.add_argument("agent", nargs=argparse.REMAINDER, help="Optional CLI command to auto-start")

    subparsers.add_parser("stop", help="Stop the background bridge")
    subparsers.add_parser("status", help="Show background bridge status")

    mute_parser = subparsers.add_parser("mute", help="Suppress proactive Telegram output")
    mute_parser.add_argument("--file", help="Override mute block file")
    unmute_parser = subparsers.add_parser("unmute", help="Resume proactive Telegram output")
    unmute_parser.add_argument("--file", help="Override mute block file")
    toggle_parser = subparsers.add_parser("toggle", help="Toggle the mute block file")
    toggle_parser.add_argument("--file", help="Override mute block file")

    logs_parser = subparsers.add_parser("logs", help="Print the daemon log tail")
    logs_parser.add_argument("--log", help="Override daemon log file")
    logs_parser.add_argument("--chars", type=int, default=8000)

    return parser


def app() -> int:
    return main()


def normalize_remainder(values: list[str]) -> list[str]:
    if values and values[0] == "--":
        return values[1:]
    return values


def configured_mute_file(config_path: Path | None) -> Path:
    try:
        return load_settings(config_path).notification_block_file.expanduser()
    except Exception:  # noqa: BLE001 - status/mute should still work before setup is complete
        return default_mute_file()


def print_config(config_path: Path | None) -> int:
    path = config_path or default_config_path()
    print(f"path: {path}")
    print(f"exists: {'yes' if path.exists() else 'no'}")
    try:
        settings = load_settings(config_path)
    except Exception as exc:  # noqa: BLE001 - config command should explain bad config
        print(f"valid: no ({exc})")
        return 1
    print("valid: yes")
    print("telegram_token: present" if settings.telegram_bot_token.get_secret_value() else "telegram_token: missing")
    print("allowed_users: " + ",".join(str(user_id) for user_id in settings.allowed_telegram_user_ids))
    print(f"workspace_root: {settings.workspace_root}")
    print(f"default_agent_command: {settings.default_agent_command}")
    print(f"whisper_backend: {settings.whisper_backend.value}")
    print(f"whisper_model: {settings.whisper_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
