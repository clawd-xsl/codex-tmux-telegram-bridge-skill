---
name: codex-tmux-telegram-bridge
description: Build, configure, launch, and operate a Telegram-to-tmux bridge for a Codex or terminal process pane. Use when another Codex agent must set up a Telegram bot, identify a tmux pane, configure authorized chat access, start the bridge, verify inbound Telegram-to-pane input and outbound pane-to-Telegram output, or recover the bridge without relying on machine-specific paths.
---

# Codex Tmux Telegram Bridge

## Goal

Set up a small bridge that lets an authorized Telegram chat interact with a live tmux pane:

- Telegram text messages are pasted into the tmux pane and submitted with Enter.
- New tmux pane output is captured and sent back to Telegram.
- Raw tmux/TUI style output is cleaned before it is sent to Telegram.
- Telegram shows a typing indicator while the pane is working on a response.
- The tmux pane stays canonical and reattachable by SSH.
- The bridge is a separate process; it should not own or kill the tmux session.

Use the bundled `scripts/bridge.py` as the implementation unless the user already has an equivalent bridge.

## Architecture

Data flow:

1. User runs a terminal process inside tmux, for example Codex, a shell, or another TUI.
2. Agent identifies the exact tmux target pane, such as `<session>:<window>.<pane>`.
3. User creates a Telegram bot and sends it a first message.
4. Agent records `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `TMUX_TARGET` in a private env file.
5. Bridge long-polls Telegram `getUpdates`.
6. Authorized Telegram text is injected with:
   - `tmux load-buffer`
   - `tmux paste-buffer`
   - `tmux send-keys Enter`
7. Bridge starts Telegram `sendChatAction(action=typing)` refreshes while waiting for output.
8. Bridge polls `tmux capture-pane` and sends newly observed pane text back to Telegram after sanitizing tmux/TUI style output.

The bridge is intentionally simple. It does not require a web server, webhook, public port, database, or daemon manager.

## Output Cleanup

Do not treat raw tmux pane bytes as a clean agent transcript. Codex and many TUIs draw status bars, boxes, spinners, cursor controls, and style sequences that are useful on a terminal but noisy in Telegram.

Required cleanup behavior for outbound pane-to-Telegram output:

- Capture plain text with `tmux capture-pane -p`; do not use `-e` unless you also strip ANSI/SGR escapes.
- If adapting from `tmux pipe-pane` or another raw terminal stream, strip ANSI CSI/OSC/control sequences before sending.
- Drop style-only lines such as box borders, progress bars, separator rules, and other lines made mostly of `┌─┐│╭╮╰╯█░▒▓`-style drawing characters.
- Strip wrapping border characters from content lines, for example convert `│ answer text │` to `answer text`.
- Ignore empty output after sanitization so spinner or border-only deltas do not stop typing indicators or generate blank Telegram messages.
- Keep real user-visible text, command output, errors, and assistant prose.

For Codex specifically, prefer structured rollout/session JSONL for assistant text when that surface is available. If the bridge is intentionally generic and only reads tmux, keep a sanitizer equivalent to `scripts/bridge.py`'s `sanitize_tmux_line()` and `format_new_output()` in the outbound path.

Common bug: an agent forwards every new `capture-pane` line and Telegram receives Codex chrome instead of the answer. Fix it at the outbound formatter, not by changing the tmux target or disabling the TUI.

## Typing Indicator

Telegram does not keep `typing` visible permanently. The bridge must refresh it while a response is pending:

1. After an authorized Telegram message is successfully pasted into tmux, call Telegram `sendChatAction` with `action=typing`.
2. Refresh every `TELEGRAM_TYPING_INTERVAL` seconds while waiting for outbound text.
3. Stop refreshing as soon as a real sanitized outbound message is sent.
4. Also stop on timeout so a stuck pane does not show typing forever.
5. Never send a visible "typing..." chat message; use `sendChatAction` only.

The bundled script enables this by default with `TELEGRAM_TYPING_ENABLED=1`, `TELEGRAM_TYPING_INTERVAL=4.0`, and `TELEGRAM_TYPING_TTL=180.0`.

## Prerequisites

Confirm tools:

```bash
command -v tmux
command -v python3
```

Confirm a process is running in tmux:

```bash
tmux list-panes -a -F '#{session_name}:#{window_name}.#{pane_index} #{pane_current_command} #{pane_pid}'
```

If no tmux pane exists, create one and start the target process:

```bash
tmux new-session -s <session-name>
# Start the desired process inside tmux, then detach with Ctrl-b d.
```

Do not invent a target pane. Verify it with `tmux capture-pane`:

```bash
tmux capture-pane -t '<session>:<window>.<pane>' -p -S -40
```

## Configure Telegram

Ask the user to create a bot:

1. In Telegram, open `@BotFather`.
2. Send `/newbot`.
3. Pick a display name and username.
4. Store the bot token privately as `TELEGRAM_BOT_TOKEN`.
5. Send any message to the new bot from the Telegram account or group that should control the tmux pane.

Get the authorized chat id locally. Do not paste the token into chat.

```bash
export TELEGRAM_BOT_TOKEN='<token-from-botfather>'
python3 scripts/bridge.py --print-chat-ids
```

Choose the intended chat id from the output and write a private env file:

```bash
install -m 700 -d <private-config-dir>
cat > <private-config-dir>/tmux-telegram-bridge.env <<'EOF'
TELEGRAM_BOT_TOKEN=<token-from-botfather>
TELEGRAM_CHAT_ID=<authorized-chat-id>
TMUX_TARGET=<session>:<window>.<pane>
EOF
chmod 600 <private-config-dir>/tmux-telegram-bridge.env
```

Never commit this env file.

## Launch Bridge

Start it in the foreground first:

```bash
set -a
. <private-config-dir>/tmux-telegram-bridge.env
set +a
python3 scripts/bridge.py
```

If foreground mode works, start it under tmux or another supervisor.

Example tmux-owned launch:

```bash
tmux new-session -d -s tmux-telegram-bridge \
  "set -a; . <private-config-dir>/tmux-telegram-bridge.env; set +a; python3 <repo-dir>/scripts/bridge.py"
```

Example background launch:

```bash
set -a
. <private-config-dir>/tmux-telegram-bridge.env
set +a
nohup python3 <repo-dir>/scripts/bridge.py >> <log-dir>/tmux-telegram-bridge.log 2>&1 &
```

## Verify

Inbound test:

1. Send a short Telegram message to the bot.
2. Watch the tmux pane:

```bash
tmux capture-pane -t "$TMUX_TARGET" -p -S -20
```

Expected result: the message appears in the pane and is submitted.

Outbound test:

1. Make the tmux process print a short, non-secret response.
2. Confirm the Telegram chat receives that output.

Process check:

```bash
pgrep -af 'scripts/bridge.py|tmux-telegram-bridge'
```

## Runtime Options

Required environment:

- `TELEGRAM_BOT_TOKEN`: Telegram bot token from BotFather.
- `TELEGRAM_CHAT_ID`: Single authorized chat id.
- `TMUX_TARGET`: Target pane in tmux target syntax.

Optional environment:

- `TELEGRAM_TIMEOUT`: Long-poll timeout seconds. Default: `30`.
- `POLL_MS`: tmux capture polling interval. Default: `1000`.
- `TMUX_HISTORY_LINES`: number of pane lines to compare. Default: `120`.
- `SEND_ENTER`: set `0` to paste without pressing Enter. Default: `1`.
- `OUTBOUND_ENABLED`: set `0` to disable tmux-pane output forwarding. Default: `1`.
- `TELEGRAM_CHUNK_LIMIT`: Telegram message chunk size. Default: `3800`.
- `STRIP_TMUX_STYLES`: set `0` to disable ANSI/control/TUI-style cleanup. Default: `1`.
- `TELEGRAM_TYPING_ENABLED`: set `0` to disable typing indicators. Default: `1`.
- `TELEGRAM_TYPING_INTERVAL`: seconds between typing refreshes. Default: `4.0`.
- `TELEGRAM_TYPING_TTL`: max seconds to keep refreshing after one inbound message without real output. Default: `180.0`.

## Recovery

Bridge stopped:

```bash
pgrep -af 'scripts/bridge.py'
# Relaunch using the same env file and supervisor method.
```

Telegram reaches bot but not tmux:

- Confirm `TELEGRAM_CHAT_ID` matches the sender chat.
- Confirm `TMUX_TARGET` still exists.
- Confirm the target pane is not at a shell prompt that needs a different submission key.
- Set `SEND_ENTER=0` if the target process should receive pasted text without automatic submit.

Tmux output does not reach Telegram:

- Confirm `OUTBOUND_ENABLED` is not `0`.
- Confirm the pane output actually changes in `tmux capture-pane`.
- Increase `TMUX_HISTORY_LINES` if output scrolls too fast.
- Reduce `POLL_MS` only if the host can tolerate more polling.

Telegram receives Codex boxes, spinners, or styled junk:

- Confirm `STRIP_TMUX_STYLES` is not `0`.
- Confirm the code uses `tmux capture-pane -p`, not `tmux capture-pane -e`.
- Add or tighten the outbound sanitizer instead of forwarding raw terminal output.
- If Codex rollout/session JSONL is available, use that structured assistant stream for outbound text.

Typing indicator does not show:

- Confirm the bridge receives the authorized Telegram message and successfully pastes it into tmux.
- Confirm `TELEGRAM_TYPING_ENABLED` is not `0`.
- Confirm Telegram `sendChatAction` succeeds for the same `TELEGRAM_CHAT_ID`.
- Remember the indicator disappears after a few seconds unless refreshed.

Duplicate Telegram output:

- Ensure only one bridge process is running for the same bot and pane.
- Restart stale bridge processes after changing env.

## Safety Rules

- Never commit bot tokens or chat ids.
- Never print the bot token in logs, chat replies, issue comments, or public repos.
- Treat Telegram as part of the transcript path; do not send secrets through the bridge.
- Do not expose the bridge through webhooks unless the user explicitly asks for a webhook architecture.
- Do not kill tmux sessions as bridge cleanup.
- Do not restart unrelated gateways or application services to recover this bridge.
- Prefer one authorized chat id and one bridge process per target pane.
