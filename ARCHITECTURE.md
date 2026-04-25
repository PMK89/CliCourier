# Architecture

CliCourier is split into small modules so most policy can be tested without a live
Telegram bot.

## Runtime Flow

1. `clicourier` loads `Settings` from environment, local config, and `.env`.
2. `TelegramBridgeBot` registers one Telegram update dispatcher and one callback dispatcher.
3. Every update passes through allowlist and chat-type authorization.
4. Slash commands are handled by the bridge. Non-command text is routed to the active agent.
5. `/start_agent` creates an `AgentSession`, which wraps a configured `PtyAgentProcess`.
6. PTY output is sanitized, stored in a ring buffer, debounced, filtered, chunked, and
   flushed to Telegram as final output by default.
7. Recent output is scanned for adapter-specific approval prompts.

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
  agent/                 adapters, PTY process, sessions, approval detection
  filesystem/            workspace sandbox and safe file operations
  screenshots/           newest screenshot artifact lookup
  voice/                 transcriber protocol, faster-whisper, whisper.cpp, and OpenAI backends
  security/              terminal output sanitization
```

## Agent Adapter Boundary

Adapters define:

- stable id and display name;
- default command;
- approval prompt regexes;
- approve and reject input strings;
- output normalization.

The MVP ships:

- `codex`: tuned for Codex CLI approval prompts;
- `generic`: conservative fallback and test adapter.

Adding Claude or Gemini should not change Telegram command handling. A new adapter should
only supply command defaults, prompt patterns, and approval inputs.

## Process Model

`PtyAgentProcess` uses `pexpect.spawn` with:

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

## Output Model

`AGENT_OUTPUT_MODE=final` is the default. The bridge buffers PTY output and sends it after
`FINAL_OUTPUT_IDLE_MS` of quiet time or `FINAL_OUTPUT_MAX_WAIT_MS`, whichever comes first.
This avoids streaming intermediate reasoning and tool traces. A generic filter removes
common status/tool lines before delivery. Since CLI tools differ, this is best-effort; use
the generic adapter for unknown tools and tune command output in the tool itself when
available.

`AGENT_OUTPUT_MODE=stream` restores chunked streaming behavior.

## Background Model

`clicourier start -- <tool>` launches `python -m cli_courier.cli run` in a new session,
writes a PID file under `~/.local/state/clicourier`, and logs stdout/stderr there. The
daemon sets `AUTO_START_AGENT=true`, so the configured or supplied CLI command starts when
Telegram polling begins.

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
