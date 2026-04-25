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
- Local CPU-only `faster-whisper` voice transcription with transcript confirmation before sending.
- Optional OpenAI voice transcription for users who prefer an API backend.
- Background daemon controls through the `clicourier` command.
- A mute/block file so proactive Telegram output can be paused when you are at the computer.

## Requirements

- Python 3.11 or newer.
- A Telegram bot token from BotFather.
- Your numeric Telegram user id.
- A local agent command, such as `codex`.

## Installation

One-command install for Linux or WSL:

```bash
curl -LsSf https://raw.githubusercontent.com/PMK89/CliCourier/main/install.sh | sh
```

The installer uses `uv tool` and creates `~/.config/clicourier` if needed.

Manual install with uv:

```bash
uv tool install git+https://github.com/PMK89/CliCourier.git
```

Manual install with pipx:

```bash
pipx install git+https://github.com/PMK89/CliCourier.git
```

From a checkout:

```bash
uv tool install --force .
```

## Configuration

Create `.env` from `.env.example` and fill in the required values:

```bash
TELEGRAM_BOT_TOKEN=123456:replace-me
ALLOWED_TELEGRAM_USER_IDS=123456789
WORKSPACE_ROOT=.
DEFAULT_AGENT_COMMAND=codex
```

Or run the interactive setup:

```bash
clicourier init
```

This writes `~/.config/clicourier/config.env`. If the file already exists, `init` loads
its current values as prompt defaults and asks before writing the updated config.

`WORKSPACE_ROOT=.` means the project directory where you start `clicourier`. This is the
recommended workflow: `cd` into your project, then run `clicourier run` or
`clicourier start`.

Useful optional settings:

```bash
DEFAULT_AGENT_ADAPTER=codex
AUTO_START_AGENT=true
DEFAULT_TELEGRAM_CHAT_ID=123456789
AGENT_ENV_ALLOWLIST=OPENAI_API_KEY
SCREENSHOT_DIR=/absolute/path/to/workspace/screenshots
WHISPER_BACKEND=local
WHISPER_MODEL=small
WHISPER_COMPUTE_TYPE=int8
WHISPER_DEVICE=cpu
WHISPER_MODEL_DIR=
FFMPEG_BINARY=ffmpeg
AGENT_OUTPUT_MODE=final
NOTIFICATION_BLOCK_FILE=muted
MAX_TELEGRAM_CHUNK_CHARS=3500
```

`AGENT_ENV_ALLOWLIST` is only needed for environment variables the child CLI agent must
see. Bridge secrets are not forwarded by default.

`DEFAULT_TELEGRAM_CHAT_ID` is only for proactive background output, such as auto-start
messages before you send a command. The bot can only message a private chat after you have
opened the bot in Telegram and sent `/start`; if the chat is not reachable, CliCourier now
logs that and keeps polling instead of crashing.

`AGENT_INITIAL_PROMPT_ENABLED=true` sends a short one-time context note to the CLI agent
when it starts, explaining that it is being controlled through CliCourier and should keep
Telegram-facing final output concise.

Security-sensitive defaults are conservative: group chats are disabled, screenshot
directories must stay under `WORKSPACE_ROOT`, sensitive files are not sent, and
unauthorized users are ignored.

## Local Whisper

```bash
clicourier model download
clicourier model list
```

Voice transcription defaults to local `faster-whisper`, CPU device, and `int8` compute.
The default model is `small`, configurable with `WHISPER_MODEL`. Recommended CPU choices
are `base`, `small`, and `turbo`; `turbo` is accepted as an alias for
`large-v3-turbo`. Models are downloaded lazily on first use or explicitly through
`clicourier model download`.

Telegram voice messages are OGG/Opus. CliCourier converts them to 16 kHz mono WAV with
`ffmpeg` before transcription, so `ffmpeg` must be installed and available on PATH.

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
By default this creates a `muted` file in the project working directory.

## Development

```bash
uv sync
uv run clicourier run
uv run pytest
```

The integration test uses `tests/fixtures/fake_agent.py` so PTY behavior can be tested
without Codex, Claude, Gemini, or a Telegram bot token.

## WSL

Windows support means WSL for the MVP. Install and run CliCourier inside a Linux
distribution, use Linux paths such as `/home/you/project`, and run Linux CLI tools from
inside WSL. Native Windows terminals and PowerShell are not supported yet.

## Docker

Docker is not the recommended path because CliCourier is meant to control host coding
agents and local workspaces. Local uv or pipx installation is the default. A Docker setup
can be added later for isolated experiments, but it is not included in this MVP.

## Security

CliCourier controls a real local CLI process from Telegram. Keep the Telegram allowlist
narrow, do not run as root, keep `WORKSPACE_ROOT` scoped to the project you want to expose,
and leave sensitive file sending disabled by default.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) explains the module boundaries and data flow.
- [COMMANDS.md](COMMANDS.md) lists the Telegram command surface.
- [SECURITY.md](SECURITY.md) documents the threat model and local hardening notes.
- [ROADMAP.md](ROADMAP.md) tracks planned work beyond the MVP.
