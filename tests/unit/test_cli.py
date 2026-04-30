from __future__ import annotations

from pathlib import Path

import pytest

from cli_courier.daemon import DaemonStatus
import cli_courier.cli as cli_courier_cli
import clicourier.cli
from cli_courier.cli import normalize_remainder
from cli_courier.cli import normalize_run_mode, run_with_mode_prompt, set_mute_file
from cli_courier.cli import desktop_terminal_env, launch_tmux_terminal, terminal_attach_command
from cli_courier.cli import wait_for_tmux_session
from cli_courier.doctor import collect_checks
from cli_courier.local_config import default_state_dir
from cli_courier.setup import (
    default_mute_prompt_value,
    default_workspace_prompt_value,
    infer_adapter,
    init_config,
)


def test_normalize_remainder_strips_double_dash() -> None:
    assert normalize_remainder(["--", "codex", "--model", "x"]) == ["codex", "--model", "x"]


def test_normalize_run_mode_maps_local_to_desktop() -> None:
    assert normalize_run_mode("local") == "desktop"
    assert normalize_run_mode("telegram") == "telegram"


def test_set_mute_file_toggles_file(tmp_path: Path) -> None:
    path = tmp_path / "muted"
    set_mute_file(path, muted=True)
    assert path.exists()

    set_mute_file(path, muted=False)
    assert not path.exists()


def test_telegram_run_mode_attaches_visible_tmux_agent(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class FakeSettings:
        notification_block_file = tmp_path / "muted"
        agent_tmux_session = "clicourier-test"
        default_agent_command = "codex"

    monkeypatch.setattr("cli_courier.cli.load_settings", lambda _config_path: FakeSettings())
    monkeypatch.setattr("cli_courier.cli.wait_for_tmux_session", lambda _session: True)

    def fake_start_daemon(**kwargs):
        calls["start"] = kwargs
        return DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log")

    def fake_run(command, *, check=False):
        calls["run"] = command

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("cli_courier.cli.start_daemon", fake_start_daemon)
    monkeypatch.setattr("cli_courier.cli.subprocess.run", fake_run)

    result = run_with_mode_prompt(
        config_path=None,
        agent_command=["codex"],
        mode="telegram",
    )

    assert result == 0
    assert calls["start"]["auto_start_agent"] is True
    assert calls["start"]["extra_env"]["AGENT_TERMINAL_BACKEND"] == "tmux"
    assert calls["run"] == ["tmux", "attach", "-t", "clicourier-test"]
    assert not FakeSettings.notification_block_file.exists()


def test_telegram_run_mode_without_agent_starts_bridge_only(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class FakeSettings:
        notification_block_file = tmp_path / "muted"
        agent_tmux_session = "clicourier-test"
        default_agent_command = ""

    monkeypatch.setattr("cli_courier.cli.load_settings", lambda _config_path: FakeSettings())
    monkeypatch.setattr("cli_courier.cli.wait_for_tmux_session", lambda _session: False)
    monkeypatch.setattr("cli_courier.cli.subprocess.run", lambda *args, **kwargs: None)

    def fake_start_daemon(**kwargs):
        calls["start"] = kwargs
        return DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log")

    monkeypatch.setattr("cli_courier.cli.start_daemon", fake_start_daemon)

    result = run_with_mode_prompt(
        config_path=None,
        agent_command=[],
        mode="telegram",
    )

    assert result == 0
    assert calls["start"]["auto_start_agent"] is False
    assert not FakeSettings.notification_block_file.exists()


def test_telegram_run_mode_with_default_agent_attaches_visible_tmux_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}

    class FakeSettings:
        notification_block_file = tmp_path / "muted"
        agent_tmux_session = "clicourier-test"
        default_agent_command = "codex"

    monkeypatch.setattr("cli_courier.cli.load_settings", lambda _config_path: FakeSettings())
    monkeypatch.setattr("cli_courier.cli.wait_for_tmux_session", lambda _session: True)

    def fake_start_daemon(**kwargs):
        calls["start"] = kwargs
        return DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log")

    def fake_run(command, *, check=False):
        calls["run"] = command

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("cli_courier.cli.start_daemon", fake_start_daemon)
    monkeypatch.setattr("cli_courier.cli.subprocess.run", fake_run)

    result = run_with_mode_prompt(
        config_path=None,
        agent_command=[],
        mode="telegram",
    )

    assert result == 0
    assert calls["start"]["auto_start_agent"] is True
    assert calls["run"] == ["tmux", "attach", "-t", "clicourier-test"]


def test_start_can_resume_last_codex_session(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    def fake_start_daemon(**kwargs):
        calls["start"] = kwargs
        return DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log")

    monkeypatch.setattr("cli_courier.cli.start_daemon", fake_start_daemon)

    result = cli_courier_cli.main(["start", "--resume"])

    assert result == 0
    assert calls["start"]["resume_agent"] is True


def test_restart_resumes_last_codex_session_by_default(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class FakeSettings:
        agent_tmux_session = "clicourier-test"
        default_agent_command = "codex"

    def fake_stop_daemon():
        calls["stop"] = True
        return DaemonStatus(False, None, tmp_path / "pid", tmp_path / "log")

    def fake_start_daemon(**kwargs):
        calls["start"] = kwargs
        return DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log")

    monkeypatch.setattr("cli_courier.cli.stop_daemon", fake_stop_daemon)
    monkeypatch.setattr("cli_courier.cli.start_daemon", fake_start_daemon)
    monkeypatch.setattr("cli_courier.cli.load_settings", lambda _config_path: FakeSettings())

    result = cli_courier_cli.main(["restart"])

    assert result == 0
    assert calls["stop"] is True
    assert calls["start"]["resume_agent"] is True
    assert calls["start"]["auto_start_agent"] is True
    assert calls["start"]["extra_env"]["AGENT_TERMINAL_BACKEND"] == "tmux"
    assert calls["start"]["extra_env"]["AGENT_TMUX_SESSION"] == "clicourier-test"


def test_restart_can_disable_resume(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class FakeSettings:
        agent_tmux_session = "clicourier-test"
        default_agent_command = "codex"

    monkeypatch.setattr(
        "cli_courier.cli.stop_daemon",
        lambda: DaemonStatus(False, None, tmp_path / "pid", tmp_path / "log"),
    )

    def fake_start_daemon(**kwargs):
        calls["start"] = kwargs
        return DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log")

    monkeypatch.setattr("cli_courier.cli.start_daemon", fake_start_daemon)
    monkeypatch.setattr("cli_courier.cli.load_settings", lambda _config_path: FakeSettings())

    result = cli_courier_cli.main(["restart", "--no-resume"])

    assert result == 0
    assert calls["start"]["resume_agent"] is False
    assert calls["start"]["extra_env"]["AGENT_TERMINAL_BACKEND"] == "tmux"


def test_restart_attaches_visible_tmux_agent_when_interactive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}

    class FakeSettings:
        agent_tmux_session = "clicourier-test"
        default_agent_command = "codex"

    monkeypatch.setattr(
        "cli_courier.cli.stop_daemon",
        lambda: DaemonStatus(False, None, tmp_path / "pid", tmp_path / "log"),
    )
    monkeypatch.setattr(
        "cli_courier.cli.start_daemon",
        lambda **kwargs: DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log"),
    )
    monkeypatch.setattr("cli_courier.cli.load_settings", lambda _config_path: FakeSettings())
    monkeypatch.setattr("cli_courier.cli.wait_for_tmux_session", lambda _session: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def fake_run(command, *, check=False):
        calls["run"] = command

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("cli_courier.cli.subprocess.run", fake_run)

    result = cli_courier_cli.main(["restart"])

    assert result == 0
    assert calls["run"] == ["tmux", "attach", "-t", "clicourier-test"]


def test_restart_detach_skips_attach_but_starts_tmux(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class FakeSettings:
        agent_tmux_session = "clicourier-test"
        default_agent_command = "codex"

    monkeypatch.setattr(
        "cli_courier.cli.stop_daemon",
        lambda: DaemonStatus(False, None, tmp_path / "pid", tmp_path / "log"),
    )

    def fake_start_daemon(**kwargs):
        calls["start"] = kwargs
        return DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log")

    monkeypatch.setattr("cli_courier.cli.start_daemon", fake_start_daemon)
    monkeypatch.setattr("cli_courier.cli.load_settings", lambda _config_path: FakeSettings())
    monkeypatch.setattr("cli_courier.cli.wait_for_tmux_session", lambda _session: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    result = cli_courier_cli.main(["restart", "--detach"])

    assert result == 0
    assert calls["start"]["extra_env"]["AGENT_TERMINAL_BACKEND"] == "tmux"


def test_restart_open_terminal_launches_desktop_terminal(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class FakeSettings:
        agent_tmux_session = "clicourier-test"
        default_agent_command = "codex"

    monkeypatch.setattr(
        "cli_courier.cli.stop_daemon",
        lambda: DaemonStatus(False, None, tmp_path / "pid", tmp_path / "log"),
    )
    monkeypatch.setattr(
        "cli_courier.cli.start_daemon",
        lambda **kwargs: DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log"),
    )
    monkeypatch.setattr("cli_courier.cli.load_settings", lambda _config_path: FakeSettings())
    monkeypatch.setattr("cli_courier.cli.wait_for_tmux_session", lambda _session: True)
    monkeypatch.setattr("cli_courier.cli.terminal_attach_commands", lambda _session: [["terminal"]])
    monkeypatch.setenv("DISPLAY", ":0")

    class FakePopen:
        def __init__(self, command, **kwargs) -> None:
            calls["command"] = command
            calls["kwargs"] = kwargs

        def poll(self):
            return None

    monkeypatch.setattr("cli_courier.cli.subprocess.Popen", FakePopen)

    result = cli_courier_cli.main(["restart", "--detach", "--open-terminal"])

    assert result == 0
    assert calls["command"] == ["terminal"]
    assert calls["kwargs"]["start_new_session"] is True
    assert calls["kwargs"]["env"]["DISPLAY"] == ":0"


def test_restart_open_terminal_infers_desktop_env(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class FakeSettings:
        agent_tmux_session = "clicourier-test"
        default_agent_command = "codex"

    monkeypatch.setattr(
        "cli_courier.cli.stop_daemon",
        lambda: DaemonStatus(False, None, tmp_path / "pid", tmp_path / "log"),
    )
    monkeypatch.setattr(
        "cli_courier.cli.start_daemon",
        lambda **kwargs: DaemonStatus(True, 123, tmp_path / "pid", tmp_path / "log"),
    )
    monkeypatch.setattr("cli_courier.cli.load_settings", lambda _config_path: FakeSettings())
    monkeypatch.setattr("cli_courier.cli.wait_for_tmux_session", lambda _session: True)
    monkeypatch.setattr("cli_courier.cli.terminal_attach_commands", lambda _session: [["terminal"]])
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("cli_courier.cli.desktop_terminal_env", lambda: {"DISPLAY": ":0"})

    class FakePopen:
        def __init__(self, command, **kwargs) -> None:
            calls["command"] = command
            calls["kwargs"] = kwargs

        def poll(self):
            return None

    monkeypatch.setattr("cli_courier.cli.subprocess.Popen", FakePopen)

    result = cli_courier_cli.main(["restart", "--detach", "--open-terminal"])

    assert result == 0
    assert calls["command"] == ["terminal"]
    assert calls["kwargs"]["env"]["DISPLAY"] == ":0"


def test_terminal_attach_command_uses_gnome_terminal(monkeypatch) -> None:
    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == "gnome-terminal" else None

    monkeypatch.setattr("cli_courier.cli.shutil.which", fake_which)

    assert terminal_attach_command("clicourier") == [
        "/usr/bin/gnome-terminal",
        "--",
        "tmux",
        "attach",
        "-t",
        "clicourier",
    ]


def test_desktop_terminal_env_uses_desktop_session_auth(monkeypatch) -> None:
    for key in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_CURRENT_DESKTOP",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "cli_courier.cli._desktop_process_env",
        lambda: {
            "DISPLAY": ":0",
            "XAUTHORITY": "/home/test/.Xauthority",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        },
    )

    env = desktop_terminal_env()

    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == "/home/test/.Xauthority"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"


def test_launch_tmux_terminal_falls_back_after_failed_terminal(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr("cli_courier.cli.desktop_terminal_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr("cli_courier.cli.wait_for_tmux_session", lambda _session: True)
    monkeypatch.setattr(
        "cli_courier.cli.terminal_attach_commands",
        lambda _session: [["bad-terminal"], ["xterm", "-e", "tmux", "attach", "-t", "clicourier"]],
    )
    monkeypatch.setattr("cli_courier.cli.time.sleep", lambda _seconds: None)

    class FakePopen:
        def __init__(self, command, **kwargs) -> None:
            calls.append(command)
            self._returncode = 1 if command == ["bad-terminal"] else None

        def poll(self):
            return self._returncode

    monkeypatch.setattr("cli_courier.cli.subprocess.Popen", FakePopen)

    assert launch_tmux_terminal("clicourier") is True
    assert calls == [
        ["bad-terminal"],
        ["xterm", "-e", "tmux", "attach", "-t", "clicourier"],
    ]


def test_wait_for_tmux_session_ignores_dead_pane_before_live_pane(monkeypatch) -> None:
    pane_states = ["1\n", "0\n"]
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["tmux", "list-panes", "-t"]:
            return type("Result", (), {"returncode": 0, "stdout": pane_states.pop(0)})()
        raise AssertionError(command)

    monkeypatch.setattr("cli_courier.cli.subprocess.run", fake_run)
    monkeypatch.setattr("cli_courier.cli.time.sleep", lambda _seconds: None)

    assert wait_for_tmux_session("clicourier", timeout_seconds=1) is True
    assert len(calls) == 2


def test_infer_adapter_uses_codex_only_for_codex_command() -> None:
    assert infer_adapter("codex --model x") == "codex"
    assert infer_adapter("claude") == "generic"
    assert infer_adapter("gemini") == "generic"


def test_clicourier_entrypoint_imports() -> None:
    assert callable(clicourier.cli.app)


def test_init_does_not_overwrite_existing_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.env"
    config_path.write_text("TELEGRAM_BOT_TOKEN=keep-me\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        init_config(config_path, interactive=False)

    assert config_path.read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=keep-me\n"


def test_init_template_writes_local_whisper_defaults(tmp_path: Path) -> None:
    config_path = init_config(tmp_path / "config.env", interactive=False)
    text = config_path.read_text(encoding="utf-8")

    assert 'WHISPER_BACKEND="local"' in text
    assert 'WHISPER_MODEL="small"' in text
    assert 'WORKSPACE_ROOT="."' in text
    assert 'NOTIFICATION_BLOCK_FILE="muted"' in text
    assert "replace-me" in text


def test_init_interactive_loads_existing_values_as_defaults(tmp_path: Path, monkeypatch) -> None:
    config_path = init_config(tmp_path / "config.env", interactive=False)
    answers = iter(
        [
            "",  # user ids default
            "",  # default chat id default
            "",  # workspace default
            "gemini",
            "",  # adapter default inferred from changed command
            "",  # auto-start default
            "",  # mute file default
            "",  # backend default
            "turbo",
            "",  # write updated config
            "n",  # launcher
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("cli_courier.setup.getpass", lambda _prompt: "")

    init_config(config_path, interactive=True)
    text = config_path.read_text(encoding="utf-8")

    assert 'TELEGRAM_BOT_TOKEN="replace-me"' in text
    assert 'DEFAULT_AGENT_COMMAND="gemini"' in text
    assert 'DEFAULT_AGENT_ADAPTER="generic"' in text
    assert 'WHISPER_MODEL="turbo"' in text


def test_legacy_global_mute_default_becomes_project_local() -> None:
    assert default_mute_prompt_value(
        {"NOTIFICATION_BLOCK_FILE": str(default_state_dir() / "muted")}
    ) == "muted"


def test_legacy_home_workspace_default_becomes_current_directory_marker() -> None:
    assert default_workspace_prompt_value({"WORKSPACE_ROOT": str(Path.home())}) == "."
    assert default_workspace_prompt_value({"WORKSPACE_ROOT": str(Path.home()) + "/"}) == "."


def test_custom_workspace_default_is_preserved(tmp_path: Path) -> None:
    assert default_workspace_prompt_value({"WORKSPACE_ROOT": str(tmp_path)}) == str(tmp_path)


def test_restart_fails_when_existing_daemon_does_not_stop(monkeypatch, capsys) -> None:
    monkeypatch.setattr("cli_courier.cli.stop_daemon", lambda: DaemonStatus(True, 321, Path("pid"), Path("log")))

    result = clicourier.cli.main(["restart"])

    captured = capsys.readouterr()
    assert result == 1
    assert "failed to stop existing clicourier process: 321" in captured.err


def test_doctor_checks_can_run_with_missing_dependencies(tmp_path: Path) -> None:
    config_path = init_config(tmp_path / "config.env", interactive=False)

    checks = collect_checks(config_path)

    assert any(check.name == "python" for check in checks)
    assert any(check.name == "telegram token" for check in checks)
