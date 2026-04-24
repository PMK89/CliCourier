# Security

CliCourier is a private local bridge, not a hosted multi-tenant service. The default
posture is conservative because the bot can control a real local CLI agent.

## Threat Model

### Unauthorized Telegram Users

Every update is checked against `ALLOWED_TELEGRAM_USER_IDS`. Group chats are blocked unless
`ALLOW_GROUP_CHATS=true`. The default unauthorized behavior is silence; set
`UNAUTHORIZED_REPLY_MODE=generic` if you prefer a generic refusal.

### Leaked Bot Token

The allowlist is a second gate if the bot token leaks. Rotate leaked tokens with BotFather.
Do not commit `.env`, logs, screenshots, or chat exports containing the token.

### File Exfiltration

All file commands are workspace-scoped. The resolver blocks:

- `..` traversal outside the workspace;
- symlinks that resolve outside the workspace;
- sensitive env, key, credential, token, and password-like files by default;
- oversized `/cat`, `/sendfile`, and `/screenshot` responses.

`ALLOW_SENSITIVE_FILE_SEND=true` only affects `/sendfile`; `/cat` still refuses sensitive
files.

### Prompt Injection From Agent Output

Agent output is displayed to Telegram but never interpreted as a bot command. Approval
detection only creates pending approval state; it does not approve automatically. The user
must approve or reject through a command or a nonce-backed inline button.

`AGENT_OUTPUT_MODE=final` avoids streaming raw intermediate PTY output and suppresses
common reasoning/tool/status trace lines before Telegram delivery. This is a best-effort
filter because every CLI formats output differently.

### Command Injection

`DEFAULT_AGENT_COMMAND` is parsed once with `shlex` and executed without a shell. Telegram
messages are not appended to shell commands. Filesystem commands use Python path APIs, not
shell commands.

### Bridge Secrets In Agent Environment

The child agent process receives a small sanitized environment. Bridge secrets such as
`TELEGRAM_BOT_TOKEN` and transcription API keys are not forwarded unless explicitly named
in `AGENT_ENV_ALLOWLIST`.

### Local Voice Models

`faster-whisper` model files are downloaded to the local model cache. Keep them under your
user account and avoid running the bridge as root. Telegram voice files are converted in a
temporary directory and deleted after transcription. The optional `whisper_cpp` backend uses
local executables/data; install those only from sources you trust.

### Local Mute Toggle

Anyone who can create `NOTIFICATION_BLOCK_FILE` can suppress proactive Telegram output.
Place it in a user-owned directory such as `~/.local/state/clicourier`.

## Hardening Notes

- Run the bridge as an unprivileged local user.
- Keep `WORKSPACE_ROOT` narrow.
- Keep `ALLOW_GROUP_CHATS=false` unless you have a concrete reason.
- Keep `ALLOW_SCREENSHOT_DIR_OUTSIDE_WORKSPACE=false` unless the screenshot artifact path
  is controlled and trusted.
- Prefer a dedicated Telegram bot token for this bridge.
- Treat approvals as real local workstation actions.
- Use `clicourier mute` when you are actively working at the machine and do not want remote
  output duplicated to Telegram.
