from __future__ import annotations

from pathlib import Path

import cli_courier.daemon as daemon
from cli_courier.daemon import DaemonStatus


def test_start_daemon_replaces_running_daemon_when_tmux_session_is_stale(tmp_path: Path, monkeypatch) -> None:
    calls: dict[str, object] = {}
    statuses = [
        DaemonStatus(True, 111, tmp_path / "pid", tmp_path / "log"),
        DaemonStatus(True, 222, tmp_path / "pid", tmp_path / "log"),
    ]

    class FakePopen:
        pid = 222

        def __init__(self, command, **kwargs) -> None:
            calls["popen"] = command
            calls["popen_kwargs"] = kwargs

    monkeypatch.setattr("cli_courier.daemon.daemon_status", lambda **_kwargs: statuses.pop(0))
    monkeypatch.setattr("cli_courier.daemon.tmux_session_has_running_agent", lambda _session: False)
    monkeypatch.setattr(
        "cli_courier.daemon.stop_daemon",
        lambda **_kwargs: DaemonStatus(False, None, tmp_path / "pid", tmp_path / "log"),
    )
    monkeypatch.setattr("cli_courier.daemon.subprocess.Popen", FakePopen)

    status = daemon.start_daemon(
        required_agent_tmux_session="clicourier",
        pid_path=tmp_path / "pid",
        log_path=tmp_path / "log",
    )

    assert status.pid == 222
    assert calls["popen"][-1] == "run"


def test_start_daemon_reuses_running_daemon_when_tmux_agent_is_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, bool] = {}
    existing = DaemonStatus(True, 111, tmp_path / "pid", tmp_path / "log")

    monkeypatch.setattr("cli_courier.daemon.daemon_status", lambda **_kwargs: existing)
    monkeypatch.setattr("cli_courier.daemon.tmux_session_has_running_agent", lambda _session: True)
    monkeypatch.setattr("cli_courier.daemon.stop_daemon", lambda **_kwargs: calls.setdefault("stop", True))
    monkeypatch.setattr(
        "cli_courier.daemon.subprocess.Popen",
        lambda *_args, **_kwargs: calls.setdefault("popen", True),
    )

    status = daemon.start_daemon(
        required_agent_tmux_session="clicourier",
        pid_path=tmp_path / "pid",
        log_path=tmp_path / "log",
    )

    assert status is existing
    assert calls == {}
