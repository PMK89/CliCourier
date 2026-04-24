from __future__ import annotations

import shlex
from enum import Enum
import os
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cli_courier.local_config import default_config_path, default_mute_file


class ConfigError(ValueError):
    """Raised when runtime configuration is invalid."""


class TranscriptionBackend(str, Enum):
    NONE = "none"
    OPENAI = "openai"
    WHISPER_CPP = "whisper_cpp"


class UnauthorizedReplyMode(str, Enum):
    IGNORE = "ignore"
    GENERIC = "generic"


class AgentOutputMode(str, Enum):
    FINAL = "final"
    STREAM = "stream"


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


class Settings(BaseSettings):
    """Environment-backed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    telegram_bot_token: SecretStr = Field(alias="TELEGRAM_BOT_TOKEN")
    allowed_telegram_user_ids: tuple[int, ...] = Field(alias="ALLOWED_TELEGRAM_USER_IDS")
    workspace_root: Path = Field(alias="WORKSPACE_ROOT")
    default_agent_command: str = Field(alias="DEFAULT_AGENT_COMMAND")

    default_agent_adapter: str = Field(default="codex", alias="DEFAULT_AGENT_ADAPTER")
    screenshot_dir: Path | None = Field(default=None, alias="SCREENSHOT_DIR")
    transcription_backend: TranscriptionBackend = Field(
        default=TranscriptionBackend.NONE,
        alias="TRANSCRIPTION_BACKEND",
    )
    transcription_openai_api_key: SecretStr | None = Field(
        default=None,
        alias="TRANSCRIPTION_OPENAI_API_KEY",
    )
    openai_transcription_model: str = Field(
        default="gpt-4o-mini-transcribe",
        alias="OPENAI_TRANSCRIPTION_MODEL",
    )
    whisper_cpp_binary: Path | None = Field(default=None, alias="WHISPER_CPP_BINARY")
    whisper_cpp_model: Path | None = Field(default=None, alias="WHISPER_CPP_MODEL")
    whisper_cpp_ffmpeg_binary: str = Field(default="ffmpeg", alias="WHISPER_CPP_FFMPEG_BINARY")
    whisper_cpp_extra_args: tuple[str, ...] = Field(default=(), alias="WHISPER_CPP_EXTRA_ARGS")
    whisper_cpp_timeout_seconds: int = Field(default=120, alias="WHISPER_CPP_TIMEOUT_SECONDS")

    max_telegram_chunk_chars: int = Field(default=3500, alias="MAX_TELEGRAM_CHUNK_CHARS")
    output_flush_interval_ms: int = Field(default=1000, alias="OUTPUT_FLUSH_INTERVAL_MS")
    final_output_idle_ms: int = Field(default=2500, alias="FINAL_OUTPUT_IDLE_MS")
    final_output_max_wait_ms: int = Field(default=120000, alias="FINAL_OUTPUT_MAX_WAIT_MS")
    recent_output_max_chars: int = Field(default=100000, alias="RECENT_OUTPUT_MAX_CHARS")
    cat_max_bytes: int = Field(default=65536, alias="CAT_MAX_BYTES")
    sendfile_max_bytes: int = Field(default=10485760, alias="SENDFILE_MAX_BYTES")
    voice_max_bytes: int = Field(default=25000000, alias="VOICE_MAX_BYTES")
    screenshot_max_bytes: int = Field(default=10485760, alias="SCREENSHOT_MAX_BYTES")

    allow_group_chats: bool = Field(default=False, alias="ALLOW_GROUP_CHATS")
    allow_sensitive_file_send: bool = Field(default=False, alias="ALLOW_SENSITIVE_FILE_SEND")
    allow_screenshot_dir_outside_workspace: bool = Field(
        default=False,
        alias="ALLOW_SCREENSHOT_DIR_OUTSIDE_WORKSPACE",
    )
    unauthorized_reply_mode: UnauthorizedReplyMode = Field(
        default=UnauthorizedReplyMode.IGNORE,
        alias="UNAUTHORIZED_REPLY_MODE",
    )
    agent_env_allowlist: tuple[str, ...] = Field(default=(), alias="AGENT_ENV_ALLOWLIST")
    agent_output_mode: AgentOutputMode = Field(default=AgentOutputMode.FINAL, alias="AGENT_OUTPUT_MODE")
    suppress_agent_trace_lines: bool = Field(default=True, alias="SUPPRESS_AGENT_TRACE_LINES")
    auto_start_agent: bool = Field(default=False, alias="AUTO_START_AGENT")
    default_telegram_chat_id: int | None = Field(default=None, alias="DEFAULT_TELEGRAM_CHAT_ID")
    notification_block_file: Path = Field(
        default_factory=default_mute_file,
        alias="NOTIFICATION_BLOCK_FILE",
    )

    @field_validator("allowed_telegram_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, value: Any) -> tuple[int, ...]:
        ids = tuple(int(part) for part in _split_csv(value))
        if not ids:
            raise ConfigError("ALLOWED_TELEGRAM_USER_IDS must include at least one user id")
        if any(user_id <= 0 for user_id in ids):
            raise ConfigError("ALLOWED_TELEGRAM_USER_IDS must contain positive integer ids")
        return ids

    @field_validator("agent_env_allowlist", mode="before")
    @classmethod
    def parse_env_allowlist(cls, value: Any) -> tuple[str, ...]:
        return tuple(_split_csv(value))

    @field_validator("whisper_cpp_extra_args", mode="before")
    @classmethod
    def parse_whisper_extra_args(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return tuple(shlex.split(value)) if value.strip() else ()
        if isinstance(value, (list, tuple, set)):
            return tuple(str(part) for part in value)
        return (str(value),)

    @field_validator("default_telegram_chat_id", mode="before")
    @classmethod
    def normalize_optional_int(cls, value: Any) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        return int(value)

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if not resolved.exists():
            raise ConfigError(f"WORKSPACE_ROOT does not exist: {resolved}")
        if not resolved.is_dir():
            raise ConfigError(f"WORKSPACE_ROOT must be a directory: {resolved}")
        return resolved

    @field_validator("screenshot_dir", mode="before")
    @classmethod
    def normalize_screenshot_dir(cls, value: Any) -> Path | None:
        if value is None or str(value).strip() == "":
            return None
        return Path(value).expanduser().resolve()

    @field_validator("whisper_cpp_binary", "whisper_cpp_model", mode="before")
    @classmethod
    def normalize_optional_path(cls, value: Any) -> Path | None:
        if value is None or str(value).strip() == "":
            return None
        return Path(value).expanduser().resolve()

    @field_validator("notification_block_file", mode="before")
    @classmethod
    def normalize_notification_block_file(cls, value: Any) -> Path:
        if value is None or str(value).strip() == "":
            return default_mute_file()
        return Path(value).expanduser()

    @field_validator("default_agent_command")
    @classmethod
    def validate_agent_command(cls, value: str) -> str:
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            raise ConfigError(f"DEFAULT_AGENT_COMMAND is not valid shell-style syntax: {exc}") from exc
        if not parts:
            raise ConfigError("DEFAULT_AGENT_COMMAND must not be empty")
        return value

    @field_validator(
        "max_telegram_chunk_chars",
        "output_flush_interval_ms",
        "final_output_idle_ms",
        "final_output_max_wait_ms",
        "recent_output_max_chars",
        "cat_max_bytes",
        "sendfile_max_bytes",
        "voice_max_bytes",
        "screenshot_max_bytes",
        "whisper_cpp_timeout_seconds",
    )
    @classmethod
    def validate_positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ConfigError("numeric limits must be positive")
        return value

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> "Settings":
        if (
            self.transcription_backend == TranscriptionBackend.OPENAI
            and (
                self.transcription_openai_api_key is None
                or not self.transcription_openai_api_key.get_secret_value().strip()
            )
        ):
            raise ConfigError(
                "TRANSCRIPTION_OPENAI_API_KEY is required when TRANSCRIPTION_BACKEND=openai"
            )
        if self.transcription_backend == TranscriptionBackend.WHISPER_CPP:
            if self.whisper_cpp_binary is None:
                raise ConfigError("WHISPER_CPP_BINARY is required when TRANSCRIPTION_BACKEND=whisper_cpp")
            if self.whisper_cpp_model is None:
                raise ConfigError("WHISPER_CPP_MODEL is required when TRANSCRIPTION_BACKEND=whisper_cpp")
            if not self.whisper_cpp_binary.exists():
                raise ConfigError(f"WHISPER_CPP_BINARY does not exist: {self.whisper_cpp_binary}")
            if not self.whisper_cpp_model.exists():
                raise ConfigError(f"WHISPER_CPP_MODEL does not exist: {self.whisper_cpp_model}")

        if self.screenshot_dir is not None and not self.allow_screenshot_dir_outside_workspace:
            try:
                self.screenshot_dir.relative_to(self.workspace_root)
            except ValueError as exc:
                raise ConfigError(
                    "SCREENSHOT_DIR must be inside WORKSPACE_ROOT unless "
                    "ALLOW_SCREENSHOT_DIR_OUTSIDE_WORKSPACE=true"
                ) from exc

        if self.max_telegram_chunk_chars > 4000:
            raise ConfigError("MAX_TELEGRAM_CHUNK_CHARS must stay below Telegram's hard limit")
        if self.final_output_idle_ms > self.final_output_max_wait_ms:
            raise ConfigError("FINAL_OUTPUT_IDLE_MS must be <= FINAL_OUTPUT_MAX_WAIT_MS")

        return self

    @property
    def agent_command_parts(self) -> list[str]:
        return shlex.split(self.default_agent_command)


def load_settings(config_path: Path | None = None) -> Settings:
    env_files: list[Path] = []
    env_config = os.environ.get("CLICOURIER_CONFIG")
    configured = config_path or (Path(env_config).expanduser() if env_config else None)
    if configured is not None and configured.exists():
        env_files.append(configured)
    default_path = default_config_path()
    if default_path.exists() and default_path not in env_files:
        env_files.append(default_path)
    cwd_env = Path(".env")
    if cwd_env.exists():
        env_files.append(cwd_env)
    return Settings(_env_file=tuple(env_files) if env_files else None)
