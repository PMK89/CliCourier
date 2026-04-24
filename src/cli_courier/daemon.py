from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from cli_courier.local_config import default_log_path, default_pid_path, ensure_private_parent


@dataclass(frozen=True)
class DaemonStatus:
    running: bool
    pid: int | None
    pid_path: Path
    log_path: Path


def read_pid(pid_path: Path = default_pid_path()) -> int | None:
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_process_running(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def daemon_status(
    *,
    pid_path: Path = default_pid_path(),
    log_path: Path = default_log_path(),
) -> DaemonStatus:
    pid = read_pid(pid_path)
    return DaemonStatus(
        running=is_process_running(pid),
        pid=pid,
        pid_path=pid_path,
        log_path=log_path,
    )


def start_daemon(
    *,
    config_path: Path | None = None,
    agent_command: list[str] | None = None,
    pid_path: Path = default_pid_path(),
    log_path: Path = default_log_path(),
) -> DaemonStatus:
    status = daemon_status(pid_path=pid_path, log_path=log_path)
    if status.running:
        return status

    ensure_private_parent(pid_path)
    ensure_private_parent(log_path)
    env = os.environ.copy()
    env["AUTO_START_AGENT"] = "true"
    src_path = Path(__file__).resolve().parents[1]
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{src_path}{os.pathsep}{current_pythonpath}" if current_pythonpath else str(src_path)
    )
    if config_path is not None:
        env["CLICOURIER_CONFIG"] = str(config_path.expanduser())
    if agent_command:
        import shlex

        env["DEFAULT_AGENT_COMMAND"] = shlex.join(agent_command)
        env["DEFAULT_AGENT_ADAPTER"] = (
            "codex" if Path(agent_command[0]).name == "codex" else "generic"
        )

    command = [sys.executable, "-m", "cli_courier.cli", "run"]
    if config_path is not None:
        command.extend(["--config", str(config_path.expanduser())])

    with log_path.open("ab", buffering=0) as log_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(Path.cwd()),
            start_new_session=True,
        )
    pid_path.write_text(str(process.pid), encoding="utf-8")
    try:
        pid_path.chmod(0o600)
    except PermissionError:
        pass
    return daemon_status(pid_path=pid_path, log_path=log_path)


def stop_daemon(
    *,
    pid_path: Path = default_pid_path(),
    log_path: Path = default_log_path(),
    timeout_seconds: float = 8.0,
) -> DaemonStatus:
    pid = read_pid(pid_path)
    if not is_process_running(pid):
        pid_path.unlink(missing_ok=True)
        return daemon_status(pid_path=pid_path, log_path=log_path)

    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_process_running(pid):
            pid_path.unlink(missing_ok=True)
            return daemon_status(pid_path=pid_path, log_path=log_path)
        time.sleep(0.1)
    return daemon_status(pid_path=pid_path, log_path=log_path)
