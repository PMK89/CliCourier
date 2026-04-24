# Commands

All bridge control uses Telegram slash commands. All other allowlisted text is sent to the
active agent, except approval-like words when no approval is pending.

## Agent

| Command | Description |
| --- | --- |
| `/status` | Show bridge cwd, agent status, adapter, command, and pending approval state. |
| `/start_agent` | Start the configured adapter and `DEFAULT_AGENT_COMMAND`. |
| `/stop_agent` | Stop the active agent process. |
| `/restart_agent` | Stop and start the configured agent. |
| `/agent <text>` | Send text to the active agent even if it looks like an approval. |
| `/agents` | List available adapter ids. |
| `/mute` | Suppress proactive agent output. |
| `/unmute` | Resume proactive agent output. |
| `/mute_status` | Show whether proactive output is muted. |
| `/help` | Show command help. |

## Approvals

| Command | Description |
| --- | --- |
| `/approve` | Send the adapter's approve input to the active agent. |
| `/reject` | Send the adapter's reject input to the active agent. |

When a prompt is detected, CliCourier also sends inline `Approve` and `Reject` buttons with
a nonce. Short text replies such as `yes`, `ok`, or `no` are only accepted when an approval
is pending.

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

For file commands, `/` means `WORKSPACE_ROOT`, not host `/`.

## Voice

| Command | Description |
| --- | --- |
| `/voice_approve` | Send the pending voice transcript to the agent. |
| `/voice_reject` | Discard the pending voice transcript. |
| `/voice_edit <text>` | Replace the pending voice transcript. |

Voice messages are ignored unless transcription is configured. A transcript is never sent
to the agent without confirmation.

## Local CLI

| Command | Description |
| --- | --- |
| `clicourier setup` | Prompt for Telegram token, user id, workspace, CLI command, voice backend, and write local config. |
| `clicourier setup-whisper` | Clone/build `whisper.cpp`, download a ggml model, and configure local transcription. |
| `clicourier run -- <tool>` | Run the bridge in the foreground and auto-start the CLI tool. |
| `clicourier start -- <tool>` | Run the bridge in the background and auto-start the CLI tool. |
| `clicourier stop` | Stop the background bridge. |
| `clicourier restart -- <tool>` | Restart the background bridge. |
| `clicourier status` | Show daemon pid/log path and mute state. |
| `clicourier mute` | Create the mute block file. |
| `clicourier unmute` | Remove the mute block file. |
| `clicourier logs` | Print the daemon log tail. |
