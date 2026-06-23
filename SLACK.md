# Slack Codex Bridge

This is the Slack target for the Codex bridge. It uses one Slack app/bot and
binds each Codex session to one Slack DM thread.

## UX

- `/codex new` opens a setup modal.
- `new Research` in the app DM quick-creates a session with defaults.
- The bridge posts one root DM message for the session.
- Replies inside that Slack thread are forwarded to the bound Codex thread.
- Codex `agentMessage` items stream back as separate Slack thread replies.
- `/commands` inside a session thread posts a command card.

## Credentials

Required:

- `SLACK_BOT_TOKEN`: Slack bot token, starts with `xoxb-`.
- `SLACK_APP_TOKEN`: Slack app-level Socket Mode token, starts with `xapp-`.

Optional:

- `SLACK_AUTHORIZED_USER_IDS`: comma-separated Slack user IDs allowed to use the
  bridge, for example `U0123,U0456`.
- `SLACK_AUTHORIZED_TEAM_IDS`: comma-separated Slack workspace/team IDs allowed
  to use the bridge.
- `SLACK_BRIDGE_HOME`: state directory. Defaults to `~/.codex-slack-bridge`.
- `CODEX_APP_SERVER_COMMAND`: defaults to `codex app-server --listen stdio://`.

The bridge does not need a Slack signing secret when using Socket Mode.

## Slack App Setup

Create a Slack app from this manifest, or configure the same settings manually:

```yaml
display_information:
  name: Codex Bridge
features:
  app_home:
    home_tab_enabled: true
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  bot_user:
    display_name: Codex Bridge
    always_online: false
  slash_commands:
    - command: /codex
      description: Manage Codex sessions
      usage_hint: new | sessions | help
      should_escape: false
      url: https://example.com/slack/commands
oauth_config:
  scopes:
    bot:
      - chat:write
      - commands
      - im:history
      - im:read
      - im:write
      - users:read
settings:
  event_subscriptions:
    bot_events:
      - app_home_opened
      - message.im
  interactivity:
    is_enabled: true
    request_url: https://example.com/slack/interactivity
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

With Socket Mode enabled, the HTTPS URLs above are placeholders for Slack app
configuration; this bridge receives events and interactions over the Socket Mode
WebSocket.

After creating the app:

1. Install the app to the workspace.
2. Copy the Bot User OAuth Token into `SLACK_BOT_TOKEN`.
3. Create an app-level token with the `connections:write` scope.
4. Copy that app-level token into `SLACK_APP_TOKEN`.

## Run

```bash
export SLACK_BOT_TOKEN='xoxb-...'
export SLACK_APP_TOKEN='xapp-...'
export SLACK_AUTHORIZED_USER_IDS='U...'
python3 scripts/slack_bridge.py
```

The state file is plaintext JSON under `SLACK_BRIDGE_HOME`.

## Current Scope

Implemented in `scripts/slack_bridge.py`:

- Socket Mode event loop without third-party Python dependencies.
- App Home session list.
- `/codex new` setup modal.
- DM `new <name>` quick create.
- Slack DM thread to Codex thread binding.
- `turn/start` when idle and `turn/steer` while a turn is active.
- Per-`agentMessage` Slack replies with in-place updates.
- Compact Activity message with Details/Summary buttons.
- Session commands: account, session, interrupt, plan, goal, compact, review,
  model, effort, fast, cwd, and mcp.

Not yet migrated:

- Rich cwd picker.
- CLI takeover transcript UX.
- Native Slack text streaming APIs.
