from __future__ import annotations

from pathlib import Path

import pytest

from cli_courier.voice import (
    DisabledTranscriber,
    TranscriptionDisabled,
    WhisperCppTranscriber,
    transcribe_with_cleanup,
)


class EchoTranscriber:
    async def transcribe(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")


async def test_transcribe_with_cleanup_deletes_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "voice.txt"
    path.write_text("hello", encoding="utf-8")

    result = await transcribe_with_cleanup(EchoTranscriber(), path)

    assert result == "hello"
    assert not path.exists()


async def test_disabled_transcriber_raises(tmp_path: Path) -> None:
    path = tmp_path / "voice.oga"
    path.write_bytes(b"data")

    with pytest.raises(TranscriptionDisabled):
        await DisabledTranscriber().transcribe(path)


async def test_whisper_cpp_transcriber_uses_local_binary(tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg"
    whisper = tmp_path / "whisper"
    model = tmp_path / "model.bin"
    source = tmp_path / "voice.oga"
    model.write_bytes(b"model")
    source.write_bytes(b"audio")
    ffmpeg.write_text(
        "#!/bin/sh\n"
        "out=\"\"\n"
        "for arg in \"$@\"; do out=\"$arg\"; done\n"
        "printf wav > \"$out\"\n",
        encoding="utf-8",
    )
    whisper.write_text("#!/bin/sh\nprintf 'local transcript\\n'\n", encoding="utf-8")
    ffmpeg.chmod(0o755)
    whisper.chmod(0o755)

    transcriber = WhisperCppTranscriber(
        binary=whisper,
        model=model,
        ffmpeg_binary=str(ffmpeg),
        timeout_seconds=5,
    )

    assert await transcriber.transcribe(source) == "local transcript"
