---
name: codex-telegram-multi-bot-bridge
description: Build, configure, launch, and operate a Telegram multi-bot bridge backed by Codex app-server. Use when a Telegram manager bot should create managed session bots, bind each bot to one Codex thread, stream Codex responses by editing Telegram messages, support Codex slash commands, or run multiple Codex sessions from one Telegram group.
---

# Codex Telegram Multi-Bot Bridge

## Goal

Run a persistent Telegram bridge that creates one Telegram session bot per
Codex app-server thread:

- A manager bot owns `/new` and session setup.
- Each session bot is bound to exactly one Codex thread/session.
- Telegram messages route to Codex through `codex app-server`, not tmux.
- Assistant deltas stream per `agentMessage` item; each Codex message item owns
  its own Telegram message.
- Tokens are stored locally in plaintext JSON.
- Multiple Codex sessions can be active in the same Telegram group.

Use `scripts/bridge.py` as the implementation.

## UX

In a Telegram group, send:

```text
/new
/new Research Codex
```

The manager bot sends a setup card:

```text
New Codex Bot

Name: Research Codex
Username: @research_codex_ab12_bot
Command: /research_codex
Model: default
Effort: default
Fast: off
Approval: Auto Review

Status: draft
```

Setup buttons:

```text
[Name] [Username]
[Model] [Effort] [Fast: Off]
[Approval: Auto Review]
[Create]
[Cancel]
```

`Name` and `Username` use Telegram `ForceReply` in the current setup chat.
In group chats, only the reply to the prompt is accepted as the field value.
Display names must be unique across active session bots and pending drafts
because the bridge derives the group autocomplete command from the display name.
`Model` and `Effort` open an inline choice view instead of cycling silently.
`Fast` toggles directly. `Approval` opens a permissions view with Auto Review,
Ask, and Allow All.

In group setup cards, `Create` is a deep link to the manager bot private chat.
The deep link carries the draft id and the manager sends a final
`https://t.me/newbot/{manager}/{suggested_username}?name={suggested_name}` link
there. This avoids the `request_managed_bot` reply keyboard path, which can
open Telegram's blank Forward flow on some clients.

After the managed bot is created, the manager sends `Open bot` and
`Add to group` links. Telegram does not let a bot add another bot to a group by
API call, so the user confirms the group add through the `startgroup` link. The
link requests `admin=manage_topics` so Telegram can add or promote the bot with
the required topic permission.
When the session bot joins a topics-enabled supergroup and has admin
`can_manage_topics`, it creates a dedicated forum topic and sends the
session-ready message inside that topic. If the group is not a forum or the bot
lacks topic permissions, it sends a permission prompt and does not create a
root-message fallback.

The manager bot must have `can_manage_bots=true`. If it does not, open the
@BotFather Mini App and enable management of other bots for the manager bot.

## Defaults

Create uses:

- `model`: inherit Codex default
- `effort`: inherit Codex default
- `fast`: off; when enabled it maps to app-server `serviceTier=priority`
- `approval`: Auto Review, which maps to `approvalPolicy=on-request` and
  `approvalsReviewer=auto_review`
- `cwd`: omitted

Approval modes:

- Auto Review: `approvalPolicy=on-request`, `approvalsReviewer=auto_review`
- Ask: `approvalPolicy=on-request`, `approvalsReviewer=user`
- Allow All: `approvalPolicy=never`, `approvalsReviewer=user`,
  `sandbox={"danger-full-access":{}}`

`cwd` is intentionally omitted during creation. The app-server chooses the
default working directory and returns the actual `cwd` in `thread/start`.
Users can change it later with the Work Dir button or `/cwd`.

## Architecture

Data flow:

1. The bridge starts `codex app-server --listen stdio://`.
2. The manager bot long-polls Telegram updates.
3. `/new` creates a local draft in `BRIDGE_HOME/state.json`.
4. The setup card edits the draft through inline buttons and `ForceReply`.
5. Managed-bot creation returns a Telegram bot identity.
6. The bridge calls `getManagedBotToken` and stores the token in plaintext.
7. The bridge calls `thread/start` with the selected approval mode and a
   Telegram-specific developer instruction asking Codex to preserve commentary /
   tool / final rollout shape for non-trivial tasks.
8. The bridge calls `thread/name/set` with the bot display name.
9. The new session bot starts long-polling.
10. The bridge configures each session bot's Telegram command scopes: private
    chats expose `/commands`, while group chats expose one unique command
    derived from the display name, such as `/raven` or `/mma2_mak`.
11. When a session bot joins a group, it creates and records a forum topic. If
    it lacks topic permissions, it sends a prompt asking the user to grant
    `can_manage_topics`.
12. Session bot messages call `turn/start` when idle and `turn/steer` while a
    turn is active.
13. When Plan mode is enabled, the next idle `turn/start` includes app-server
    `collaborationMode={mode:"plan", ...}` from the official collaboration-mode
    preset shape. Plan mode must not be simulated by sending a "make a plan"
    user prompt.
14. The bridge sends an animated ellipsis placeholder while waiting for the
    first assistant delta.
15. Non-message work items such as `commandExecution`, `webSearch`,
    `dynamicToolCall`, `mcpToolCall`, and `fileChange` use the default compact
    tool display: one throttled Activity panel per turn. The panel has Details
    and Hide buttons. Details edits that same Activity message into a paginated
    detail view; Summary returns it to compact mode.
16. `item/agentMessage/delta` notifications are keyed by `itemId`; each
    `agentMessage` item updates its own Telegram message with `editMessageText`.
    This matches the Codex JSONL boundary:
    `response_item` where `payload.type == "message"` and
    `payload.role == "assistant"`.
17. Long single Codex messages may be split only to satisfy Telegram length
    limits; do not merge distinct `agentMessage` items into one Telegram
    message.
18. `turn/completed` clears the active turn and flushes the final edits.

## Routing

Private chat with a session bot:

- Any text goes to that bot's Codex thread.
- Slash commands are handled by the bridge before text forwarding.

Group chat with many session bots:

- `@bot message` routes to that bot.
- `/status@bot` and other addressed slash commands route to that bot.
- Messages inside the bot's dedicated forum topic route to that bot.
- `/commands` only works inside that bot's dedicated topic or private chat.
- In the group root, Telegram autocomplete should use the bot-specific command,
  for example `/raven` or `/mma2_mak`, which opens that bot's command card.
- Naked root `/commands` is ignored so one autocomplete choice cannot trigger
  every session bot.
- Unmentioned group text is ignored by session bots.

## Session Commands

Each session bot supports `/commands`, which opens an inline command card.
The card exposes the Codex commands as buttons. Account and Session are separate
views: Account shows the logged-in account, plan, and primary usage limits,
while Session shows thread id/status and the configured model, effort, Fast,
Plan mode, approval, work dir, and active turn. Model and Effort choices come
from app-server `model/list`, not a stale hardcoded list. Goal opens a second
menu with Set Goal, Current, Clear, Pause, Resume, and Cancel. Set Goal sends a
`ForceReply` prompt and applies the user's reply. If a goal already exists, a
new objective shows a replace confirmation before the bridge clears and sets the
goal, matching official Codex behavior. Work Dir opens an inline directory
picker backed by app-server `fs/readDirectory` and `fs/getMetadata`; it also has
a Type Path button for direct path entry and no workspace-specific shortcuts.

Typed commands still supported by the bridge:

- `/status` or `/account`: show real Codex account status, including account
  plan and the primary usage limits from `account/read` and
  `account/rateLimits/read`. Do not show other limit buckets here.
- `/session` or `/session_status`: show session settings such as bot command,
  bound thread id, selected model/effort, plan-mode toggle, work dir, and active
  turn id. `/bridge` and `/bridge_status` are kept as aliases.
- `/interrupt`: call `turn/interrupt` for the active turn and explain that the
  active Codex run was asked to stop.
- `/plan`: switch Plan mode on for future idle turns and send a visible
  confirmation message; `/plan <message>` switches to Plan mode and submits that
  message. `/plan off` and `/plan status` are Telegram conveniences.
- `/goal`: call `thread/goal/get`, `thread/goal/set`, or
  `thread/goal/clear`; `clear`, `pause`, and `resume` follow official goal
  control semantics.
- `/compact`: call `thread/compact/start`; disabled while a task is in
  progress.
- `/review`: call `review/start` for uncommitted changes, or a custom review
  target when inline args are provided; disabled while a task is in progress.
  The response should name the target and review thread so the action is clear.
- `/model`: open the model picker or set a visible app-server model for future
  `turn/start` calls; disabled while a task is in progress.
- `/effort`: open the effort picker or set a reasoning effort supported by the
  current model; disabled while a task is in progress.
- `/fast`: toggle official Fast service tier. It sends `serviceTier="priority"`
  on future `turn/start` calls and sends `serviceTier=null` when off so a sticky
  Fast override is cleared. It does not lower reasoning effort.
- `/cwd`: open the working directory picker; `/cwd /absolute/path` validates and
  sets the cwd used for future turns.

Do not forward these commands to the model as plain text. Slash commands are a
client-side command layer and must be translated into app-server RPCs or local
bridge behavior.

## Launch

Create a manager bot and enable the managed-bot flow for it in Telegram. Then
run:

```bash
export TELEGRAM_MANAGER_BOT_TOKEN='<manager-bot-token>'
python3 scripts/bridge.py
```

Optional hardening:

```bash
export AUTHORIZED_USER_IDS='<telegram-user-id>[,<telegram-user-id>...]'
export AUTHORIZED_CHAT_IDS='<group-chat-id>[,<group-chat-id>...]'
```

Optional app-server override:

```bash
export CODEX_APP_SERVER_COMMAND='codex app-server --listen stdio://'
```

Runtime options:

- `TELEGRAM_MANAGER_BOT_TOKEN`: manager bot token. `TELEGRAM_BOT_TOKEN` is
  accepted as a fallback.
- `BRIDGE_HOME`: state directory. Default: `~/.codex-telegram-bridge`.
- `TELEGRAM_TIMEOUT`: Telegram long-poll timeout. Default: `30`.
- `AUTHORIZED_USER_IDS`: comma-separated allowlist of Telegram user ids.
- `AUTHORIZED_CHAT_IDS`: comma-separated allowlist of chat ids.
- `CODEX_APP_SERVER_COMMAND`: command used to start app-server.

State is stored as plaintext JSON at:

```text
~/.codex-telegram-bridge/state.json
```

Keep this directory private because it contains managed bot tokens.

## Verification

Local checks:

```bash
python3 -m py_compile scripts/bridge.py
python3 scripts/bridge.py --help
```

App-server smoke test:

```bash
python3 - <<'PY'
from scripts.bridge import AppServerClient
client = AppServerClient(["codex", "app-server", "--listen", "stdio://"], lambda msg: None)
try:
    client.start()
    result = client.request("model/list", {"limit": 1}, timeout=30)
    print(len(result.get("data", [])))
finally:
    client.stop()
PY
```

End-to-end Telegram verification:

1. Start the bridge with a manager bot token.
2. Send `/new Research Codex` in the target group.
3. Use `Create`.
4. Open the `Create managed bot` link in the manager private chat and confirm
   creation.
5. Confirm the manager reports `@bot ready`.
6. Use `Add to group` and confirm the session bot joins the target group.
7. Promote the session bot with Manage Topics permission if Telegram did not do
   it through the `Add to group` link.
8. Confirm it creates a dedicated topic.
9. Send a message in that topic and confirm Telegram shows commentary, one
   compact Activity panel, and final messages as separate rollout events.
10. Run `/status@bot` in the group and confirm it returns the bound Codex thread.

## Safety Rules

- Never commit bot tokens or chat ids.
- Never log tokens.
- Keep `BRIDGE_HOME` private.
- Do not expose a webhook or public port unless explicitly requested.
- Do not create a tmux dependency for this bridge.
- Do not use filesystem transcript tailing for routing; the app-server thread
  id is the source of truth.
