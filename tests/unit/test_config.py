from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cli_courier.config import (
    AgentOutputMode,
    Settings,
    TranscriptionBackend,
    WhisperBackend,
    load_settings,
)
from cli_courier.local_config import write_env_file


def make_settings(root: Path, **overrides):
    values = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "ALLOWED_TELEGRAM_USER_IDS": "1001,1002",
        "WORKSPACE_ROOT": str(root),
        "DEFAULT_AGENT_COMMAND": "codex --ask-for-approval on-request",
        "SCREENSHOT_DIR": "",
        "TRANSCRIPTION_OPENAI_API_KEY": "",
        "ALLOW_SCREENSHOT_DIR_OUTSIDE_WORKSPACE": False,
        "DEFAULT_TELEGRAM_CHAT_ID": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_settings_parse_required_values(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, AGENT_ENV_ALLOWLIST="EDITOR,GIT_AUTHOR_NAME")

    assert settings.allowed_telegram_user_ids == (1001, 1002)
    assert settings.workspace_root == tmp_path.resolve()
    assert settings.agent_command_parts == ["codex", "--ask-for-approval", "on-request"]
    assert settings.agent_env_allowlist == ("EDITOR", "GIT_AUTHOR_NAME")
    assert settings.agent_output_mode == AgentOutputMode.FINAL
    assert settings.default_telegram_chat_id is None
    assert settings.whisper_backend == WhisperBackend.LOCAL
    assert settings.whisper_model == "small"
    assert settings.whisper_compute_type == "int8"
    assert settings.whisper_device == "cpu"


def test_settings_requires_existing_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path / "missing")


def test_openai_transcription_requires_api_key(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, TRANSCRIPTION_BACKEND=TranscriptionBackend.OPENAI)

    with pytest.raises(ValidationError):
        make_settings(tmp_path, WHISPER_BACKEND=WhisperBackend.OPENAI)


def test_screenshot_dir_must_stay_in_workspace_by_default(tmp_path: Path) -> None:
    outside = tmp_path.parent

    with pytest.raises(ValidationError):
        make_settings(tmp_path, SCREENSHOT_DIR=str(outside))

    settings = make_settings(
        tmp_path,
        SCREENSHOT_DIR=str(outside),
        ALLOW_SCREENSHOT_DIR_OUTSIDE_WORKSPACE=True,
    )
    assert settings.screenshot_dir == outside.resolve()


def test_whisper_cpp_requires_binary_and_model_when_enabled(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, TRANSCRIPTION_BACKEND=TranscriptionBackend.WHISPER_CPP)


def test_whisper_cpp_settings_accept_existing_paths(tmp_path: Path) -> None:
    binary = tmp_path / "main"
    model = tmp_path / "ggml-turbo.bin"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    model.write_bytes(b"model")

    settings = make_settings(
        tmp_path,
        TRANSCRIPTION_BACKEND=TranscriptionBackend.WHISPER_CPP,
        WHISPER_CPP_BINARY=str(binary),
        WHISPER_CPP_MODEL=str(model),
        WHISPER_CPP_EXTRA_ARGS="--language en",
    )

    assert settings.whisper_cpp_binary == binary.resolve()
    assert settings.whisper_cpp_model == model.resolve()
    assert settings.whisper_cpp_extra_args == ("--language", "en")


def test_load_settings_reads_local_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.env"
    write_env_file(
        config_path,
        {
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "ALLOWED_TELEGRAM_USER_IDS": "42",
            "WORKSPACE_ROOT": str(tmp_path),
            "DEFAULT_AGENT_COMMAND": "gemini",
            "DEFAULT_AGENT_ADAPTER": "generic",
            "DEFAULT_TELEGRAM_CHAT_ID": "",
            "WHISPER_BACKEND": "local",
            "WHISPER_MODEL": "base",
        },
    )

    settings = load_settings(config_path)

    assert settings.allowed_telegram_user_ids == (42,)
    assert settings.default_agent_command == "gemini"
    assert settings.whisper_model == "base"
