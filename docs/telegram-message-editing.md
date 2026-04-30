# Telegram Message Editing

CliCourier displays live command output in one editable Telegram message per running
request. The bot sends the message when the first completed output line arrives, then
edits that same `message_id` as newer output becomes available.

## Expected Behavior

- While running, the message starts with:

```text
Running.
Showing latest 60 lines
```

- On completion, the same message is force-edited to:

```text
Finished.
Showing final 60 lines
```

- The body is always a rolling output window. It does not append forever and should not
create a new Telegram message for each output chunk.
- The final edit is forced even when normal streaming edits were throttled.
- Reasoning, tool-call, and status trace lines stay suppressed unless `/trace_on` is used.

## Rolling Window

The renderer keeps a rolling buffer of completed output lines. The visible body is the
latest 60 lines. Incomplete trailing lines are preserved internally and flushed into the
buffer on completion.

Telegram text is rendered below a conservative 3900-character limit. If 60 lines do not
fit, the renderer drops older lines until the message fits. If one line is still too long,
the line is trimmed from the left so the newest text is preserved.

## Throttling

Active streaming edits are throttled by `OUTPUT_FLUSH_INTERVAL_MS` to avoid Telegram flood
limits. The default is 1000 ms. Duplicate rendered text is skipped so Telegram does not
reject the edit as "message is not modified".

The final render bypasses throttling and edits the existing output message to the final
latest 60 lines.

For requests that originated from Telegram, CliCourier sends a separate `Done.` message
after completion. That message exists to trigger a normal Telegram notification because
edits to an existing progress message may not alert the user. It is still sent for that
active request even when desktop mode has muted proactive background output.

## Unit Tests

Run the renderer and Telegram runtime tests:

```bash
uv run pytest tests/unit/test_output_renderer.py tests/unit/test_auth_router.py -q
```

Run the full suite:

```bash
uv run pytest
```

## Deterministic Manual Agent

For manual verification, run CliCourier with the deterministic test agent:

```bash
DEFAULT_AGENT_ADAPTER=generic \
DEFAULT_AGENT_COMMAND="python scripts/numbered_line_agent.py" \
AGENT_TERMINAL_BACKEND=pty \
AGENT_INITIAL_PROMPT_ENABLED=false \
uv run clicourier run --mode telegram
```

Then send this to the bot:

```text
numbered-lines 150 0.05
```

The final Telegram output message should contain `LINE 091` through `LINE 150` and should
not contain `LINE 001`.

## Telegram Web Verification

Install Playwright Chromium once if needed:

```bash
uv run --with playwright python -m playwright install chromium
```

Run the verifier:

```bash
TELEGRAM_BOT_CHAT_NAME="Your bot name or username" \
CLICOURIER_TEST_COMMAND="numbered-lines 150 0.05" \
PLAYWRIGHT_HEADLESS=false \
uv run --with playwright python scripts/verify_telegram_web_editing.py
```

The script uses a persistent browser profile at `.playwright/telegram-profile`, opens
`https://web.telegram.org/`, and pauses so you can log in manually. It does not store or
ask for Telegram credentials. After login, it searches for the bot chat or lets you open
it manually, sends the test command, observes the output message, and writes:

```text
tmp/telegram_web_editing_report.json
```

The report checks that the visible output text changes over time, stays under Telegram's
message limit, does not grow beyond 60 `LINE nnn` entries, and finishes with lines 091
through 150.

## Telegram Constraints

- Telegram bot messages have a hard 4096-character limit.
- Telegram may reject rapid edits with retry/flood-limit errors.
- Telegram rejects duplicate edits with "message is not modified"; CliCourier treats that
  as harmless.
- Plain text is used for output messages, with no Markdown or HTML parse mode, to avoid
  escaping failures in command logs.
