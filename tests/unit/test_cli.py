from __future__ import annotations

from pathlib import Path

import pytest

import clicourier.cli
from cli_courier.cli import normalize_remainder
from cli_courier.cli import normalize_run_mode, set_mute_file
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


def test_doctor_checks_can_run_with_missing_dependencies(tmp_path: Path) -> None:
    config_path = init_config(tmp_path / "config.env", interactive=False)

    checks = collect_checks(config_path)

    assert any(check.name == "python" for check in checks)
    assert any(check.name == "telegram token" for check in checks)
