# Architecture

CliCourier is split into small modules so most policy can be tested without a live
Telegram bot.

## Runtime Flow

1. `clicourier` loads `Settings` from environment, local config, and `.env`.
2. `TelegramBridgeBot` registers one Telegram update dispatcher and one callback dispatcher.
3. Every update passes through allowlist and chat-type authorization.
4. Slash commands are handled by the bridge. Non-command text is routed to the active agent.
5. `/start_agent` creates an `AgentSession`. Codex uses structured `codex exec --json`
   turns by default; explicit tmux/PTY configuration uses terminal fallback.
6. Agent adapters emit normalized `AgentEvent` objects. Telegram consumes events, updates
   one dashboard message, and sends separate important messages for final answers,
   approvals, errors, screenshots, and artifacts.
7. Inline buttons are backed by `PendingAction` ids. Arbitrary output is not parsed into
   choices.

## Package Layout

```text
src/cli_courier/
  app.py                 composition root
  cli.py                 clicourier command dispatcher
  config.py              pydantic-settings config model
  daemon.py              PID/log helpers for background mode
  setup.py               interactive config initialization helpers
  model_manager.py       faster-whisper model listing and download helpers
  doctor.py              local dependency and config diagnostics
  state.py               one-session runtime state
  telegram_bot/          auth, command parsing, routing, Telegram runtime
  agent/                 adapters, event schema, Codex JSONL parser, tmux/PTY fallback
  filesystem/            workspace sandbox and safe file operations
  screenshots/           newest screenshot artifact lookup
  voice/                 transcriber protocol, faster-whisper, whisper.cpp, and OpenAI backends
  security/              terminal output sanitization
```

## Agent Adapter Boundary

Adapters define:

- stable id and display name;
- default command;
- capability flags for structured streaming, resume, partial final output, approval
  events, file events, and PTY requirements;
- fallback approval prompt regexes;
- approve and reject input strings;
- output normalization.

The MVP ships:

- `codex`: structured JSONL by default, terminal fallback when forced;
- `claude`: terminal fallback adapter prepared for future stream-json support;
- `gemini`: terminal fallback adapter prepared for future stream-json support;
- `generic`: conservative fallback and test adapter.

Adding Claude or Gemini should not change Telegram command handling. A new adapter should
only supply command defaults, capabilities, structured stream parsing when available, and
approval inputs.

## Process Model

`AGENT_TERMINAL_BACKEND=auto` uses structured Codex mode when the adapter supports it.
For Codex this means each Telegram prompt starts `codex exec --json <prompt>`; later turns
use `codex exec resume --last --json <prompt>`. JSONL is parsed line by line and mapped to
`AgentEvent` values such as `final_message`, `tool_started`, `approval_requested`, and
`status`.

Tmux is still the recommended fallback for TUI agents because the same terminal can be
attached locally while the Telegram bridge runs in the background. Telegram input is
delivered with `tmux send-keys`, and output is captured from the pane.

The PTY fallback uses `pexpect.spawn` with:

- no shell interpolation;
- fixed terminal dimensions;
- workspace cwd;
- sanitized environment containing only common shell variables plus
  `AGENT_ENV_ALLOWLIST`.

The bridge never constructs commands from Telegram text. Telegram input is sent as a line
to the already-running configured agent.

## Filesystem Model

Bot file commands use `WORKSPACE_ROOT` as their root. A Telegram path beginning with `/`
means workspace root, not host root. Every path is resolved with realpath semantics and
must remain under `WORKSPACE_ROOT`, which blocks path traversal and symlink escapes.

The bot's file-command cwd is independent from the agent process cwd.

## Event And Output Model

`AgentEvent` is the internal contract between adapters and Telegram. Structured adapters
map native events directly. Terminal fallback maps raw output to `assistant_delta` and
uses the same editable progress-message renderer as structured output.

The bridge does not chunk agent output into a stream of chat messages. It sends one
progress message when output starts, edits that same message with the latest 60-line
rolling window, and force-edits it to the final tail when a structured `final_message`,
`turn_completed`, or session stop arrives. Structured Codex final answers are sent from
`final_message` events, while reasoning/tool/status events are treated as progress/debug
data and are not shown as raw Telegram messages unless `/trace_on` is enabled.

Telegram maintains one editable dashboard/status message per active session. It shows the
agent, state, cwd, current tool or phase, last important event, and a short output tail.
Updates are throttled to avoid flooding the chat and rendered under Telegram's message
limit.

Rolling terminal output uses a separate editable progress message so visible output is
distinct from the dashboard summary without flooding the chat.

Interactive Telegram requests also get a separate `Done.` message after the turn finishes.
This completion ping is deliberately not represented as an edit, so Telegram can raise a
normal notification. The ping is allowed through for the chat that initiated the request
even when the mute file is present.

Approvals and voice transcripts are represented as `PendingAction` records. Button
callbacks use `cc:<action-id>:<choice-id>`, stale callbacks are rejected, and `yes`/`no`
text only routes to approval handling when a matching pending approval exists.

## Background Model

`clicourier start -- <tool>` launches `python -m cli_courier.cli run` in a new session,
writes a PID file under `~/.local/state/clicourier`, and logs stdout/stderr there. The
daemon sets `AUTO_START_AGENT=true`, so the configured or supplied CLI command starts when
Telegram polling begins.

`clicourier run` is an interactive convenience wrapper. In desktop mode it creates the
mute file, starts the bridge daemon with a tmux-backed agent, and attaches to the tmux
session. In Telegram mode it removes the mute file, starts the same background daemon,
and attaches to the tmux-backed agent when an agent command or configured default exists.
Local `clicourier restart` also forces tmux for the restarted agent and attaches from an
interactive terminal. Telegram `/restart` uses detached restart and reports the `tmux
attach` command after asking the restarted CLI process to open a local desktop terminal.

`NOTIFICATION_BLOCK_FILE` is a simple mute toggle. The generated default is `muted`, placed
in the project working directory. When the file exists, proactive agent output and approval
prompts are not sent to Telegram; command replies still work.

## Voice Model

Voice defaults to `WHISPER_BACKEND=local`, implemented with `faster-whisper` on CPU with
`WHISPER_COMPUTE_TYPE=int8`. Telegram OGG/Opus voice audio is converted to 16 kHz mono WAV
with `ffmpeg` before inference. Models download lazily through faster-whisper or explicitly
through `clicourier model download`.

`openai` and `whisper_cpp` remain backend options for future or advanced use.

Voice files are downloaded to a private temp path, size-checked, transcribed, and deleted.
The transcript is stored as pending state and is only sent to the agent after
`/voice_approve` or the nonce-backed inline button.
