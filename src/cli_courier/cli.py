from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from cli_courier.app import main as run_app
from cli_courier.config import load_settings
from cli_courier.daemon import daemon_status, start_daemon, stop_daemon
from cli_courier.local_config import default_config_path, default_log_path, default_mute_file
from cli_courier.setup import run_setup, setup_whisper_cpp


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "status"
    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else None

    if command == "setup":
        run_setup(config_path or default_config_path())
        return 0
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

    subparsers.add_parser("setup", help="Prompt for config and write local config.env")
    subparsers.add_parser("setup-whisper", help="Clone/build whisper.cpp and configure local voice")

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


def normalize_remainder(values: list[str]) -> list[str]:
    if values and values[0] == "--":
        return values[1:]
    return values


def configured_mute_file(config_path: Path | None) -> Path:
    try:
        return load_settings(config_path).notification_block_file.expanduser()
    except Exception:  # noqa: BLE001 - status/mute should still work before setup is complete
        return default_mute_file()


if __name__ == "__main__":
    raise SystemExit(main())
