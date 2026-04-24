from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol


class TranscriptionDisabled(RuntimeError):
    """Raised when voice transcription is not configured."""


class Transcriber(Protocol):
    async def transcribe(self, path: Path) -> str: ...


class DisabledTranscriber:
    async def transcribe(self, path: Path) -> str:
        raise TranscriptionDisabled("voice transcription is disabled")


class OpenAITranscriber:
    def __init__(self, *, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def transcribe(self, path: Path) -> str:
        return await asyncio.to_thread(self._transcribe_sync, path)

    def _transcribe_sync(self, path: Path) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        with path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=self.model,
                file=audio_file,
            )
        text = getattr(response, "text", None)
        if isinstance(text, str):
            return text.strip()
        return str(response).strip()


class FasterWhisperTranscriber:
    def __init__(
        self,
        *,
        model: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        model_dir: Path | None = None,
        ffmpeg_binary: str = "ffmpeg",
    ) -> None:
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.model_dir = model_dir
        self.ffmpeg_binary = ffmpeg_binary

    async def transcribe(self, path: Path) -> str:
        return await asyncio.to_thread(self._transcribe_sync, path)

    def _transcribe_sync(self, path: Path) -> str:
        with tempfile.TemporaryDirectory(prefix="cli-courier-faster-whisper-") as temp_dir:
            wav_path = Path(temp_dir) / "audio.wav"
            convert_audio_to_wav(
                source=path,
                target=wav_path,
                ffmpeg_binary=self.ffmpeg_binary,
            )
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "faster-whisper is not installed. Reinstall CliCourier with uv or pipx."
                ) from exc

            kwargs: dict[str, str] = {
                "device": self.device,
                "compute_type": self.compute_type,
            }
            if self.model_dir is not None:
                self.model_dir.mkdir(parents=True, exist_ok=True)
                kwargs["download_root"] = str(self.model_dir)
            try:
                model = WhisperModel(self.model, **kwargs)
                segments, _info = model.transcribe(str(wav_path))
                transcript = " ".join(segment.text.strip() for segment in segments).strip()
            except Exception as exc:  # noqa: BLE001 - convert backend errors into operator action
                raise RuntimeError(
                    "local Whisper transcription failed. Check ffmpeg, faster-whisper, and "
                    f"model '{self.model}' on {self.device}/{self.compute_type}."
                ) from exc
            if not transcript:
                raise RuntimeError("local Whisper returned an empty transcript")
            return transcript


class WhisperCppTranscriber:
    def __init__(
        self,
        *,
        binary: Path,
        model: Path,
        ffmpeg_binary: str = "ffmpeg",
        extra_args: tuple[str, ...] = (),
        timeout_seconds: int = 120,
    ) -> None:
        self.binary = binary
        self.model = model
        self.ffmpeg_binary = ffmpeg_binary
        self.extra_args = extra_args
        self.timeout_seconds = timeout_seconds

    async def transcribe(self, path: Path) -> str:
        return await asyncio.to_thread(self._transcribe_sync, path)

    def _transcribe_sync(self, path: Path) -> str:
        with tempfile.TemporaryDirectory(prefix="cli-courier-whisper-") as temp_dir:
            wav_path = Path(temp_dir) / "audio.wav"
            self._convert_to_wav(path, wav_path)
            command = [
                str(self.binary),
                "-m",
                str(self.model),
                "-f",
                str(wav_path),
                "-nt",
                *self.extra_args,
            ]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            if completed.returncode != 0:
                error = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(f"whisper.cpp failed: {error}")
            transcript = _clean_whisper_output(completed.stdout)
            if not transcript:
                transcript = _clean_whisper_output(completed.stderr)
            if not transcript:
                raise RuntimeError("whisper.cpp returned an empty transcript")
            return transcript

    def _convert_to_wav(self, source: Path, target: Path) -> None:
        convert_audio_to_wav(
            source=source,
            target=target,
            ffmpeg_binary=self.ffmpeg_binary,
            timeout_seconds=self.timeout_seconds,
        )


def convert_audio_to_wav(
    *,
    source: Path,
    target: Path,
    ffmpeg_binary: str = "ffmpeg",
    timeout_seconds: int = 120,
) -> None:
    if shutil.which(ffmpeg_binary) is None and not Path(ffmpeg_binary).exists():
        raise RuntimeError(
            "ffmpeg is required to decode Telegram voice messages (OGG/Opus). "
            "Install ffmpeg and make sure it is on PATH."
        )
    command = [
        ffmpeg_binary,
        "-y",
        "-i",
        str(source),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            "ffmpeg could not decode the Telegram voice message. Install ffmpeg with OGG/Opus "
            f"support. Details: {error}"
        )


def _clean_whisper_output(output: str) -> str:
    lines: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("whisper_") or lower.startswith("main:") or "load time" in lower:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


async def transcribe_with_cleanup(transcriber: Transcriber, path: Path) -> str:
    try:
        return await transcriber.transcribe(path)
    finally:
        path.unlink(missing_ok=True)
