"""Benchmark each stage of the V2T pipeline.

Run with: uv run pytest -m slow -s tests/unit/test_voice_benchmark.py

Requires:
  - faster-whisper installed
  - A locally cached model (no download attempted)
  - ffmpeg on PATH
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest


def _find_cached_whisper_model() -> tuple[str, str] | None:
    """Return (model_name, hf_cache_dir) if any supported model is cached locally."""
    hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
    candidates = [
        ("base", "models--Systran--faster-whisper-base"),
        ("small", "models--Systran--faster-whisper-small"),
        ("large-v3-turbo", "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"),
    ]
    for model_name, hf_dir in candidates:
        snapshots = hf_cache / hf_dir / "snapshots"
        if not snapshots.exists():
            continue
        for snap in sorted(snapshots.iterdir(), reverse=True):
            if (snap / "model.bin").exists():
                return model_name, str(hf_cache)
    return None


def _generate_ogg(path: Path, duration_s: float = 5.0) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_s}",
            "-c:a", "libopus", "-ar", "48000",
            str(path),
        ],
        capture_output=True,
        check=True,
    )


@pytest.mark.slow
def test_v2t_pipeline_benchmark(tmp_path: Path) -> None:
    """Time each V2T stage and print a breakdown to identify bottlenecks."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        pytest.skip("faster-whisper not installed")

    cached = _find_cached_whisper_model()
    if cached is None:
        pytest.skip("No cached faster-whisper model found; skipping benchmark (avoids download)")

    model_name, hf_cache = cached
    from cli_courier.voice.transcriber import convert_audio_to_wav

    # ------------------------------------------------------------------
    # Stage 0: generate a synthetic 5s OGG file (simulates Telegram voice)
    # ------------------------------------------------------------------
    ogg_path = tmp_path / "test.oga"
    wav_path = tmp_path / "test.wav"
    _generate_ogg(ogg_path, duration_s=5.0)

    # ------------------------------------------------------------------
    # Stage 1: ffmpeg OGG → 16 kHz mono WAV (always happens per-call)
    # ------------------------------------------------------------------
    t0 = time.perf_counter()
    convert_audio_to_wav(source=ogg_path, target=wav_path)
    ffmpeg_ms = (time.perf_counter() - t0) * 1000

    # ------------------------------------------------------------------
    # Stage 2: cold model load (what FasterWhisperTranscriber used to do
    #          on EVERY call before the caching fix)
    # ------------------------------------------------------------------
    t0 = time.perf_counter()
    model = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=hf_cache)
    model_load_ms = (time.perf_counter() - t0) * 1000

    # ------------------------------------------------------------------
    # Stage 3: first inference (cold — model freshly loaded)
    # ------------------------------------------------------------------
    t0 = time.perf_counter()
    segs, _ = model.transcribe(str(wav_path))
    _ = " ".join(s.text.strip() for s in segs).strip()
    inference_cold_ms = (time.perf_counter() - t0) * 1000

    # ------------------------------------------------------------------
    # Stage 4: second inference (warm — model cached on transcriber)
    # ------------------------------------------------------------------
    t0 = time.perf_counter()
    segs, _ = model.transcribe(str(wav_path))
    _ = " ".join(s.text.strip() for s in segs).strip()
    inference_warm_ms = (time.perf_counter() - t0) * 1000

    total_cold_ms = ffmpeg_ms + model_load_ms + inference_cold_ms
    total_warm_ms = ffmpeg_ms + inference_warm_ms

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    print(f"\nModel: {model_name}  |  Audio: 5s OGG/Opus\n")
    print(f"{'Stage':<38} {'ms':>8}")
    print("-" * 48)
    print(f"{'ffmpeg OGG→WAV':<38} {ffmpeg_ms:>8.1f}")
    print(f"{'WhisperModel load (cold)':<38} {model_load_ms:>8.1f}  ← reloaded per call BEFORE fix")
    print(f"{'Inference (cold cycle, 1st call)':<38} {inference_cold_ms:>8.1f}")
    print(f"{'Inference (warm, model cached)':<38} {inference_warm_ms:>8.1f}")
    print("-" * 48)
    print(f"{'Total WITHOUT model cache (old)':<38} {total_cold_ms:>8.1f}")
    print(f"{'Total WITH model cache (fixed)':<38} {total_warm_ms:>8.1f}")
    print(f"\nModel reload overhead per voice message: {model_load_ms:.0f} ms")
    speedup = total_cold_ms / total_warm_ms if total_warm_ms > 0 else 0
    print(f"End-to-end speedup from caching:         {speedup:.1f}x")

    # Sanity bounds — not strict timing SLAs, just guards against broken state
    assert ffmpeg_ms < 5000, f"ffmpeg took {ffmpeg_ms:.0f} ms — suspiciously slow"
    assert model_load_ms < 30_000, f"model load took {model_load_ms:.0f} ms"
    assert inference_warm_ms < 30_000, f"warm inference took {inference_warm_ms:.0f} ms"
