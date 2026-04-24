from __future__ import annotations

from pathlib import Path

import pytest

import clicourier.cli
from cli_courier.cli import normalize_remainder
from cli_courier.doctor import collect_checks
from cli_courier.setup import infer_adapter, init_config


def test_normalize_remainder_strips_double_dash() -> None:
    assert normalize_remainder(["--", "codex", "--model", "x"]) == ["codex", "--model", "x"]


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
    assert "replace-me" in text


def test_doctor_checks_can_run_with_missing_dependencies(tmp_path: Path) -> None:
    config_path = init_config(tmp_path / "config.env", interactive=False)

    checks = collect_checks(config_path)

    assert any(check.name == "python" for check in checks)
    assert any(check.name == "telegram token" for check in checks)
