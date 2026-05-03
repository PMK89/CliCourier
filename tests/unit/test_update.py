from __future__ import annotations

from cli_courier import update


def test_update_reinstalls_uv_tool_when_no_checkout(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(update, "installed_version", lambda: "0.1.0")
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(update.subprocess, "run", fake_run)

    result = update.run_tool_update()

    assert result.success is True
    assert result.changed is True
    assert calls == [
        [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            "--upgrade",
            "--reinstall-package",
            "cli-courier",
            "git+https://github.com/PMK89/CliCourier.git",
        ]
    ]
