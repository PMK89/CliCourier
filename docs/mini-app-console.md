# Future Mini App Console

This is a design note, not part of the MVP implementation.

CliCourier's MVP should keep Telegram chat as a compact control surface: dashboard,
approvals, final answers, files, screenshots, and voice transcript confirmation. A richer
console can be added later as a Telegram Mini App without bringing live terminal video
streaming into the chat.

## Shape

- `xterm.js` terminal view for users who explicitly want raw interactive console access.
- WebSocket event stream carrying normalized `AgentEvent` objects.
- Approval panel rendering `approval_requested` events with Approve, Reject, Details, and
  Send log controls.
- File browser scoped to `WORKSPACE_ROOT`, using the same sandbox rules as `/ls`,
  `/tree`, `/cat`, and `/sendfile`.
- Screenshot preview panel for latest screenshot/artifact events.

## Server Boundary

The Mini App should subscribe to the same event stream Telegram uses instead of scraping
terminal text. Codex should remain on `codex exec --json`; PTY/tmux should stay fallback
adapters that emit normalized events.

## MVP Non-Goals

- No always-on video or terminal frame streaming in Telegram chat.
- No bypass around pending-action ids for approvals.
- No file access outside the existing workspace sandbox.

