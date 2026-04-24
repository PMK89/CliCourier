from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cli_courier.config import Settings


KNOWN_LOCAL_MODELS = (
    "tiny",
    "base",
    "small",
    "medium",
    "large-v3",
    "distil-small.en",
    "distil-medium.en",
    "distil-large-v3",
)


@dataclass(frozen=True)
class ModelCacheStatus:
    model: str
    cache_dir: Path | None
    status: str


def download_model(settings: Settings, *, model_name: str | None = None) -> None:
    name = model_name or settings.whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Install CliCourier with `uv tool install` or `pipx`."
        ) from exc

    kwargs: dict[str, str] = {
        "device": settings.whisper_device,
        "compute_type": settings.whisper_compute_type,
    }
    if settings.whisper_model_dir is not None:
        settings.whisper_model_dir.mkdir(parents=True, exist_ok=True)
        kwargs["download_root"] = str(settings.whisper_model_dir)
    WhisperModel(name, **kwargs)


def model_cache_status(settings: Settings) -> ModelCacheStatus:
    if settings.whisper_model_dir is None:
        return ModelCacheStatus(
            model=settings.whisper_model,
            cache_dir=None,
            status="managed by faster-whisper cache (not inspected)",
        )
    cache_dir = settings.whisper_model_dir
    if not cache_dir.exists():
        return ModelCacheStatus(model=settings.whisper_model, cache_dir=cache_dir, status="missing")
    has_files = any(cache_dir.iterdir())
    return ModelCacheStatus(
        model=settings.whisper_model,
        cache_dir=cache_dir,
        status="present" if has_files else "empty",
    )


def format_model_list(settings: Settings) -> str:
    cache = model_cache_status(settings)
    lines = [
        f"configured: {settings.whisper_model}",
        f"backend: {settings.whisper_backend.value}",
        f"device: {settings.whisper_device}",
        f"compute_type: {settings.whisper_compute_type}",
        f"cache: {cache.status}",
    ]
    if cache.cache_dir is not None:
        lines.append(f"cache_dir: {cache.cache_dir}")
    lines.append("known models: " + ", ".join(KNOWN_LOCAL_MODELS))
    return "\n".join(lines)

