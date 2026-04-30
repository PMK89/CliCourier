# Commands

All bridge control uses Telegram slash commands. All other allowlisted text is sent to the
active agent, except approval-like words when no approval is pending.

## Agent

| Command | Description |
| --- | --- |
| `/botstatus` | Show bridge cwd, agent status, adapter, command, and pending approval state. |
| `/restart` | Restart the CliCourier daemon, auto-start the agent with Codex resume enabled by default, and open a local tmux terminal when possible. Use `/restart --no-resume` for a fresh agent session. |
| `/start_agent` | Start the configured adapter and `DEFAULT_AGENT_COMMAND`. |
| `/stop_agent` | Stop the active agent process. |
| `/restart_agent` | Stop and start the configured agent with Codex resume enabled by default. Use `/restart_agent --no-resume` for a fresh session. |
| `/resume` | Stop any active agent and start the configured Codex agent with `resume --last`. |
| `/resume_agent` | Alias for `/resume`. |
| `/agent <text>` | Send text to the active agent even if it looks like an approval. |
| `/agents` | List available adapter ids. |
| `/stream` | Stream agent output to Telegram as it arrives. |
| `/final` | Return to final/idle output mode. |
| `/trace_on` | Forward reasoning, tool, and status lines instead of filtering them. |
| `/trace_off` | Suppress reasoning, tool, and status lines. |
| `/tail [chars]` | Show recent raw agent event/output text. |
| `/log [chars]` | Alias for `/tail`. |
| `/sendlog` | Send recent raw agent event/output text as a file. |
| `/mute` | Suppress proactive agent output. |
| `/unmute` | Resume proactive agent output. |
| `/desktop` | Same as `/mute`; use this when you are working locally. |
| `/telegram` | Same as `/unmute`; use this when you want proactive Telegram output. |
| `/mute_status` | Show whether proactive output is muted. |
| `/bothelp` | Show command help. |

Unknown slash commands such as `/status`, `/model`, or `/reasoning` are forwarded raw to the
active agent, so CLI-native slash handling still sees the leading `/` even on the first turn.
CliCourier does not parse arbitrary agent output into Telegram choices. Buttons are
only created from explicit bridge states such as approvals and voice transcript
confirmation.

Desktop/mute mode suppresses proactive background output, but a request you send from
Telegram still gets its final output and a separate `Done.` completion notification.

## Approvals

| Command | Description |
| --- | --- |
| `/approve` | Send the adapter's approve input to the active agent. |
| `/reject` | Send the adapter's reject input to the active agent. |

When an approval event is received, CliCourier sends inline `Approve`, `Reject`,
`Details`, and `Send log` buttons backed by a pending-action id. Short text replies such
as `yes`, `ok`, or `no` are only accepted when an approval is pending. Reacting to the
approval-request message with thumbs-up or a heart approves; thumbs-down rejects.

## Files

| Command | Description |
| --- | --- |
| `/pwd` | Show the bot file-command cwd inside `WORKSPACE_ROOT`. |
| `/cd <path>` | Change the bot file-command cwd. |
| `/ls [path]` | List a workspace directory. |
| `/tree [path]` | Show a bounded workspace tree. |
| `/cat <path>` | Return a small non-sensitive text file. |
| `/sendfile <path>` | Send a safe workspace file as a Telegram document. |
| `/screenshot` | Send the newest supported image from `SCREENSHOT_DIR`. |
| `/artifacts` | List recent screenshot artifacts. |

For file commands, `/` means `WORKSPACE_ROOT`, not host `/`.

## Voice

| Command | Description |
| --- | --- |
| `/voice_approve` | Send the pending voice transcript to the agent. |
| `/voice_reject` | Discard the pending voice transcript. |

Voice messages are ignored unless transcription is configured. A transcript is never sent
to the agent without confirmation. The confirmation uses `Send`, `Cancel`, and `Edit`
buttons backed by a pending action. To correct a pending transcript, reply with the edited
text before approving it.

## Local CLI

| Command | Description |
| --- | --- |
| `clicourier init` | Prompt for Telegram token, user id, workspace, CLI command, local Whisper defaults, and write local config. Existing config values are reused as defaults. |
| `clicourier doctor` | Check platform, config, Telegram settings, agent command, ffmpeg, faster-whisper, and model cache status. |
| `clicourier config` | Print config path and a redacted non-secret summary. |
| `clicourier model download` | Download/load the configured faster-whisper model. |
| `clicourier model list` | Show configured model, backend, cache status, and known model names. |
| `clicourier run -- <tool>` | Ask for desktop/telegram mode, start the bridge daemon, auto-start the CLI tool in tmux, and attach locally. |
| `clicourier run --mode telegram` | Enable Telegram forwarding, auto-start the configured agent in tmux, and attach locally. |
| `clicourier start --resume -- <tool>` | Run the bridge in the background and auto-start the CLI tool with Codex resume enabled. |
| `clicourier stop` | Stop the background bridge. |
| `clicourier restart -- <tool>` | Restart the bridge, auto-start the agent in tmux, and attach locally when run from a terminal. Codex resume is enabled by default; use `--no-resume` for a fresh session or `--detach` to skip local attach. |
| `clicourier status` | Show daemon pid/log path and mute state. |
| `clicourier mute` | Create the mute block file. |
| `clicourier unmute` | Remove the mute block file. |
| `clicourier logs` | Print the daemon log tail. |
