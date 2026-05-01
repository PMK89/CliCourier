<p align="center">
  <img src="CliCourierLogo.png" alt="CliCourier - vibe code everywhere" width="560">
</p>

<p align="center">
  <a href="#installation"><img alt="Install with uv" src="https://img.shields.io/badge/install-uv%20tool-5F45E4?style=flat-square"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-2B6CF1?style=flat-square">
  <img alt="Local-first Telegram bridge" src="https://img.shields.io/badge/local--first-Telegram-1789F9?style=flat-square">
</p>

# CliCourier

CliCourier is a local-first Telegram bridge for controlling a trusted CLI agent from a
private Telegram chat. It starts your configured coding tool on your workstation, forwards
requests from Telegram, and sends back useful output, approvals, files, screenshots, and
voice transcripts without exposing your whole machine.

CliCourier is currently an early beta for a trusted single operator and one active agent
session at a time. It is built for a local Linux or WSL workstation where Telegram is the
remote control surface, while the actual work still happens on your machine.

## Features

- Allowlisted Telegram control for one trusted user.
- Configured CLI agent only; Telegram messages are never turned into arbitrary shell commands.
- Codex, Claude Code, Gemini CLI, and generic adapter support.
- Structured JSONL sessions for Codex, Claude Code, and Gemini CLI, with tmux/PTY fallback for TUI-style tools.
- Editable Telegram progress output with a rolling 60-line window, silent live updates, and one completion notification when the turn is done.
- Approvals through inline buttons, `/approve`, `/reject`, short replies, or reactions.
- Workspace-scoped `/ls`, `/tree`, `/cd`, `/cat`, and `/sendfile`.
- Sensitive file blocking for env files, private keys, cloud credentials, and token-like filenames.
- Screenshot artifact lookup and sending from a configured directory.
- Local `faster-whisper` voice transcription with transcript confirmation before anything reaches the agent.
- Optional OpenAI voice transcription for users who prefer an API backend.
- Background daemon controls, restart/resume commands, and desktop/Telegram mute modes through `clicourier`.

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

## Quick Start

1. Create a Telegram bot with BotFather and copy the token.
2. Find your numeric Telegram user id.
3. Run `clicourier init` and paste those values.
4. `cd` into the project you want the agent to work on.
5. Start the bridge with `clicourier run -- codex`.
6. Open the bot chat in Telegram and send a request.

## Telegram Setup

Create a bot token:

1. Open Telegram and start a chat with `@BotFather`.
2. Send `/newbot`.
3. Pick a display name, for example `CliCourier`.
4. Pick a username ending in `bot`, for example `my_clicourier_bot`.
5. Copy the token BotFather returns. It looks like `123456789:AA...`.
6. Open your new bot chat and send `/start` once. Telegram bots cannot message you
   until you have started the private chat.

Find your numeric Telegram user id:

1. Open Telegram and start a chat with `@userinfobot`.
2. Send `/start`.
3. Copy the `Id` value it returns, for example `123456789`.
4. Use that value for `ALLOWED_TELEGRAM_USER_IDS`.

Use the BotFather token as `TELEGRAM_BOT_TOKEN`. Use your numeric id as
`ALLOWED_TELEGRAM_USER_IDS`. If you want CliCourier to send proactive startup messages,
use the same numeric id as `DEFAULT_TELEGRAM_CHAT_ID`.

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

`AGENT_ENV_ALLOWLIST` is only needed for extra environment variables the child CLI agent
must see. Provider API keys are not forwarded by default because they can override local
CLI login credentials; add a key here only if you intentionally use API-key auth.

`DEFAULT_TELEGRAM_CHAT_ID` is only for proactive background output, such as auto-start
messages before you send a command. The bot can only message a private chat after you have
opened the bot in Telegram and sent `/start`; if the chat is not reachable, CliCourier
logs that and keeps polling.

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

`clicourier run` defaults to Telegram mode: it removes the mute file, starts the
bridge daemon in the background, auto-starts the configured agent in tmux, and attaches
to that terminal in the same shell. The console stays visible and interactive while
output is forwarded to Telegram, and the tmux session keeps running if the terminal or
SSH connection closes. Desktop mode mutes proactive Telegram output. Detached mode is
the VPS/SSH path: it starts the same tmux-backed daemon but does not attach.

```bash
clicourier run --mode telegram -- codex
clicourier run --mode telegram
clicourier run --mode detached -- codex
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

Use desktop mode when you are at the machine and want Telegram to stay quiet. Use Telegram
mode when you want remote progress, approvals, files, screenshots, and the final `Done.`
notification in chat.

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

**Gemini CLI** (`DEFAULT_AGENT_ADAPTER=gemini`): starts structured turns with
`gemini --output-format stream-json --yolo --skip-trust --prompt <prompt>`. Follow-up
turns add `--resume latest` unless your command already provides a resume option.

Final answers, tool events, and status updates arrive as JSONL events for Codex, Claude
Code, and Gemini CLI. `clicourier restart` and Telegram `/restart` resume the most recent
session by default; pass `--no-resume` for a fresh session. Local `clicourier restart`
starts the agent in a fresh tmux session and attaches when run from an interactive
terminal; use `--detach` to restart without attaching. Telegram `/restart` preserves the
active agent command, resumes that tool's latest session, and replies with the manual
`tmux attach` command. Telegram `/resume` restarts the configured agent directly in
resume mode.
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

When an approval action is pending, `yes`, `y`, `ok`, thumbs-up, or a heart approve it;
`no`, `n`, or thumbs-down reject it. The inline buttons use pending-action callback ids such as
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

For Telegram-originated requests, CliCourier also sends a short `Done.` message after the
turn completes. Structured adapters use their native completion events; terminal/tmux
adapters use `FINAL_OUTPUT_IDLE_MS` once the output is idle and no approval is pending.
This message is intentionally separate from the edited progress message so Telegram can
raise one normal completion notification, even when desktop mode has muted proactive
background output. Live progress and dashboard messages are sent silently, while approval
and choice prompts still notify because they require user action.

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

## Beta Notes

- CliCourier is built for one trusted operator and one active agent session at a time.
- Structured streams are preferred because they give reliable turn completion, final
  answers, and approval events. PTY/tmux fallback is still available for terminal-first
  tools, but it can only observe raw terminal output.
- Claude Code requires `--dangerously-skip-permissions` or
  `--permission-mode bypassPermissions` in the agent command when running in `--print`
  mode, because interactive permission prompts cannot be answered over non-interactive
  stdin.
- Telegram chat is optimized for remote control: send a request, approve actions, receive
  files/screenshots, and get the final answer. For full terminal work, use the attached
  local tmux session.

## WSL

Windows support means WSL. Install and run CliCourier inside a Linux distribution, use
Linux paths such as `/home/you/project`, and run Linux CLI tools from inside WSL. Native
Windows terminals and PowerShell are not supported yet.

## Security

CliCourier controls a real local CLI process from Telegram. Keep the Telegram allowlist
narrow, do not run as root, keep `WORKSPACE_ROOT` scoped to the project you want to expose,
and leave sensitive file sending disabled by default.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) explains the module boundaries and data flow.
- [COMMANDS.md](COMMANDS.md) lists the Telegram command surface.
- [SECURITY.md](SECURITY.md) documents the threat model and local hardening notes.
- [docs/brand.md](docs/brand.md) documents the logo assets and extracted purple-blue palette.
- [docs/telegram-control-surface.md](docs/telegram-control-surface.md) explains how Telegram is used as the remote control UI.
- [docs/telegram-message-editing.md](docs/telegram-message-editing.md) documents progress and completion notification behavior.
- [STATUS.md](STATUS.md) summarizes the current early-beta scope.
