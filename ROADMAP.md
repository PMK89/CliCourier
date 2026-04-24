# Roadmap

## MVP

- Python package scaffold and environment-backed config.
- Telegram allowlist, command routing, and text-to-agent forwarding.
- PTY agent lifecycle with Codex and generic adapters.
- Output chunking, terminal sanitization, and recent-output buffering.
- Conservative approval detection with explicit approve/reject flow.
- Workspace sandbox commands for listing, reading, and sending files.
- Newest screenshot artifact retrieval.
- Local whisper.cpp transcription with transcript confirmation.
- Background daemon controls via `clicourier`.
- Final-output delivery mode with trace-line suppression.
- Local mute block file for pausing proactive Telegram output.
- Unit and fake-agent integration tests.

## Next

- Multiple named sessions with persisted state.
- Claude Code and Gemini CLI adapters.
- faster-whisper transcription backend.
- Tool-specific final-answer extractors for Claude Code and Gemini CLI.
- Configured screenshot command backend with strict setup-time configuration.
- Optional encrypted state and log persistence.
- Webhook deployment mode while keeping local polling as the default.
- Richer Telegram UI for session switching and approval context.
