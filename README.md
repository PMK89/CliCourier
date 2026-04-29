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
- Codex CLI adapter, Claude/Gemini fallback adapters, and a generic adapter for testing.
- Structured JSONL sessions: Codex via `codex exec --json`, Claude Code via `claude --print --output-format stream-json`.
- Terminal-backed tmux/PTY sessions remain available as fallback for TUI-first agents.
- One throttled Telegram dashboard message per active session, plus a dedicated `/progress` rolling message and separate final/error/artifact messages.
- Event-backed approvals with explicit `/approve`, `/reject`, reactions, and pending-action buttons.
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
AGENT_TERMINAL_BACKEND=auto
AGENT_TMUX_SESSION=clicourier
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

Interactive run:

```bash
clicourier run -- codex
```

`clicourier run` asks for `desktop` or `telegram` mode when launched from a terminal.
Desktop mode creates the mute file, starts the bridge daemon in the background, and
attaches to the agent's tmux terminal so the CLI stays visible and usable locally.
Telegram mode removes the mute file, auto-starts the configured agent in tmux, and
attaches to that terminal so the console stays visible and interactive while output is
forwarded to Telegram. If no agent is running when a Telegram request arrives, CliCourier
starts the configured agent automatically for that chat.

```bash
clicourier run --mode telegram -- codex
clicourier run --mode telegram
```

Background daemon:

```bash
clicourier start -- codex
clicourier start --resume -- codex
clicourier restart -- codex
clicourier restart --no-resume -- codex
clicourier status
clicourier stop
```

Any CLI command can be used after `--`, for example `claude` or `gemini`. For non-Codex
tools set `DEFAULT_AGENT_ADAPTER` to the matching adapter id (`claude`, `gemini`, or
`generic`); setup infers this for common commands.

**Codex** (`DEFAULT_AGENT_ADAPTER=codex`): starts structured turns with
`codex exec --json <prompt>`; follow-up turns use `codex exec resume --last --json
<prompt>`.

**Claude Code** (`DEFAULT_AGENT_ADAPTER=claude`): starts structured turns with
`claude --print --output-format stream-json --verbose [--continue] <prompt>`.
Because CliCourier drives Claude in non-interactive `--print` mode, interactive
permission prompts cannot be answered. You must pass `--dangerously-skip-permissions`
(or `--permission-mode bypassPermissions`) in your agent command so that tool calls are
not blocked:

```bash
clicourier start -- claude --dangerously-skip-permissions
```

Or set `DEFAULT_AGENT_COMMAND=claude --dangerously-skip-permissions` in `.env`.

Final answers, tool events, and status updates arrive as JSONL events for both Codex and
Claude. `clicourier restart` and Telegram `/restart` resume the most recent session by
default; pass `--no-resume` for a fresh session. Local `clicourier restart` starts the
agent in tmux and attaches when run from an interactive terminal; use `--detach` to
restart without attaching. Telegram `/restart` uses detached restart, opens a local
terminal attached to tmux when a desktop terminal is available, and replies with the
manual `tmux attach` command as fallback. Telegram `/resume` restarts the configured
agent directly in resume mode.
When you force `AGENT_TERMINAL_BACKEND=tmux` or `pty`, CliCourier falls back to terminal
capture for local/TUI workflows.

All non-command text from an allowlisted user is sent to the active agent, except
approval-like words such as `yes` or `approve` when no approval is pending. Use
`/agent yes` to send those words literally.

Unknown Telegram slash commands are forwarded raw to the agent, so CLI-native commands such
as `/model` or `/reasoning` still keep their leading `/` and still work on the first turn.
CliCourier no longer parses arbitrary terminal
output into menu buttons. Inline buttons are only created for explicit bridge states such
as approvals and voice transcript confirmation.

When an approval action is pending, `yes`, `y`, `ok`, 👍, or a heart approve it; `no`,
`n`, or 👎 reject it. The inline buttons use pending-action callback ids such as
`cc:act_...:approve`; stale or unknown callbacks are rejected. If no approval is pending,
approval-like text is not sent as an approval. Use `/agent yes` to send that text literally.

Agent output is shown through one editable progress message per chat: CliCourier sends
one message when output starts, edits that same message with the latest 60-line rolling
window, and force-edits it one last time to the final 60-line tail when the turn
completes. Structured Codex final answers are sent from `final_message` events.
Reasoning, tool deltas, and status events update progress/debug state but are not exposed
in Telegram as raw reasoning unless `/trace_on` is enabled. Use `/tail`, `/log`, or
`/sendlog` to retrieve recent raw agent events on demand. See
[docs/telegram-message-editing.md](docs/telegram-message-editing.md) for the deterministic
test agent and Telegram Web verification workflow.

The daemon log records progress-message operations without message content, for example
`clicourier agent_output action=progress_send_ok ...`,
`action=progress_edit_ok`, and `action=progress_edit_failed`. Use `clicourier logs` to
inspect whether Telegram edits are succeeding or being rejected by the Bot API.

To pause proactive Telegram output while you are at the machine:

```bash
clicourier mute
clicourier unmute
```

The same toggle is available from Telegram with `/mute`, `/unmute`, `/desktop`,
`/telegram`, `/mute_status`, `/botstatus`, and `/bothelp`. By default this creates a `muted` file in the project
working directory. The initial agent prompt explains this to the coding agent, so you can
ask it to "switch to desktop" or "switch to Telegram" and it can create or delete that
file.

## Development

```bash
uv sync
uv run clicourier run
uv run pytest
```

The integration test uses `tests/fixtures/fake_agent.py` so PTY behavior can be tested
without Codex, Claude, Gemini, or a Telegram bot token.

## Known limitations

- The MVP is still single-user and single active agent session.
- Claude Code structured stream-json (`--print --output-format stream-json`) is fully
  implemented. Gemini uses the PTY/tmux fallback.
- Claude Code requires `--dangerously-skip-permissions` (or `--permission-mode
  bypassPermissions`) in the agent command when running in `--print` mode, because
  interactive permission prompts cannot be answered over a non-interactive stdin.
- PTY/tmux fallback can only expose raw terminal deltas; structured streams are preferred
  whenever an agent supports them because they provide explicit turn completion.
- The Telegram chat UI is optimized for approvals, final answers, files, and screenshots,
  not full terminal operation. See the Mini App console note for the future richer UI.

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
- [docs/mini-app-console.md](docs/mini-app-console.md) sketches a future Telegram Mini App console.
- [ROADMAP.md](ROADMAP.md) tracks planned work beyond the MVP.
