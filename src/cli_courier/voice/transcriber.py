from __future__ import annotations

import asyncio
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
        command = [
            self.ffmpeg_binary,
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
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"ffmpeg failed before whisper.cpp transcription: {error}")


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
