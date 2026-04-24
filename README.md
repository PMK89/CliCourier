# CliCourier

CliCourier is a local-first Telegram bridge for controlling a trusted CLI agent from a
private Telegram chat. It starts one configured agent command on your workstation, sends
debounced final CLI output back to Telegram, and exposes a small set of workspace-scoped
file, approval, screenshot, and voice-transcription commands.

The MVP is intentionally single-user and single-session. It is built for a local Linux
workstation where Telegram is the remote control surface, not the security boundary.

## Features

- Telegram allowlist gate before every handler.
- Configured CLI agent only; Telegram users cannot run arbitrary shell commands.
- Codex CLI adapter plus a generic CLI adapter for testing and future agents.
- PTY-backed agent session with output sanitization, chunking, and recent-output buffer.
- Final-output Telegram delivery by default, with noisy tool/status lines suppressed.
- Pending approval detection with explicit `/approve`, `/reject`, and nonce-backed buttons.
- Workspace sandbox for `/ls`, `/tree`, `/cd`, `/cat`, and `/sendfile`.
- Sensitive file blocking for env files, private keys, cloud credentials, and token-like names.
- Newest screenshot artifact retrieval from a configured directory.
- Local `whisper.cpp` voice transcription with transcript confirmation before sending.
- Optional OpenAI voice transcription for users who prefer an API backend.
- Background daemon controls through the `clicourier` command.
- A mute/block file so proactive Telegram output can be paused when you are at the computer.

## Requirements

- Python 3.11 or newer.
- A Telegram bot token from BotFather.
- Your numeric Telegram user id.
- A local agent command, such as `codex`.

## Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

## Configuration

Create `.env` from `.env.example` and fill in the required values:

```bash
TELEGRAM_BOT_TOKEN=123456:replace-me
ALLOWED_TELEGRAM_USER_IDS=123456789
WORKSPACE_ROOT=/absolute/path/to/workspace
DEFAULT_AGENT_COMMAND=codex
```

Or run the interactive setup:

```bash
clicourier setup
```

This writes `~/.config/clicourier/config.env` and can install a `~/.local/bin/clicourier`
launcher for source-tree use.

Useful optional settings:

```bash
DEFAULT_AGENT_ADAPTER=codex
AUTO_START_AGENT=true
DEFAULT_TELEGRAM_CHAT_ID=123456789
AGENT_ENV_ALLOWLIST=OPENAI_API_KEY
SCREENSHOT_DIR=/absolute/path/to/workspace/screenshots
TRANSCRIPTION_BACKEND=whisper_cpp
WHISPER_CPP_BINARY=/home/you/.local/share/clicourier/whisper.cpp/main
WHISPER_CPP_MODEL=/home/you/.local/share/clicourier/whisper.cpp/models/ggml-turbo.bin
AGENT_OUTPUT_MODE=final
NOTIFICATION_BLOCK_FILE=/home/you/.local/state/clicourier/muted
MAX_TELEGRAM_CHUNK_CHARS=3500
```

`AGENT_ENV_ALLOWLIST` is only needed for environment variables the child CLI agent must
see. Bridge secrets are not forwarded by default.

Security-sensitive defaults are conservative: group chats are disabled, screenshot
directories must stay under `WORKSPACE_ROOT`, sensitive files are not sent, and
unauthorized users are ignored.

## Local Whisper

```bash
clicourier setup-whisper
```

The helper clones `whisper.cpp`, builds it with `make`, downloads the selected ggml model
(`turbo` by default), and appends the local backend paths to your config. Telegram voice
messages are still confirmed before being sent to the agent.

Manual equivalent:

```bash
git clone https://github.com/ggerganov/whisper.cpp.git ~/.local/share/clicourier/whisper.cpp
cd ~/.local/share/clicourier/whisper.cpp
make
bash ./models/download-ggml-model.sh turbo
```

Set `TRANSCRIPTION_BACKEND=whisper_cpp`, `WHISPER_CPP_BINARY`, and `WHISPER_CPP_MODEL`
to use the local model.

## Run

Foreground:

```bash
clicourier run -- codex
```

Background daemon:

```bash
clicourier start -- codex
clicourier status
clicourier stop
```

Any CLI command can be used after `--`, for example `claude` or `gemini`. For non-Codex
tools use `DEFAULT_AGENT_ADAPTER=generic`; setup infers this for common non-Codex commands.

All non-command text from an allowlisted user is sent to the active agent, except
approval-like words such as `yes` or `approve` when no approval is pending. Use
`/agent yes` to send those words literally.

By default `AGENT_OUTPUT_MODE=final`, so CliCourier waits for a quiet period before sending
agent output to Telegram and suppresses common reasoning/tool/status trace lines. Set
`AGENT_OUTPUT_MODE=stream` if you want raw streaming output.

To pause proactive Telegram output while you are at the machine:

```bash
clicourier mute
clicourier unmute
```

The same toggle is available from Telegram with `/mute`, `/unmute`, and `/mute_status`.

## Development

```bash
python -m pytest
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m cli_courier.cli status
```

The integration test uses `tests/fixtures/fake_agent.py` so PTY behavior can be tested
without Codex, Claude, Gemini, or a Telegram bot token.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) explains the module boundaries and data flow.
- [COMMANDS.md](COMMANDS.md) lists the Telegram command surface.
- [SECURITY.md](SECURITY.md) documents the threat model and local hardening notes.
- [ROADMAP.md](ROADMAP.md) tracks planned work beyond the MVP.
