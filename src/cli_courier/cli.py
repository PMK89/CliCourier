from __future__ import annotations

import argparse
import os
import subprocess
import shlex
import shutil
import sys
import time
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
        if should_offer_run_mode(args):
            return run_with_mode_prompt(config_path=config_path, agent_command=agent, mode=args.mode)
        run_app(config_path=config_path)
        return 0
    if command == "start":
        status = start_daemon(
            config_path=config_path,
            agent_command=normalize_remainder(args.agent) or None,
            resume_agent=args.resume,
        )
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
        agent = normalize_remainder(args.agent)
        restart_plan = restart_agent_terminal_plan(
            config_path=config_path,
            agent_command=agent,
            detach=args.detach,
        )
        stopped = stop_daemon()
        if stopped.running:
            print(f"failed to stop existing clicourier process: {stopped.pid}", file=sys.stderr)
            return 1
        status = start_daemon(
            config_path=config_path,
            agent_command=agent or None,
            extra_env=restart_plan.extra_env,
            auto_start_agent=restart_plan.should_start_agent,
            resume_agent=not args.no_resume,
        )
        if not status.running:
            print("failed to start")
            return 1
        print(f"clicourier running with pid {status.pid}")
        print(f"log: {status.log_path}")
        if restart_plan.should_attach and restart_plan.tmux_session:
            return attach_tmux_session(restart_plan.tmux_session)
        if args.open_terminal and restart_plan.tmux_session:
            if launch_tmux_terminal(restart_plan.tmux_session):
                print(f"opened agent terminal: tmux attach -t {restart_plan.tmux_session}")
            else:
                print(
                    f"could not open a local terminal; attach manually with: "
                    f"tmux attach -t {restart_plan.tmux_session}",
                    file=sys.stderr,
                )
            return 0
        if restart_plan.tmux_session:
            print(f"agent terminal: tmux attach -t {restart_plan.tmux_session}")
        return 0
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
    run_parser.add_argument(
        "--mode",
        choices=("ask", "desktop", "local", "telegram", "foreground"),
        default="ask",
        help=(
            "desktop/local attaches tmux with Telegram muted; telegram attaches tmux "
            "unmuted, or starts bridge-only forwarding when no agent command is given"
        ),
    )
    run_parser.add_argument("agent", nargs=argparse.REMAINDER, help="Optional CLI command to auto-start")

    start_parser = subparsers.add_parser("start", help="Start the bridge in the background")
    start_parser.add_argument("--resume", action="store_true", help="Resume the last Codex session")
    start_parser.add_argument("agent", nargs=argparse.REMAINDER, help="Optional CLI command to auto-start")

    restart_parser = subparsers.add_parser("restart", help="Restart the background bridge")
    restart_parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start a fresh agent session instead of resuming the last Codex session",
    )
    restart_parser.add_argument(
        "--detach",
        action="store_true",
        help="Restart without attaching to the agent tmux terminal",
    )
    restart_parser.add_argument(
        "--open-terminal",
        action="store_true",
        help="Open a new desktop terminal attached to the agent tmux session",
    )
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


def should_offer_run_mode(args) -> bool:
    if os.environ.get("CLICOURIER_DAEMON_CHILD") == "1":
        return False
    if args.mode == "foreground":
        return False
    if args.mode != "ask":
        return True
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_with_mode_prompt(
    *,
    config_path: Path | None,
    agent_command: list[str],
    mode: str,
) -> int:
    settings = load_settings(config_path)
    selected = normalize_run_mode(mode)
    if selected == "ask":
        selected = prompt_run_mode()

    mute_file = settings.notification_block_file.expanduser()
    if selected in {"desktop", "local"}:
        set_mute_file(mute_file, muted=True)
        print(f"desktop mode: Telegram proactive output muted via {mute_file}")
    elif selected == "telegram":
        set_mute_file(mute_file, muted=False)
        print("telegram mode: proactive Telegram output enabled")
    else:
        run_app(config_path=config_path)
        return 0

    extra_env = {
        "AGENT_TERMINAL_BACKEND": "tmux",
        "AGENT_TMUX_SESSION": settings.agent_tmux_session or "clicourier",
    }
    default_agent_command = getattr(settings, "default_agent_command", "").strip()
    should_start_agent = bool(agent_command) or bool(default_agent_command)
    should_attach_terminal = should_start_agent
    status = start_daemon(
        config_path=config_path,
        agent_command=agent_command or None,
        extra_env=extra_env,
        auto_start_agent=should_start_agent,
        resume_agent=False,
    )
    if not status.running:
        print("failed to start clicourier", file=sys.stderr)
        return 1
    print(f"clicourier bridge running with pid {status.pid}")
    print(f"log: {status.log_path}")

    if not should_start_agent:
        print("bridge is forwarding Telegram messages; no agent was started")
        print("use /start_agent from Telegram to start the configured agent")
        return 0

    if should_attach_terminal and selected in {"desktop", "local", "telegram"}:
        session_name = extra_env["AGENT_TMUX_SESSION"]
        return attach_tmux_session(session_name)
    return 0


class RestartTerminalPlan:
    def __init__(
        self,
        *,
        should_start_agent: bool,
        should_attach: bool,
        tmux_session: str | None,
        extra_env: dict[str, str] | None,
    ) -> None:
        self.should_start_agent = should_start_agent
        self.should_attach = should_attach
        self.tmux_session = tmux_session
        self.extra_env = extra_env


def restart_agent_terminal_plan(
    *,
    config_path: Path | None,
    agent_command: list[str],
    detach: bool,
) -> RestartTerminalPlan:
    settings = load_settings(config_path)
    default_agent_command = getattr(settings, "default_agent_command", "").strip()
    should_start_agent = bool(agent_command) or bool(default_agent_command)
    if not should_start_agent:
        return RestartTerminalPlan(
            should_start_agent=False,
            should_attach=False,
            tmux_session=None,
            extra_env=None,
        )
    session_name = settings.agent_tmux_session or "clicourier"
    return RestartTerminalPlan(
        should_start_agent=True,
        should_attach=not detach and sys.stdin.isatty() and sys.stdout.isatty(),
        tmux_session=session_name,
        extra_env={
            "AGENT_TERMINAL_BACKEND": "tmux",
            "AGENT_TMUX_SESSION": session_name,
        },
    )


def attach_tmux_session(session_name: str) -> int:
    if wait_for_tmux_session(session_name):
        print(f"attaching to agent terminal: tmux attach -t {session_name}")
        return subprocess.run(["tmux", "attach", "-t", session_name], check=False).returncode
    print(f"bridge started, but tmux session is not ready yet: {session_name}", file=sys.stderr)
    print(f"attach later with: tmux attach -t {session_name}")
    return 1


def launch_tmux_terminal(session_name: str) -> bool:
    env = desktop_terminal_env()
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return False
    if not wait_for_tmux_session(session_name):
        return False
    command = terminal_attach_command(session_name)
    if command is None:
        return False
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    return True


def desktop_terminal_env() -> dict[str, str]:
    env = os.environ.copy()
    uid = os.getuid()
    runtime_dir = Path(f"/run/user/{uid}")
    if "XDG_RUNTIME_DIR" not in env and runtime_dir.exists():
        env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    bus_path = runtime_dir / "bus"
    if "DBUS_SESSION_BUS_ADDRESS" not in env and bus_path.exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
    if "WAYLAND_DISPLAY" not in env:
        wayland_path = runtime_dir / "wayland-0"
        if wayland_path.exists():
            env["WAYLAND_DISPLAY"] = "wayland-0"
    if "DISPLAY" not in env and Path("/tmp/.X11-unix/X0").exists():
        env["DISPLAY"] = ":0"
    return env


def terminal_attach_command(session_name: str) -> list[str] | None:
    attach_args = ["tmux", "attach", "-t", session_name]
    for name in (
        "gnome-terminal",
        "kgx",
        "xfce4-terminal",
        "mate-terminal",
        "konsole",
        "xterm",
        "x-terminal-emulator",
    ):
        path = shutil.which(name)
        if path is None:
            continue
        executable = Path(path).name
        if executable in {"gnome-terminal", "kgx", "xfce4-terminal", "mate-terminal"}:
            return [path, "--", *attach_args]
        return [path, "-e", *attach_args]
    return None


def normalize_run_mode(mode: str) -> str:
    return "desktop" if mode == "local" else mode


def prompt_run_mode() -> str:
    while True:
        value = input("Run mode: desktop/local or telegram [desktop]: ").strip().lower()
        if not value:
            return "desktop"
        if value in {"desktop", "local", "telegram", "foreground"}:
            return normalize_run_mode(value)
        print("Choose desktop, local, telegram, or foreground.")


def set_mute_file(path: Path, *, muted: bool) -> None:
    if muted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("muted\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def wait_for_tmux_session(session_name: str, *, timeout_seconds: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return True
        time.sleep(0.2)
    return False


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
    print(f"agent_terminal_backend: {settings.agent_terminal_backend.value}")
    print(f"agent_resume_last: {settings.agent_resume_last}")
    print(f"agent_tmux_session: {settings.agent_tmux_session or 'clicourier'}")
    print(f"whisper_backend: {settings.whisper_backend.value}")
    print(f"whisper_model: {settings.whisper_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
