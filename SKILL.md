---
name: codex-tmux-telegram-bridge
description: Build, configure, launch, and operate a Telegram bridge for a Codex process running in tmux. Use when another Codex agent must set up a Telegram bot, identify a Codex tmux pane, configure authorized chat access, start the bridge, verify inbound Telegram-to-pane input, verify outbound Codex rollout JSONL-to-Telegram output, add typing indicators, or recover the bridge without relying on machine-specific paths.
---

# Codex Tmux Telegram Bridge

## Goal

Set up a small bridge that lets an authorized Telegram chat interact with a live Codex process running inside tmux:

- Telegram text messages are pasted into the tmux pane and submitted with Enter.
- Outbound replies come from Codex rollout JSONL, not from raw tmux screen scraping.
- Raw tmux/TUI style output is avoided on the main outbound path.
- Telegram shows a typing indicator while Codex is generating a response.
- The tmux pane stays canonical and reattachable by SSH.
- The bridge is a separate process; it should not own or kill the tmux session.

Use the bundled `scripts/bridge.py` as the implementation unless the user already has an equivalent bridge.

## Architecture

Data flow:

1. User runs a terminal process inside tmux, for example Codex, a shell, or another TUI.
2. Agent identifies the exact tmux target pane, such as `<session>:<window>.<pane>`.
3. User creates a Telegram bot and sends it a first message.
4. Agent records `TELEGRAM_BOT_TOKEN`, `AUTHORIZED_CHAT_ID`, `TMUX_TARGET`, and optionally `CODEX_SESSIONS` in a private env file.
5. Bridge long-polls Telegram `getUpdates`.
6. Authorized Telegram text is injected with:
   - `tmux load-buffer`
   - `tmux paste-buffer`
   - `tmux send-keys Enter`
7. Bridge tails the newest Codex rollout JSONL under `CODEX_SESSIONS`.
8. When rollout events show `task_started`, bridge refreshes Telegram `sendChatAction(action=typing)`.
9. Bridge forwards structured assistant messages and structured errors from the rollout JSONL to Telegram.

The bridge is intentionally simple. It does not require a web server, webhook, public port, database, or daemon manager.

## Output Cleanup

Do not treat raw tmux pane bytes as the Codex transcript. Codex draws status bars, boxes, spinners, cursor controls, and style sequences that are useful on a terminal but noisy in Telegram.

The current bridge avoids that class of bugs by reading Codex rollout JSONL for outbound messages. That JSONL contains structured `response_item` and `event_msg` records, so the bridge can forward assistant text without terminal chrome.

Required outbound behavior:

- Prefer Codex rollout/session JSONL for assistant text.
- Forward only structured assistant text and structured error events.
- Skip tool calls, tool outputs, reasoning records, token-count events, and generic TUI/status noise.
- Do not use `tmux capture-pane` as the primary outbound source for Codex replies.
- Ignore empty output after formatting so spinner or border-only deltas do not generate blank Telegram messages.

If adapting the bridge for a non-Codex TUI and you must use raw tmux output:

- Capture plain text with `tmux capture-pane -p`; do not use `-e` unless you also strip ANSI/SGR escapes.
- If adapting from `tmux pipe-pane` or another raw terminal stream, strip ANSI CSI/OSC/control sequences before sending.
- Drop style-only lines such as box borders, progress bars, separator rules, and other lines made mostly of `┌─┐│╭╮╰╯█░▒▓`-style drawing characters.
- Strip wrapping border characters from content lines, for example convert `│ answer text │` to `answer text`.
- Keep real user-visible text, command output, errors, and assistant prose.

Common bug: an agent forwards every new `capture-pane` line and Telegram receives Codex chrome instead of the answer. For Codex, fix this by switching outbound to rollout JSONL. Only add a tmux-output sanitizer when the target process has no structured output stream.

## Typing Indicator

Telegram does not keep `typing` visible permanently. The bridge must refresh it while a response is pending:

1. Watch the rollout JSONL for `event_msg` records with `payload.type == "task_started"`.
2. On task start, call Telegram `sendChatAction` with `action=typing`.
3. Refresh every `TYPING_INTERVAL` seconds while the response is active.
4. Keep refreshing even after assistant text is sent; Codex may emit partial text before tool calls or additional final text.
5. Stop refreshing only when the whole turn completes: `task_complete`, `turn_aborted`, or structured error/stream error.
6. Never send a visible "typing..." chat message; use `sendChatAction` only.

The bundled script enables this by default with `TYPING_INTERVAL=4.0`.

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
AUTHORIZED_CHAT_ID=<authorized-chat-id>
TMUX_TARGET=<session>:<window>.<pane>
# Optional if Codex uses a non-default sessions directory:
# CODEX_SESSIONS=<path-to-codex-sessions>
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

1. Ask Codex for a short, non-secret response through Telegram.
2. Confirm the Telegram chat shows `typing` while Codex is running.
3. Confirm Telegram receives only the assistant text, not Codex boxes, spinners, status bars, or tool JSON.
4. If no output arrives, confirm the rollout JSONL under `CODEX_SESSIONS` is being updated.

Process check:

```bash
pgrep -af 'scripts/bridge.py|tmux-telegram-bridge'
```

## Runtime Options

Required environment:

- `TELEGRAM_BOT_TOKEN`: Telegram bot token from BotFather.
- `AUTHORIZED_CHAT_ID`: Single authorized chat id. `TELEGRAM_CHAT_ID` is accepted as a compatibility alias by the bundled script.

Optional environment:

- `TMUX_TARGET`: Target pane in tmux target syntax. Default: `codex-guardian:codex`.
- `CODEX_SESSIONS`: Codex sessions directory. Default: `~/.codex/sessions`.
- `TG_TIMEOUT` or `TELEGRAM_TIMEOUT`: Telegram long-poll timeout seconds. Default: `30`.
- `POLL_MS`: rollout JSONL polling interval. Default: `500`.
- `TYPING_INTERVAL` or `TELEGRAM_TYPING_INTERVAL`: seconds between typing refreshes. Default: `4.0`.
- `TELEGRAM_CHUNK_LIMIT`: Telegram message chunk size. Default: `4000`.

## Recovery

Bridge stopped:

```bash
pgrep -af 'scripts/bridge.py'
# Relaunch using the same env file and supervisor method.
```

Telegram reaches bot but not tmux:

- Confirm `AUTHORIZED_CHAT_ID` matches the sender chat.
- Confirm `TMUX_TARGET` still exists.
- Confirm the target pane is a live Codex TUI and accepts pasted text followed by Enter.

Codex output does not reach Telegram:

- Confirm `CODEX_SESSIONS` points at the directory where Codex writes rollout JSONL files.
- Confirm the newest rollout JSONL changes when Codex responds.
- Confirm the bridge logs `tailing <path>` for the expected JSONL file.
- Confirm the response appears as a structured assistant `response_item`, not only as tmux screen text.

Telegram receives Codex boxes, spinners, or styled junk:

- Confirm the implementation is using rollout JSONL for outbound, not `tmux capture-pane`.
- If a local modification added raw tmux outbound, remove it for Codex or add a sanitizer as described in Output Cleanup.

Typing indicator does not show:

- Confirm rollout JSONL emits `task_started` for the Codex run.
- Confirm Telegram `sendChatAction` succeeds for the same `AUTHORIZED_CHAT_ID`.
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
