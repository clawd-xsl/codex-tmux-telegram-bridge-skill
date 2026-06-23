#!/usr/bin/env python3
"""Slack single-bot bridge backed by `codex app-server`.

This bridge uses one Slack app/bot and binds each Codex session to one Slack
DM thread. It intentionally does not try to create one Slack bot per Codex
session.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import random
import re
import shutil
import socket
import ssl
import string
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


DEFAULT_HOME = "~/.codex-slack-bridge"
DEFAULT_MODELS = ["default", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gpt-5.2"]
REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]
EFFORTS = ["default", *REASONING_EFFORTS]
FAST_SERVICE_TIER = "priority"
MODEL_CACHE_TTL_SECONDS = 60.0
SLACK_TEXT_LIMIT = 39000
SLACK_UPDATE_INTERVAL = 1.2
SLACK_PLACEHOLDER_INTERVAL = 2.5
SLACK_PLACEHOLDER_MAX_STEPS = 6
ACTIVITY_UPDATE_INTERVAL = 4.0
ACTIVITY_DETAIL_PAGE_LIMIT = 2800
ACTIVITY_DETAIL_MAX_ITEMS = 40

APPROVAL_MODES = {
    "auto_review": {
        "label": "Auto Review",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "auto_review",
    },
    "ask": {
        "label": "Ask",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
    },
    "allow_all": {
        "label": "Allow All",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": {"danger-full-access": {}},
    },
}

SESSION_DEVELOPER_INSTRUCTIONS = """This Codex thread is displayed through a Slack bridge.
For non-trivial tasks, preserve the normal Codex rollout shape: send concise
commentary updates before meaningful tool batches and before a long final answer.
Do not save all intermediate progress for one large final message. Keep the final
answer focused on conclusions and next steps."""

COMMANDS = [
    ("account", "show account and usage limits"),
    ("session", "show session settings"),
    ("interrupt", "interrupt the active turn"),
    ("plan", "toggle Codex Plan mode"),
    ("goal", "set, view, or clear the goal"),
    ("compact", "compact the thread context"),
    ("review", "review current changes"),
    ("model", "view or set the model"),
    ("effort", "view or set reasoning effort"),
    ("fast", "toggle fast mode"),
    ("cwd", "view or set working directory"),
    ("mcp", "show or reload MCP servers"),
]


def log(message: str) -> None:
    print(f"[slack-codex-bridge] {message}", file=sys.stderr, flush=True)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer")


def now_ms() -> int:
    return int(time.time() * 1000)


def rand_suffix(length: int = 5) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def normalize_display_name(value: str) -> str:
    return " ".join(str(value).split()).strip()[:64]


def display_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "default"
    home = os.path.expanduser("~")
    try:
        text = os.path.abspath(os.path.expanduser(text))
    except Exception:
        return text
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~/" + text[len(home) + 1 :]
    return text


def approval_label(value: str | None) -> str:
    mode = APPROVAL_MODES.get(value or "auto_review") or APPROVAL_MODES["auto_review"]
    return str(mode["label"])


def approval_thread_params(value: str | None) -> dict[str, Any]:
    mode = APPROVAL_MODES.get(value or "auto_review") or APPROVAL_MODES["auto_review"]
    params = {
        "approvalPolicy": mode["approvalPolicy"],
        "approvalsReviewer": mode["approvalsReviewer"],
    }
    sandbox = mode.get("sandbox")
    if sandbox:
        params["sandbox"] = sandbox
    return params


def text_input(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text, "text_elements": []}]


def truncate_middle(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    keep = limit - 1
    head = keep // 2
    tail = keep - head
    return text[:head] + "…" + text[-tail:]


def chunk_text(text: str, limit: int = SLACK_TEXT_LIMIT) -> list[str]:
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = max(
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if split_at < max(1, limit // 2):
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        chunks.append(chunk or remaining[:limit])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or [""]


def mrkdwn_escape(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def compact_label(text: Any, limit: int = 160) -> str:
    return truncate_middle(" ".join(str(text).split()), limit)


def parse_command(text: str) -> tuple[str | None, str]:
    text = text.strip()
    if not text.startswith("/"):
        return None, text
    token, _, args = text.partition(" ")
    command = token[1:].replace("-", "_").lower()
    return command or None, args.strip()


def slack_ts_sort_key(value: str) -> tuple[int, int]:
    left, _, right = str(value).partition(".")
    try:
        return int(left), int(right or "0")
    except ValueError:
        return 0, 0


def session_key(team_id: str, channel_id: str, thread_ts: str) -> str:
    return f"{team_id}:{channel_id}:{thread_ts}"


def make_session_id(team_id: str, user_id: str) -> str:
    digest = hashlib.sha1(f"{team_id}:{user_id}:{time.time()}:{random.random()}".encode()).hexdigest()[:10]
    return f"s_{digest}"


def format_goal_elapsed_seconds(seconds: int) -> str:
    minutes, sec = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def render_goal(goal: Any) -> str:
    if not goal:
        return "No goal is currently set."
    if not isinstance(goal, dict):
        return str(goal)
    objective = goal.get("objective") or "(no objective)"
    status = {
        "active": "active",
        "paused": "paused",
        "blocked": "blocked",
        "usageLimited": "usage limited",
        "budgetLimited": "limited by budget",
        "complete": "complete",
    }.get(str(goal.get("status") or ""), str(goal.get("status") or "unknown"))
    tokens = goal.get("tokensUsed")
    budget = goal.get("tokenBudget")
    seconds = goal.get("timeUsedSeconds")
    lines = [f"Goal: {objective}", f"Status: {status}"]
    if isinstance(seconds, int) and seconds > 0:
        lines.append(f"Time: {format_goal_elapsed_seconds(seconds)}")
    if tokens is not None:
        lines.append(f"Tokens: {tokens}" + (f" / {budget}" if budget else ""))
    return "\n".join(lines)


def append_rate_limit_rows(lines: list[str], snapshot: dict[str, Any]) -> int:
    count = 0
    candidates = [
        ("5-hour", snapshot.get("primary") or snapshot.get("shortWindow") or snapshot.get("fiveHour")),
        ("Weekly", snapshot.get("secondary") or snapshot.get("longWindow") or snapshot.get("weekly")),
    ]
    for label, value in candidates:
        if not isinstance(value, dict):
            continue
        used = value.get("used") or value.get("usage") or value.get("usedPercent")
        limit = value.get("limit") or value.get("quota")
        reset = value.get("resetAt") or value.get("resetsAt")
        if used is None and limit is None and reset is None:
            continue
        bits = []
        if used is not None and limit is not None:
            bits.append(f"{used}/{limit}")
        elif used is not None:
            bits.append(f"{used}")
        if reset:
            bits.append(f"resets {reset}")
        lines.append(f"- {label}: {', '.join(bits) if bits else 'available'}")
        count += 1
    return count


class JsonStore:
    def __init__(self, bridge_home: str) -> None:
        self.home = os.path.abspath(os.path.expanduser(bridge_home))
        self.path = os.path.join(self.home, "state.json")
        self.lock = threading.RLock()
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        if not os.path.exists(self.path):
            self._write_unlocked(self.default_state())

    @staticmethod
    def default_state() -> dict[str, Any]:
        return {
            "version": 1,
            "sessions": {},
            "slack_threads": {},
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except FileNotFoundError:
                data = self.default_state()
            if not isinstance(data, dict):
                data = self.default_state()
            data.setdefault("sessions", {})
            data.setdefault("slack_threads", {})
            return data

    def update(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        with self.lock:
            data = self.snapshot()
            result = fn(data)
            self._write_unlocked(data)
            return result

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


class SlackWebApi:
    def __init__(self, bot_token: str, app_token: str, timeout: int) -> None:
        self.bot_token = bot_token
        self.app_token = app_token
        self.timeout = timeout

    def call(self, method: str, payload: dict[str, Any] | None = None, *, token: str | None = None) -> dict[str, Any]:
        body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"https://slack.com/api/{method}",
            data=body,
            headers={
                "Authorization": f"Bearer {token or self.bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After", "1")
                log(f"slack {method} rate limited; retry after {retry_after}s")
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"slack {method} HTTP {exc.code}: {raw[:500]}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"slack {method} returned non-json: {raw[:500]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"slack {method} returned non-object: {data!r}")
        if not data.get("ok"):
            raise RuntimeError(f"slack {method} failed: {data}")
        return data

    def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "text": text[:SLACK_TEXT_LIMIT] or " "}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks is not None:
            payload["blocks"] = blocks
        return self.call("chat.postMessage", payload)

    def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text[:SLACK_TEXT_LIMIT] or " "}
        if blocks is not None:
            payload["blocks"] = blocks
        return self.call("chat.update", payload)

    def open_dm(self, user_id: str) -> str:
        result = self.call("conversations.open", {"users": user_id})
        channel = result.get("channel") if isinstance(result.get("channel"), dict) else {}
        channel_id = channel.get("id")
        if not channel_id:
            raise RuntimeError(f"conversations.open returned no channel id: {result}")
        return str(channel_id)

    def open_view(self, trigger_id: str, view: dict[str, Any]) -> None:
        self.call("views.open", {"trigger_id": trigger_id, "view": view})

    def publish_home(self, user_id: str, view: dict[str, Any]) -> None:
        self.call("views.publish", {"user_id": user_id, "view": view})


class WebSocketClosed(RuntimeError):
    pass


class WebSocketConnection:
    def __init__(self, url: str, timeout: int) -> None:
        self.url = url
        self.timeout = timeout
        self.sock: ssl.SSLSocket | socket.socket | None = None

    def connect(self) -> None:
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme not in {"wss", "ws"}:
            raise RuntimeError(f"unsupported websocket scheme: {parsed.scheme}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw_sock = socket.create_connection((host, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            context = ssl.create_default_context()
            self.sock = context.wrap_socket(raw_sock, server_hostname=host)
        else:
            self.sock = raw_sock
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = "\r\n".join(
            [
                f"GET {path} HTTP/1.1",
                f"Host: {host}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                "User-Agent: codex-slack-bridge/0",
                "",
                "",
            ]
        ).encode("ascii")
        self.sock.sendall(request)
        response = self._read_until(b"\r\n\r\n", 8192)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"websocket upgrade failed: {response[:500]!r}")

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        data = b""
        while marker not in data:
            chunk = self._recv(1)
            data += chunk
            if len(data) > limit:
                raise RuntimeError("websocket HTTP response too large")
        return data

    def _recv(self, size: int) -> bytes:
        if not self.sock:
            raise WebSocketClosed("websocket is not connected")
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise WebSocketClosed("websocket closed")
            data += chunk
        return data

    def recv_text(self) -> str:
        while True:
            first, second = self._recv(2)
            opcode = first & 0x0F
            masked = (second & 0x80) != 0
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv(8))[0]
            mask = self._recv(4) if masked else b""
            payload = self._recv(length) if length else b""
            if masked and payload:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise WebSocketClosed("websocket close frame")
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                return payload.decode("utf-8", errors="replace")

    def send_json(self, payload: dict[str, Any]) -> None:
        self.send_frame(0x1, json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.sock:
            raise WebSocketClosed("websocket is not connected")
        first = 0x80 | opcode
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            header = struct.pack("!BB", first, mask_bit | length)
        elif length < 65536:
            header = struct.pack("!BBH", first, mask_bit | 126, length)
        else:
            header = struct.pack("!BBQ", first, mask_bit | 127, length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def close(self) -> None:
        sock = self.sock
        self.sock = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass


class SocketModeLoop:
    def __init__(self, api: SlackWebApi, handler: Callable[[dict[str, Any]], None], timeout: int) -> None:
        self.api = api
        self.handler = handler
        self.timeout = timeout
        self.ws: WebSocketConnection | None = None

    def run_forever(self) -> None:
        while True:
            try:
                connection = self.api.call("apps.connections.open", {}, token=self.api.app_token)
                url = connection.get("url")
                if not isinstance(url, str) or not url:
                    raise RuntimeError(f"apps.connections.open returned no url: {connection}")
                self.ws = WebSocketConnection(url, self.timeout)
                self.ws.connect()
                log("slack socket mode connected")
                while True:
                    raw = self.ws.recv_text()
                    envelope = json.loads(raw)
                    if not isinstance(envelope, dict):
                        continue
                    envelope_id = envelope.get("envelope_id")
                    if envelope_id:
                        self.ws.send_json({"envelope_id": envelope_id})
                    self.handler(envelope)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log(f"slack socket mode disconnected: {exc}")
                if self.ws:
                    self.ws.close()
                time.sleep(3)


class AppServerClient:
    def __init__(self, command: list[str], on_notification: Callable[[dict[str, Any]], None]) -> None:
        self.command = command
        self.on_notification = on_notification
        self.proc: subprocess.Popen[str] | None = None
        self.lock = threading.RLock()
        self.pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.next_id = 1

    def start(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self.proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            threading.Thread(target=self._read_stdout, daemon=True).start()
            threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "slack-codex-bridge",
                    "title": "Slack Codex Bridge",
                    "version": "0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log(f"app-server non-json stdout: {line[:200]}")
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg:
                q = self.pending.get(int(msg["id"]))
                if q:
                    q.put(msg)
                continue
            try:
                self.on_notification(msg)
            except Exception as exc:
                log(f"notification handler error: {exc}")

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            line = line.strip()
            if line:
                log(f"app-server: {line}")

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 120.0) -> Any:
        self.start_if_needed()
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self.pending[request_id] = q
            msg: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                msg["params"] = params
            assert self.proc is not None and self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        try:
            response = q.get(timeout=timeout)
        finally:
            with self.lock:
                self.pending.pop(request_id, None)
        if "error" in response:
            raise RuntimeError(f"{method} failed: {response['error']}")
        return response.get("result")

    def start_if_needed(self) -> None:
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
        if not running:
            self.start()

    def stop(self) -> None:
        with self.lock:
            proc = self.proc
            self.proc = None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


@dataclass
class SlackAgentMessage:
    item_id: str
    ts: str
    text: str = ""
    last_update_at: float = 0.0
    last_rendered_text: str = ""
    placeholder_text: str = "…"
    placeholder_step: int = 1
    completed: bool = False
    extra_ts: list[str] = field(default_factory=list)


@dataclass
class SlackStatusItem:
    item_id: str
    item_type: str
    label: str
    status: str = "inProgress"
    output_text: str = ""
    completed: bool = False
    failed: bool = False
    detail: str = ""


@dataclass
class SlackTurnOutput:
    session_id: str
    channel_id: str
    thread_ts: str
    turn_id: str
    pending_ts: str
    pending_active: bool = True
    pending_text: str = "…"
    pending_step: int = 1
    pending_last_update_at: float = 0.0
    messages: dict[str, SlackAgentMessage] = field(default_factory=dict)
    message_order: list[str] = field(default_factory=list)
    status_items: dict[str, SlackStatusItem] = field(default_factory=dict)
    status_order: list[str] = field(default_factory=list)
    activity_ts: str | None = None
    activity_last_rendered_text: str = ""
    activity_last_update_at: float = 0.0
    activity_dirty: bool = False
    activity_hidden: bool = False
    activity_view: str = "summary"
    activity_page: int = 0


class SlackCodexBridge:
    def __init__(self, args: argparse.Namespace) -> None:
        bot_token = args.slack_bot_token or os.environ.get("SLACK_BOT_TOKEN", "")
        app_token = args.slack_app_token or os.environ.get("SLACK_APP_TOKEN", "")
        if not bot_token:
            raise SystemExit("SLACK_BOT_TOKEN is required")
        if not app_token:
            raise SystemExit("SLACK_APP_TOKEN is required")
        self.store = JsonStore(args.bridge_home)
        self.api = SlackWebApi(bot_token, app_token, args.slack_timeout)
        self.app = AppServerClient(args.codex_command, self.on_app_notification)
        self.socket = SocketModeLoop(self.api, self.handle_socket_envelope, args.slack_timeout)
        self.authorized_user_ids = self.parse_id_set(args.authorized_user_ids or os.environ.get("SLACK_AUTHORIZED_USER_IDS", ""))
        self.authorized_team_ids = self.parse_id_set(args.authorized_team_ids or os.environ.get("SLACK_AUTHORIZED_TEAM_IDS", ""))
        self.runtime_lock = threading.RLock()
        self.loaded_threads: set[str] = set()
        self.output_by_thread: dict[str, SlackTurnOutput] = {}
        self.activity_details_by_turn: dict[str, tuple[str, str, str, str, list[str]]] = {}
        self.default_model_id: str | None = None
        self.model_cache: tuple[float, list[dict[str, Any]]] | None = None

    @staticmethod
    def parse_id_set(raw: str) -> set[str]:
        return {part.strip() for part in raw.split(",") if part.strip()}

    def run(self) -> None:
        self.app.start()
        self.refresh_session_heads()
        threading.Thread(target=self.placeholder_loop, name="slack-placeholder-loop", daemon=True).start()
        self.socket.run_forever()

    def allowed(self, user_id: str, team_id: str) -> bool:
        if self.authorized_user_ids and user_id not in self.authorized_user_ids:
            return False
        if self.authorized_team_ids and team_id not in self.authorized_team_ids:
            return False
        return True

    def handle_socket_envelope(self, envelope: dict[str, Any]) -> None:
        envelope_type = str(envelope.get("type") or "")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return
        try:
            if envelope_type == "events_api":
                event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
                self.handle_event(payload, event)
            elif envelope_type == "slash_commands":
                self.handle_slash_command(payload)
            elif envelope_type == "interactive":
                self.handle_interactive(payload)
            else:
                log(f"ignored slack socket envelope type={envelope_type or 'unknown'}")
        except Exception as exc:
            log(f"socket payload handler failed: {exc}")

    def handle_event(self, wrapper: dict[str, Any], event: dict[str, Any]) -> None:
        event_type = event.get("type")
        team_id = str(wrapper.get("team_id") or event.get("team") or "")
        if event_type == "app_home_opened":
            user_id = str(event.get("user") or "")
            if user_id and self.allowed(user_id, team_id):
                self.publish_home(user_id)
            return
        if event_type != "message":
            return
        if event.get("subtype") or event.get("bot_id"):
            return
        user_id = str(event.get("user") or "")
        channel_id = str(event.get("channel") or "")
        if not user_id or not channel_id:
            return
        if not self.allowed(user_id, team_id):
            self.api.post_message(channel_id, "This Slack user is not authorized to use this bridge.")
            return
        if str(event.get("channel_type") or "") != "im":
            return
        text = str(event.get("text") or "").strip()
        if not text:
            return
        root_ts = str(event.get("thread_ts") or "")
        if not root_ts:
            self.handle_dm_root_message(team_id, user_id, channel_id, text)
            return
        key = session_key(team_id, channel_id, root_ts)
        session_id = self.store.snapshot().get("slack_threads", {}).get(key)
        if not isinstance(session_id, str):
            self.api.post_message(
                channel_id,
                "This Slack thread is not bound to a Codex session. Use `/codex new` or type `new <name>` in the app DM.",
                thread_ts=root_ts,
            )
            return
        session = self.store.snapshot().get("sessions", {}).get(session_id)
        if not isinstance(session, dict) or session.get("status") == "archived":
            self.api.post_message(channel_id, "This Codex session is archived or missing.", thread_ts=root_ts)
            return
        command, args = parse_command(text)
        if command:
            self.handle_session_command(session_id, session, command, args)
            return
        self.start_or_steer_turn(session_id, session, text)

    def handle_dm_root_message(self, team_id: str, user_id: str, channel_id: str, text: str) -> None:
        lowered = text.lower()
        if lowered == "new" or lowered.startswith("new "):
            name = normalize_display_name(text[3:].strip()) or f"Codex {rand_suffix(4)}"
            self.create_session(team_id, user_id, channel_id, name=name)
            return
        if lowered in {"help", "/help", "commands", "/commands"}:
            self.api.post_message(
                channel_id,
                "Use `/codex new` to create a session, or open this app's Home tab to manage sessions.",
            )
            return
        self.api.post_message(
            channel_id,
            "Start a new Codex session with `/codex new` or `new <name>`. After creation, reply in the session thread.",
        )

    def handle_slash_command(self, payload: dict[str, Any]) -> None:
        user_id = str(payload.get("user_id") or "")
        team_id = str(payload.get("team_id") or "")
        channel_id = str(payload.get("channel_id") or "")
        text = str(payload.get("text") or "").strip()
        if not self.allowed(user_id, team_id):
            if channel_id:
                self.api.post_message(channel_id, "This Slack user is not authorized to use this bridge.")
            return
        command, _, args = text.partition(" ")
        command = command.lower().strip()
        args = args.strip()
        trigger_id = str(payload.get("trigger_id") or "")
        if command in {"", "new"}:
            if trigger_id:
                self.open_new_session_modal(trigger_id, team_id, user_id, channel_id, initial_name=args)
            else:
                self.create_session(team_id, user_id, channel_id, name=args or f"Codex {rand_suffix(4)}")
            return
        if command in {"sessions", "home"}:
            self.publish_home(user_id)
            if channel_id:
                self.api.post_message(channel_id, "Updated the Codex app Home tab.")
            return
        if command == "help":
            if channel_id:
                self.api.post_message(
                    channel_id,
                    "Use `/codex new` to create a Codex session. In a session thread, use `/commands` for session actions.",
                )
            return
        if channel_id:
            self.api.post_message(channel_id, "Usage: `/codex new`, `/codex sessions`, or `/codex help`.")

    def handle_interactive(self, payload: dict[str, Any]) -> None:
        interaction_type = payload.get("type")
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        user_id = str(user.get("id") or "")
        team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
        team_id = str(team.get("id") or "")
        if not self.allowed(user_id, team_id):
            return
        if interaction_type == "view_submission":
            view = payload.get("view") if isinstance(payload.get("view"), dict) else {}
            if view.get("callback_id") == "new_session":
                self.handle_new_session_submission(team_id, user_id, view)
            return
        if interaction_type != "block_actions":
            return
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        if not actions:
            return
        action = actions[0] if isinstance(actions[0], dict) else {}
        action_id = str(action.get("action_id") or "")
        value = str(action.get("value") or "")
        trigger_id = str(payload.get("trigger_id") or "")
        if action_id == "new_session":
            self.open_new_session_modal(trigger_id, team_id, user_id, "", initial_name="")
            return
        if action_id.startswith("archive_session_"):
            session = self.get_session(value)
            if session:
                self.archive_session(value)
                self.publish_home(user_id)
            return
        if action_id.startswith("session_cmd_"):
            session_id, _, command = value.partition(":")
            session = self.get_session(session_id)
            if session:
                self.handle_session_command(session_id, session, command, "")
            return
        if action_id.startswith("activity_"):
            self.handle_activity_action(payload, value)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        session = self.store.snapshot().get("sessions", {}).get(session_id)
        return session if isinstance(session, dict) else None

    def publish_home(self, user_id: str) -> None:
        sessions = [
            (session_id, record)
            for session_id, record in self.store.snapshot().get("sessions", {}).items()
            if isinstance(record, dict) and str(record.get("user_id") or "") == user_id
        ]
        active = [(sid, record) for sid, record in sessions if record.get("status") != "archived"]
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": "Codex Sessions"}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Create a session, then reply inside that Slack thread to talk to Codex."},
            },
            {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "New Session"}, "action_id": "new_session"}]},
            {"type": "divider"},
        ]
        if not active:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No active sessions."}})
        for session_id, record in active[:20]:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "\n".join(
                            [
                                f"*{mrkdwn_escape(record.get('name') or 'Codex')}*",
                                f"Thread: `{record.get('thread_id')}`",
                                f"Model: `{record.get('model') or 'default'}`  Effort: `{record.get('effort') or 'default'}`",
                            ]
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Archive"},
                        "style": "danger",
                        "action_id": f"archive_session_{session_id}",
                        "value": session_id,
                    },
                }
            )
        self.api.publish_home(user_id, {"type": "home", "blocks": blocks})

    def open_new_session_modal(self, trigger_id: str, team_id: str, user_id: str, channel_id: str, *, initial_name: str) -> None:
        if not trigger_id:
            return
        model_options = [{"text": {"type": "plain_text", "text": item}, "value": item} for item in self.available_model_values()[:80]]
        if not model_options:
            model_options = [{"text": {"type": "plain_text", "text": item}, "value": item} for item in DEFAULT_MODELS]
        effort_options = [{"text": {"type": "plain_text", "text": item}, "value": item} for item in EFFORTS]
        approval_options = [
            {"text": {"type": "plain_text", "text": mode["label"]}, "value": value}
            for value, mode in APPROVAL_MODES.items()
        ]
        private_metadata = json.dumps({"team_id": team_id, "user_id": user_id, "channel_id": channel_id}, separators=(",", ":"))
        view = {
            "type": "modal",
            "callback_id": "new_session",
            "private_metadata": private_metadata,
            "title": {"type": "plain_text", "text": "New Codex Session"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "name",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "value",
                        "initial_value": normalize_display_name(initial_name) or f"Codex {rand_suffix(4)}",
                    },
                    "label": {"type": "plain_text", "text": "Name"},
                },
                {
                    "type": "input",
                    "block_id": "model",
                    "element": {"type": "static_select", "action_id": "value", "options": model_options, "initial_option": model_options[0]},
                    "label": {"type": "plain_text", "text": "Model"},
                },
                {
                    "type": "input",
                    "block_id": "effort",
                    "element": {"type": "static_select", "action_id": "value", "options": effort_options, "initial_option": effort_options[0]},
                    "label": {"type": "plain_text", "text": "Effort"},
                },
                {
                    "type": "input",
                    "block_id": "approval",
                    "element": {
                        "type": "static_select",
                        "action_id": "value",
                        "options": approval_options,
                        "initial_option": approval_options[0],
                    },
                    "label": {"type": "plain_text", "text": "Approval"},
                },
                {
                    "type": "input",
                    "block_id": "fast",
                    "optional": True,
                    "element": {
                        "type": "checkboxes",
                        "action_id": "value",
                        "options": [{"text": {"type": "plain_text", "text": "Use Fast service tier"}, "value": "on"}],
                    },
                    "label": {"type": "plain_text", "text": "Fast"},
                },
            ],
        }
        self.api.open_view(trigger_id, view)

    def handle_new_session_submission(self, team_id: str, user_id: str, view: dict[str, Any]) -> None:
        meta = {}
        try:
            meta = json.loads(str(view.get("private_metadata") or "{}"))
        except json.JSONDecodeError:
            pass
        team_id = str(meta.get("team_id") or team_id)
        channel_id = str(meta.get("channel_id") or "")
        values = view.get("state", {}).get("values", {}) if isinstance(view.get("state"), dict) else {}
        name = self.modal_plain_value(values, "name") or f"Codex {rand_suffix(4)}"
        model = self.modal_select_value(values, "model") or "default"
        effort = self.modal_select_value(values, "effort") or "default"
        approval = self.modal_select_value(values, "approval") or "auto_review"
        fast = self.modal_checkbox_has(values, "fast", "on")
        self.create_session(team_id, user_id, channel_id, name=name, model=model, effort=effort, approval=approval, fast=fast)

    @staticmethod
    def modal_plain_value(values: dict[str, Any], block_id: str) -> str:
        block = values.get(block_id) if isinstance(values.get(block_id), dict) else {}
        action = block.get("value") if isinstance(block.get("value"), dict) else {}
        return normalize_display_name(str(action.get("value") or ""))

    @staticmethod
    def modal_select_value(values: dict[str, Any], block_id: str) -> str:
        block = values.get(block_id) if isinstance(values.get(block_id), dict) else {}
        action = block.get("value") if isinstance(block.get("value"), dict) else {}
        selected = action.get("selected_option") if isinstance(action.get("selected_option"), dict) else {}
        return str(selected.get("value") or "")

    @staticmethod
    def modal_checkbox_has(values: dict[str, Any], block_id: str, target: str) -> bool:
        block = values.get(block_id) if isinstance(values.get(block_id), dict) else {}
        action = block.get("value") if isinstance(block.get("value"), dict) else {}
        selected = action.get("selected_options") if isinstance(action.get("selected_options"), list) else []
        return any(isinstance(item, dict) and item.get("value") == target for item in selected)

    def create_session(
        self,
        team_id: str,
        user_id: str,
        channel_id: str,
        *,
        name: str,
        model: str = "default",
        effort: str = "default",
        approval: str = "auto_review",
        fast: bool = False,
    ) -> None:
        name = normalize_display_name(name) or f"Codex {rand_suffix(4)}"
        try:
            thread_info = self.create_codex_thread(name=name, model=model, approval=approval, fast=fast)
            if not channel_id:
                channel_id = self.api.open_dm(user_id)
            session_id = make_session_id(team_id, user_id)
            record = {
                "id": session_id,
                "team_id": team_id,
                "user_id": user_id,
                "channel_id": channel_id,
                "root_ts": "",
                "name": name,
                "thread_id": thread_info["thread_id"],
                "session_id": thread_info.get("session_id"),
                "cwd": thread_info.get("cwd"),
                "model": model,
                "effort": effort,
                "fast": fast,
                "approval": approval,
                "plan_mode": False,
                "active_turn_id": None,
                "status": "active",
                "created_at_ms": now_ms(),
            }
            root = self.api.post_message(
                channel_id,
                self.render_session_head_text(record),
                blocks=self.render_session_head_blocks(record),
            )
            root_ts = str(root.get("ts") or "")
            if not root_ts:
                raise RuntimeError(f"chat.postMessage returned no ts: {root}")
            record["root_ts"] = root_ts

            def mutate(data: dict[str, Any]) -> None:
                data["sessions"][session_id] = record
                data["slack_threads"][session_key(team_id, channel_id, root_ts)] = session_id

            self.store.update(mutate)
            self.api.post_message(
                channel_id,
                "Reply in this thread to talk to Codex. Use `/commands` for session actions.",
                thread_ts=root_ts,
                blocks=self.command_card_blocks(session_id),
            )
            self.publish_home(user_id)
        except Exception as exc:
            log(f"create session failed: {exc}")
            target_channel = channel_id
            if not target_channel:
                try:
                    target_channel = self.api.open_dm(user_id)
                except Exception:
                    target_channel = ""
            if target_channel:
                self.api.post_message(target_channel, f"Codex session creation failed: {exc}")

    def create_codex_thread(self, *, name: str, model: str, approval: str, fast: bool) -> dict[str, Any]:
        params = approval_thread_params(approval)
        params["developerInstructions"] = SESSION_DEVELOPER_INSTRUCTIONS
        if model and model != "default":
            params["model"] = model
        if fast:
            params["serviceTier"] = FAST_SERVICE_TIER
        result = self.app.request("thread/start", params, timeout=120)
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            raise RuntimeError(f"unexpected thread/start response: {result}")
        try:
            self.app.request("thread/name/set", {"threadId": thread["id"], "name": name}, timeout=30)
        except Exception as exc:
            log(f"thread/name/set failed: {exc}")
        return {
            "thread_id": thread["id"],
            "session_id": thread.get("sessionId"),
            "cwd": thread.get("cwd") or result.get("cwd"),
        }

    def render_session_head_text(self, session: dict[str, Any]) -> str:
        state = "running" if session.get("active_turn_id") else "idle"
        lines = [
            f"Codex session: {session.get('name') or 'Codex'}",
            f"State: {state}",
            f"Thread: {session.get('thread_id')}",
            f"Model: {session.get('model') or 'default'}",
            f"Effort: {session.get('effort') or 'default'}",
            f"Fast: {'on' if session.get('fast') else 'off'}",
            f"Plan mode: {'ON' if session.get('plan_mode') else 'off'}",
            f"Approval: {approval_label(session.get('approval'))}",
            f"Codex cwd: {display_path(session.get('cwd') or 'default')}",
            f"Active turn: {session.get('active_turn_id') or 'none'}",
        ]
        pending_cwd = str(session.get("pending_cwd") or "").strip()
        if pending_cwd:
            lines.append(f"Pending cwd: {display_path(pending_cwd)}")
        return "\n".join(lines)

    def render_session_head_blocks(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        state = "running" if session.get("active_turn_id") else "idle"
        return [
            {"type": "header", "text": {"type": "plain_text", "text": str(session.get("name") or "Codex")[:150]}},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(
                        [
                            f"*State:* `{state}`  *Active turn:* `{session.get('active_turn_id') or 'none'}`",
                            f"*Thread:* `{session.get('thread_id')}`",
                            f"*Model:* `{session.get('model') or 'default'}`  *Effort:* `{session.get('effort') or 'default'}`",
                            f"*Fast:* `{'on' if session.get('fast') else 'off'}`  *Plan:* `{'ON' if session.get('plan_mode') else 'off'}`",
                            f"*Approval:* `{approval_label(session.get('approval'))}`",
                            f"*Codex cwd:* `{mrkdwn_escape(display_path(session.get('cwd') or 'default'))}`",
                            *(
                                [f"*Pending cwd:* `{mrkdwn_escape(display_path(session.get('pending_cwd')))}`"]
                                if str(session.get("pending_cwd") or "").strip()
                                else []
                            ),
                            "Reply in this thread to talk to Codex.",
                        ]
                    ),
                },
            },
        ]

    def sync_session_head_message(self, session_id: str) -> None:
        session = self.get_session(session_id)
        if not session or session.get("status") == "archived":
            return
        channel_id = str(session.get("channel_id") or "")
        root_ts = str(session.get("root_ts") or "")
        if not channel_id or not root_ts:
            return
        try:
            self.api.update_message(channel_id, root_ts, self.render_session_head_text(session), blocks=self.render_session_head_blocks(session))
        except Exception as exc:
            log(f"session head update failed for {session_id}: {exc}")

    def refresh_session_heads(self) -> None:
        for session_id, session in self.store.snapshot().get("sessions", {}).items():
            if isinstance(session, dict) and session.get("status") != "archived":
                self.sync_session_head_message(str(session_id))

    def archive_session(self, session_id: str) -> None:
        def mutate(data: dict[str, Any]) -> None:
            record = data["sessions"].get(session_id)
            if not isinstance(record, dict):
                return
            record["status"] = "archived"
            record["archived_at_ms"] = now_ms()
            key = session_key(str(record.get("team_id") or ""), str(record.get("channel_id") or ""), str(record.get("root_ts") or ""))
            data["slack_threads"].pop(key, None)

        self.store.update(mutate)
        session = self.get_session(session_id)
        if session:
            channel_id = str(session.get("channel_id") or "")
            root_ts = str(session.get("root_ts") or "")
            self.api.post_message(
                channel_id,
                f"Archived Slack bridge session `{session.get('name')}`. The Codex thread is kept.",
                thread_ts=root_ts,
            )

    def command_card_blocks(self, session_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Codex actions*"}},
        ]
        groups = [
            [("account", "Account"), ("interrupt", "Interrupt"), ("plan", "Plan")],
            [("goal", "Goal"), ("review", "Review"), ("compact", "Compact")],
            [("model", "Model"), ("effort", "Effort"), ("fast", "Fast")],
            [("cwd", "CWD"), ("mcp", "MCP")],
        ]
        for group in groups:
            rows.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": label},
                            "action_id": f"session_cmd_{command}",
                            "value": f"{session_id}:{command}",
                        }
                        for command, label in group
                    ],
                }
            )
        return rows

    def handle_session_command(self, session_id: str, session: dict[str, Any], command: str, args: str) -> None:
        channel_id = str(session.get("channel_id") or "")
        root_ts = str(session.get("root_ts") or "")
        thread_id = str(session.get("thread_id") or "")
        if not channel_id or not root_ts or not thread_id:
            return
        try:
            if command in {"commands", "help"}:
                self.api.post_message(channel_id, "Codex command card", thread_ts=root_ts, blocks=self.command_card_blocks(session_id))
            elif command in {"status", "account"}:
                self.api.post_message(channel_id, self.render_account_status(), thread_ts=root_ts)
            elif command in {"session", "session_status"}:
                self.sync_session_head_message(session_id)
                self.api.post_message(channel_id, "Session status is shown in the root message.", thread_ts=root_ts)
            elif command == "interrupt":
                turn_id = session.get("active_turn_id")
                if not turn_id:
                    self.api.post_message(channel_id, "No active turn.", thread_ts=root_ts)
                    return
                self.app.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=30)
                self.api.post_message(channel_id, "Interrupt sent. Codex will stop the active turn for this session.", thread_ts=root_ts)
            elif command == "plan":
                self.handle_plan_command(session_id, session, args)
            elif command == "goal":
                self.handle_goal_command(session_id, session, args)
            elif command == "compact":
                if self.task_running(session):
                    self.api.post_message(channel_id, "Compact is disabled while a turn is running.", thread_ts=root_ts)
                    return
                self.app.request("thread/compact/start", {"threadId": thread_id}, timeout=60)
                self.api.post_message(channel_id, "Compaction started.", thread_ts=root_ts)
            elif command == "review":
                if self.task_running(session):
                    self.api.post_message(channel_id, "Review is disabled while a turn is running.", thread_ts=root_ts)
                    return
                target = {"type": "custom", "instructions": args.strip()} if args.strip() else {"type": "uncommittedChanges"}
                result = self.app.request("review/start", {"threadId": thread_id, "target": target}, timeout=60)
                review_thread_id = result.get("reviewThreadId") if isinstance(result, dict) else None
                self.api.post_message(
                    channel_id,
                    "\n".join(
                        [
                            "Review started.",
                            f"Target: {'custom instructions' if args.strip() else 'uncommitted changes'}",
                            f"Review thread: {review_thread_id or thread_id}",
                        ]
                    ),
                    thread_ts=root_ts,
                )
            elif command == "model":
                self.handle_model_command(session_id, session, args)
            elif command == "effort":
                self.handle_effort_command(session_id, session, args)
            elif command == "fast":
                self.handle_fast_command(session_id, session, args)
            elif command == "cwd":
                self.handle_cwd_command(session_id, session, args)
            elif command == "mcp":
                self.api.post_message(channel_id, self.handle_mcp_command(session, args), thread_ts=root_ts)
            else:
                self.api.post_message(channel_id, f"Unknown command: /{command}. Use /commands.", thread_ts=root_ts)
        except Exception as exc:
            self.api.post_message(channel_id, f"Command failed: {exc}", thread_ts=root_ts)

    def handle_plan_command(self, session_id: str, session: dict[str, Any], args: str) -> None:
        channel_id = str(session.get("channel_id") or "")
        root_ts = str(session.get("root_ts") or "")
        value = args.strip()
        lowered = value.lower()
        if lowered in {"status", "?"}:
            self.api.post_message(channel_id, f"Plan mode: {'ON' if session.get('plan_mode') else 'off'}", thread_ts=root_ts)
            return
        if lowered in {"off", "false", "0"}:
            self.update_session(session_id, {"plan_mode": False})
            self.api.post_message(channel_id, "Plan mode off.", thread_ts=root_ts)
            return
        self.update_session(session_id, {"plan_mode": True})
        if not value or lowered in {"on", "true", "1"}:
            self.api.post_message(channel_id, "Plan mode ON. The next idle turn will use Codex Plan mode.", thread_ts=root_ts)
            return
        updated = self.get_session(session_id)
        if updated:
            self.api.post_message(channel_id, "Plan mode ON. Sending your plan request to Codex.", thread_ts=root_ts)
            self.start_or_steer_turn(session_id, updated, value)

    def handle_goal_command(self, session_id: str, session: dict[str, Any], args: str) -> None:
        channel_id = str(session.get("channel_id") or "")
        root_ts = str(session.get("root_ts") or "")
        thread_id = str(session.get("thread_id") or "")
        arg = args.strip()
        if not arg:
            result = self.app.request("thread/goal/get", {"threadId": thread_id}, timeout=30)
            goal = result.get("goal") if isinstance(result, dict) else result
            self.api.post_message(channel_id, render_goal(goal), thread_ts=root_ts)
            return
        if arg.lower() == "clear":
            result = self.app.request("thread/goal/clear", {"threadId": thread_id}, timeout=30)
            cleared = result.get("cleared") if isinstance(result, dict) else None
            self.api.post_message(channel_id, "Goal cleared." if cleared else "No goal to clear.", thread_ts=root_ts)
            return
        if arg.lower() in {"pause", "resume"}:
            status = "paused" if arg.lower() == "pause" else "active"
            result = self.app.request("thread/goal/set", {"threadId": thread_id, "status": status}, timeout=30)
            goal = result.get("goal") if isinstance(result, dict) else result
            self.api.post_message(channel_id, render_goal(goal), thread_ts=root_ts)
            return
        result = self.app.request("thread/goal/set", {"threadId": thread_id, "objective": arg, "status": "active"}, timeout=30)
        goal = result.get("goal") if isinstance(result, dict) else result
        self.api.post_message(channel_id, render_goal(goal) or "Goal set.", thread_ts=root_ts)

    def handle_model_command(self, session_id: str, session: dict[str, Any], args: str) -> None:
        channel_id = str(session.get("channel_id") or "")
        root_ts = str(session.get("root_ts") or "")
        value = args.strip()
        if not value:
            self.api.post_message(
                channel_id,
                f"Current model: {session.get('model') or 'default'}\nUse `/model <id>`. Available: {', '.join(self.available_model_values()[:20])}",
                thread_ts=root_ts,
            )
            return
        if value not in self.available_model_values():
            self.api.post_message(channel_id, f"Unknown model: {value}. Use `/model` to list available options.", thread_ts=root_ts)
            return
        self.update_session(session_id, {"model": value})
        self.api.post_message(channel_id, f"Model: {value}", thread_ts=root_ts)

    def handle_effort_command(self, session_id: str, session: dict[str, Any], args: str) -> None:
        channel_id = str(session.get("channel_id") or "")
        root_ts = str(session.get("root_ts") or "")
        value = args.strip()
        if not value:
            self.api.post_message(
                channel_id,
                f"Current effort: {session.get('effort') or 'default'}\nUse `/effort <value>`. Available: {', '.join(EFFORTS)}",
                thread_ts=root_ts,
            )
            return
        if value not in EFFORTS:
            self.api.post_message(channel_id, f"Unknown effort: {value}. Available: {', '.join(EFFORTS)}", thread_ts=root_ts)
            return
        self.update_session(session_id, {"effort": value})
        self.api.post_message(channel_id, f"Effort: {value}", thread_ts=root_ts)

    def handle_fast_command(self, session_id: str, session: dict[str, Any], args: str) -> None:
        channel_id = str(session.get("channel_id") or "")
        root_ts = str(session.get("root_ts") or "")
        value = args.strip().lower()
        if value in {"", "status"}:
            self.api.post_message(channel_id, f"Fast: {'on' if session.get('fast') else 'off'}", thread_ts=root_ts)
            return
        if value not in {"on", "off"}:
            self.api.post_message(channel_id, "Usage: `/fast [on|off|status]`", thread_ts=root_ts)
            return
        self.update_session(session_id, {"fast": value == "on"})
        self.api.post_message(channel_id, f"Fast: {value}", thread_ts=root_ts)

    def handle_cwd_command(self, session_id: str, session: dict[str, Any], args: str) -> None:
        channel_id = str(session.get("channel_id") or "")
        root_ts = str(session.get("root_ts") or "")
        value = args.strip()
        if not value:
            self.api.post_message(
                channel_id,
                f"Codex cwd: {display_path(session.get('cwd') or 'default')}\nPending cwd: {display_path(session.get('pending_cwd') or '')}\nUse `/cwd /absolute/path` to apply on the next idle turn.",
                thread_ts=root_ts,
            )
            return
        cwd = os.path.abspath(os.path.expanduser(value))
        if not os.path.isdir(cwd):
            self.api.post_message(channel_id, f"Work dir update failed: not a directory: {cwd}", thread_ts=root_ts)
            return
        self.update_session(session_id, {"pending_cwd": cwd})
        self.api.post_message(channel_id, f"Work dir queued for the next idle Codex turn: {display_path(cwd)}", thread_ts=root_ts)

    def handle_mcp_command(self, session: dict[str, Any], args: str) -> str:
        reloaded = False
        if args.strip().lower() == "reload":
            self.ensure_thread_loaded(session)
            self.app.request("config/mcpServer/reload", timeout=120)
            reloaded = True
        status = self.app.request("mcpServerStatus/list", {"detail": "toolsAndAuthOnly"}, timeout=90)
        servers = status.get("data") if isinstance(status, dict) else []
        lines = ["MCP", "", "Reloaded. New MCP servers are available on the next Codex turn." if reloaded else "Configured MCP servers."]
        if session.get("active_turn_id"):
            lines += ["", "A turn is currently running; reload will not change the tool set already sent to that running turn."]
        if not isinstance(servers, list) or not servers:
            lines += ["", "No MCP servers are currently available."]
            return "\n".join(lines)
        lines.append("")
        for server in servers:
            if not isinstance(server, dict):
                continue
            name = str(server.get("name") or "unknown")
            tools = server.get("tools") if isinstance(server.get("tools"), dict) else {}
            auth = server.get("authStatus") or "unknown"
            lines.append(f"- {name}: {len(tools)} tools, auth {auth}")
        return "\n".join(lines)

    def update_session(self, session_id: str, updates: dict[str, Any], *, sync_head: bool = True) -> None:
        def mutate(data: dict[str, Any]) -> None:
            record = data["sessions"].get(session_id)
            if isinstance(record, dict):
                record.update(updates)

        self.store.update(mutate)
        if sync_head:
            self.sync_session_head_message(session_id)

    @staticmethod
    def task_running(session: dict[str, Any]) -> bool:
        return bool(session.get("active_turn_id"))

    def render_account_status(self) -> str:
        lines = ["Account", ""]
        try:
            account_result = self.app.request("account/read", {}, timeout=30)
            account = account_result.get("account") if isinstance(account_result, dict) else None
            if isinstance(account, dict):
                lines.append(f"Account: {account.get('type') or 'unknown'}")
                if account.get("email"):
                    lines.append(f"Email: {account.get('email')}")
                if account.get("planType"):
                    lines.append(f"Plan: {account.get('planType')}")
            else:
                lines.append("Account: not signed in")
        except Exception as exc:
            lines.append(f"Account: error: {exc}")
        try:
            rate_result = self.app.request("account/rateLimits/read", timeout=30)
            snapshot = rate_result.get("rateLimits") if isinstance(rate_result, dict) else None
            if isinstance(snapshot, dict):
                lines += ["", "Usage limits:"]
                if append_rate_limit_rows(lines, snapshot) == 0:
                    lines.append("Limits: not available for this account")
            else:
                lines += ["", "Usage limits: data not available yet"]
        except Exception as exc:
            lines += ["", f"Usage limits: error: {exc}"]
        return "\n".join(lines)

    def render_session_status(self, session: dict[str, Any]) -> str:
        thread_status = "unknown"
        active_flags: list[Any] = []
        codex_cwd = str(session.get("cwd") or "").strip()
        pending_cwd = str(session.get("pending_cwd") or "").strip()
        try:
            thread_result = self.app.request("thread/read", {"threadId": session.get("thread_id"), "includeTurns": False}, timeout=30)
            thread = thread_result.get("thread") if isinstance(thread_result, dict) else None
            if isinstance(thread, dict):
                status = thread.get("status") if isinstance(thread.get("status"), dict) else {}
                thread_status = str(status.get("type") or "unknown")
                flags = status.get("activeFlags")
                if isinstance(flags, list):
                    active_flags = flags
                if thread.get("cwd"):
                    codex_cwd = str(thread.get("cwd"))
        except Exception as exc:
            thread_status = f"error: {exc}"
        lines = [
            "Session",
            "",
            f"Name: {session.get('name') or 'Codex'}",
            f"Thread: {session.get('thread_id')}",
            f"Thread status: {thread_status}",
            f"Codex session: {session.get('session_id')}",
            "",
            "Settings:",
            f"Model: {session.get('model') or 'default'}",
            f"Effort: {session.get('effort') or 'default'}",
            f"Fast: {'on' if session.get('fast') else 'off'}",
            f"Plan mode: {'ON' if session.get('plan_mode') else 'off'}",
            f"Approval: {approval_label(session.get('approval'))}",
            f"Codex cwd: {display_path(codex_cwd or 'default')}",
            f"Active turn: {session.get('active_turn_id') or 'none'}",
        ]
        if pending_cwd:
            lines.append(f"Pending cwd: {display_path(pending_cwd)}")
        if active_flags:
            lines.append(f"Active flags: {', '.join(str(flag) for flag in active_flags)}")
        return "\n".join(lines)

    def model_list(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self.runtime_lock:
            if self.model_cache and now - self.model_cache[0] < MODEL_CACHE_TTL_SECONDS:
                return [dict(item) for item in self.model_cache[1]]
        models: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            while True:
                params: dict[str, Any] = {"includeHidden": False}
                if cursor:
                    params["cursor"] = cursor
                result = self.app.request("model/list", params, timeout=30)
                page = result.get("data") if isinstance(result, dict) else []
                if isinstance(page, list):
                    models.extend(dict(item) for item in page if isinstance(item, dict))
                cursor = result.get("nextCursor") if isinstance(result, dict) else None
                if not cursor:
                    break
        except Exception as exc:
            log(f"model/list failed: {exc}")
            return []
        with self.runtime_lock:
            self.model_cache = (now, [dict(item) for item in models])
        return models

    def available_model_values(self) -> list[str]:
        values = ["default"]
        seen = {"default"}
        for item in self.model_list():
            if item.get("hidden"):
                continue
            value = str(item.get("model") or item.get("id") or "").strip()
            if value and value not in seen:
                values.append(value)
                seen.add(value)
        if len(values) == 1:
            for value in DEFAULT_MODELS:
                if value not in seen:
                    values.append(value)
                    seen.add(value)
        return values

    def resolve_default_model_id(self) -> str:
        if self.default_model_id:
            return self.default_model_id
        for item in self.model_list():
            if item.get("isDefault"):
                model_id = str(item.get("model") or item.get("id") or "")
                if model_id:
                    self.default_model_id = model_id
                    return model_id
        self.default_model_id = "gpt-5.5"
        return self.default_model_id

    def plan_collaboration_mode(self, session: dict[str, Any]) -> dict[str, Any]:
        mask: dict[str, Any] = {}
        try:
            result = self.app.request("collaborationMode/list", {}, timeout=30)
            modes = result.get("data") or result.get("modes") if isinstance(result, dict) else []
            if isinstance(modes, list):
                for item in modes:
                    if isinstance(item, dict) and item.get("mode") == "plan":
                        mask = item
                        break
        except Exception as exc:
            log(f"collaborationMode/list failed: {exc}")
        model = str(session.get("model") or "default")
        if model == "default":
            model = str(mask.get("model") or self.resolve_default_model_id())
        effort = str(session.get("effort") or "default")
        if effort == "default":
            effort = str(mask.get("reasoning_effort") or mask.get("reasoningEffort") or "medium")
        developer_instructions = mask.get("developer_instructions")
        if "developerInstructions" in mask:
            developer_instructions = mask.get("developerInstructions")
        return {
            "mode": "plan",
            "settings": {
                "model": model,
                "reasoning_effort": effort,
                "developer_instructions": developer_instructions,
            },
        }

    def ensure_thread_loaded(self, session: dict[str, Any]) -> None:
        thread_id = str(session.get("thread_id") or "")
        if not thread_id:
            return
        with self.runtime_lock:
            if thread_id in self.loaded_threads:
                return
        result = self.app.request("thread/resume", {"threadId": thread_id}, timeout=60)
        thread = result.get("thread") if isinstance(result, dict) else {}
        status = thread.get("status") if isinstance(thread, dict) else {}
        status_type = status.get("type") if isinstance(status, dict) else None
        turns = thread.get("turns") if isinstance(thread, dict) else []
        latest_turn = turns[-1] if isinstance(turns, list) and turns else {}
        latest_status = latest_turn.get("status") if isinstance(latest_turn, dict) else None
        if status_type == "idle" or latest_status in {"completed", "interrupted", "cancelled", "failed"}:
            self.set_active_turn_by_thread(thread_id, None)
        with self.runtime_lock:
            self.loaded_threads.add(thread_id)

    def start_or_steer_turn(self, session_id: str, session: dict[str, Any], text: str) -> None:
        channel_id = str(session.get("channel_id") or "")
        root_ts = str(session.get("root_ts") or "")
        thread_id = str(session.get("thread_id") or "")
        if not channel_id or not root_ts or not thread_id:
            return
        created_placeholder = False
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
        if output is None:
            sent = self.api.post_message(channel_id, "…", thread_ts=root_ts)
            output_ts = str(sent.get("ts") or "")
            if not output_ts:
                return
            created_placeholder = True
            with self.runtime_lock:
                self.output_by_thread[thread_id] = SlackTurnOutput(
                    session_id=session_id,
                    channel_id=channel_id,
                    thread_ts=root_ts,
                    turn_id=str(session.get("active_turn_id") or ""),
                    pending_ts=output_ts,
                    pending_last_update_at=time.monotonic(),
                )
        params: dict[str, Any] = {"threadId": thread_id, "input": text_input(text)}
        applied_pending_cwd: str | None = None
        failed_placeholder_ts = ""
        try:
            self.ensure_thread_loaded(session)
            session = self.get_session(session_id) or session
            active_turn_id = session.get("active_turn_id")
            if active_turn_id:
                params["expectedTurnId"] = active_turn_id
                result = self.app.request("turn/steer", params, timeout=60)
                turn = result.get("turn") if isinstance(result, dict) else None
                turn_id = active_turn_id
                if isinstance(turn, dict) and turn.get("id"):
                    turn_id = turn["id"]
            else:
                model = session.get("model")
                effort = session.get("effort")
                cwd = str(session.get("pending_cwd") or "").strip()
                if session.get("fast"):
                    params["serviceTier"] = FAST_SERVICE_TIER
                else:
                    params["serviceTier"] = None
                if session.get("plan_mode"):
                    params["collaborationMode"] = self.plan_collaboration_mode(session)
                elif model and model != "default":
                    params["model"] = model
                if not session.get("plan_mode") and effort and effort != "default":
                    params["effort"] = effort
                if cwd:
                    params["cwd"] = cwd
                    applied_pending_cwd = cwd
                result = self.app.request("turn/start", params, timeout=60)
                turn = result.get("turn") if isinstance(result, dict) else None
                turn_id = turn.get("id") if isinstance(turn, dict) else None
        except Exception as exc:
            log(f"Codex request failed thread={thread_id}: {exc}")
            if created_placeholder:
                with self.runtime_lock:
                    output = self.output_by_thread.pop(thread_id, None)
                    failed_placeholder_ts = output.pending_ts if output else ""
                if failed_placeholder_ts:
                    self.api.update_message(channel_id, failed_placeholder_ts, f"Codex request failed: {exc}")
            else:
                self.api.post_message(channel_id, f"Codex request failed: {exc}", thread_ts=root_ts)
            return
        if not turn_id:
            self.api.post_message(channel_id, "Codex did not return a turn id.", thread_ts=root_ts)
            return
        self.update_session(session_id, {"active_turn_id": turn_id})
        self.mark_pending_cwd_applied(session_id, applied_pending_cwd)
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if output:
                output.turn_id = str(turn_id)

    def mark_pending_cwd_applied(self, session_id: str, cwd: str | None) -> None:
        if not cwd:
            return

        def mutate(data: dict[str, Any]) -> None:
            record = data["sessions"].get(session_id)
            if not isinstance(record, dict):
                return
            record["cwd"] = cwd
            if record.get("pending_cwd") == cwd:
                record.pop("pending_cwd", None)

        self.store.update(mutate)
        self.sync_session_head_message(session_id)

    def set_active_turn_by_thread(self, thread_id: str, turn_id: str | None) -> None:
        def mutate(data: dict[str, Any]) -> list[str]:
            updated: list[str] = []
            for record in data["sessions"].values():
                if isinstance(record, dict) and record.get("thread_id") == thread_id:
                    record["active_turn_id"] = turn_id
                    session_id = str(record.get("id") or "")
                    if session_id:
                        updated.append(session_id)
            return updated

        updated_ids = self.store.update(mutate)
        if isinstance(updated_ids, list):
            for session_id in updated_ids:
                self.sync_session_head_message(str(session_id))

    def on_app_notification(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        thread_id = str(params.get("threadId") or "")
        if not thread_id:
            return
        if method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = turn.get("id")
            if turn_id:
                self.set_active_turn_by_thread(thread_id, str(turn_id))
                self.set_output_turn_id(thread_id, str(turn_id))
        elif method == "item/started":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if item.get("type") == "agentMessage":
                item_id = str(item.get("id") or "")
                turn_id = str(params.get("turnId") or "")
                if item_id:
                    self.ensure_agent_message_output(thread_id, turn_id, item_id)
            else:
                self.start_status_item(thread_id, str(params.get("turnId") or ""), item)
        elif method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                item_id = str(params.get("itemId") or "")
                turn_id = str(params.get("turnId") or "")
                if item_id:
                    self.append_output_delta(thread_id, turn_id, item_id, delta)
        elif method == "item/commandExecution/outputDelta":
            item_id = str(params.get("itemId") or "")
            delta = params.get("delta")
            if item_id and isinstance(delta, str):
                self.append_status_output(thread_id, str(params.get("turnId") or ""), item_id, delta)
        elif method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if item.get("type") == "agentMessage":
                self.apply_completed_agent_message(thread_id, str(params.get("turnId") or ""), item)
            else:
                self.complete_status_item(thread_id, str(params.get("turnId") or ""), item)
        elif method == "turn/completed":
            self.flush_output(thread_id, final=True)
            self.set_active_turn_by_thread(thread_id, None)

    def set_output_turn_id(self, thread_id: str, turn_id: str) -> None:
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if output:
                output.turn_id = turn_id

    def ensure_agent_message_output(self, thread_id: str, turn_id: str, item_id: str) -> SlackAgentMessage | None:
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if not output:
                return None
            if turn_id:
                if output.turn_id and output.turn_id != turn_id:
                    return None
                output.turn_id = turn_id
            existing = output.messages.get(item_id)
            if existing:
                return existing
            if output.pending_active:
                output.pending_active = False
                message = SlackAgentMessage(
                    item_id=item_id,
                    ts=output.pending_ts,
                    last_update_at=output.pending_last_update_at,
                    last_rendered_text=output.pending_text,
                    placeholder_text=output.pending_text,
                    placeholder_step=output.pending_step,
                )
            else:
                sent = self.api.post_message(output.channel_id, "…", thread_ts=output.thread_ts)
                ts = str(sent.get("ts") or "")
                if not ts:
                    return None
                message = SlackAgentMessage(item_id=item_id, ts=ts, last_update_at=time.monotonic())
            output.messages[item_id] = message
            output.message_order.append(item_id)
            return message

    def append_output_delta(self, thread_id: str, turn_id: str, item_id: str, delta: str) -> None:
        self.ensure_agent_message_output(thread_id, turn_id, item_id)
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if not output:
                return
            message = output.messages.get(item_id)
            if not message:
                return
            was_empty = not message.text
            message.text += delta
            now = time.monotonic()
            if was_empty or now - message.last_update_at >= SLACK_UPDATE_INTERVAL:
                self.sync_output_message(output, message)

    def apply_completed_agent_message(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        text = item.get("text")
        if not isinstance(text, str) or not text:
            return
        item_id = str(item.get("id") or "")
        if not item_id:
            return
        self.ensure_agent_message_output(thread_id, turn_id, item_id)
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if not output:
                return
            message = output.messages.get(item_id)
            if not message or message.completed:
                return
            current = message.text.strip()
            completed = text.strip()
            if not current:
                message.text = text
            elif current == completed or completed.startswith(current):
                message.text = text
            elif completed not in current:
                message.text = f"{message.text.rstrip()}\n\n{text}"
            message.completed = True
            self.sync_output_message(output, message)

    def sync_output_message(self, output: SlackTurnOutput, message: SlackAgentMessage) -> None:
        text = message.text.strip() or message.placeholder_text
        chunks = chunk_text(text, SLACK_TEXT_LIMIT)
        first_chunk = chunks[0] if chunks else " "
        if first_chunk != message.last_rendered_text:
            self.api.update_message(output.channel_id, message.ts, first_chunk)
            message.last_rendered_text = first_chunk
            message.last_update_at = time.monotonic()
        if not message.completed:
            return
        for chunk in chunks[1 + len(message.extra_ts) :]:
            sent = self.api.post_message(output.channel_id, chunk, thread_ts=output.thread_ts)
            ts = str(sent.get("ts") or "")
            if not ts:
                break
            message.extra_ts.append(ts)

    def build_status_item(self, item: dict[str, Any], *, completed: bool, output_text: str = "") -> SlackStatusItem | None:
        item_id = str(item.get("id") or "")
        if not item_id:
            return None
        item_type = str(item.get("type") or "activity")
        label = item_type
        compact_type = ""
        if item_type == "commandExecution":
            command = item.get("command") or item.get("cmd") or item.get("name") or "command"
            label = str(command)
            compact_type = "command"
        elif item_type in {"webSearch", "web_search"}:
            query = item.get("query") or item.get("searchQuery") or "search"
            label = str(query)
            compact_type = "search"
        elif item_type in {"mcpToolCall", "dynamicToolCall", "toolCall"}:
            label = str(item.get("tool") or item.get("name") or item.get("server") or item_type)
            compact_type = "mcp" if item_type == "mcpToolCall" else "tool"
        elif item_type == "fileChange":
            label = str(item.get("path") or item.get("file") or "file change")
            compact_type = "file"
        elif item_type == "plan":
            label = "plan"
            compact_type = "plan"
        else:
            return None
        status = str(item.get("status") or ("completed" if completed else "inProgress"))
        failed = completed and status == "failed"
        detail = json.dumps(item, ensure_ascii=False, indent=2)
        if output_text:
            detail += "\n\nOutput:\n" + output_text
        return SlackStatusItem(
            item_id=item_id,
            item_type=compact_type,
            label=label,
            status=status,
            output_text=output_text,
            completed=completed,
            failed=failed,
            detail=detail,
        )

    def start_status_item(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        status = self.build_status_item(item, completed=False, output_text="")
        if not status:
            return
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if not output:
                return
            if turn_id:
                if output.turn_id and output.turn_id != turn_id:
                    return
                output.turn_id = turn_id
            if status.item_id in output.status_items:
                return
            output.status_items[status.item_id] = status
            output.status_order.append(status.item_id)
            self.sync_activity_panel(output)

    def append_status_output(self, thread_id: str, turn_id: str, item_id: str, delta: str) -> None:
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if not output:
                return
            if turn_id:
                if output.turn_id and output.turn_id != turn_id:
                    return
                output.turn_id = turn_id
            status = output.status_items.get(item_id)
            if not status:
                return
            status.output_text = (status.output_text + delta)[-1600:]
            output.activity_dirty = True

    def complete_status_item(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = str(item.get("id") or "")
        if not item_id:
            return
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if not output:
                return
            if turn_id:
                if output.turn_id and output.turn_id != turn_id:
                    return
                output.turn_id = turn_id
            previous = output.status_items.get(item_id)
            output_text = previous.output_text if previous else ""
            status = self.build_status_item(item, completed=True, output_text=output_text)
            if not status:
                return
            output.status_items[item_id] = status
            if item_id not in output.status_order:
                output.status_order.append(item_id)
            output.activity_dirty = True
            self.sync_activity_panel(output, force=True)

    def sync_activity_panel(self, output: SlackTurnOutput, *, force: bool = False) -> None:
        if output.activity_hidden:
            return
        text, blocks = self.render_activity_panel(output)
        if not text or text == output.activity_last_rendered_text:
            output.activity_dirty = False
            return
        now = time.monotonic()
        if output.activity_ts is not None and not force and now - output.activity_last_update_at < ACTIVITY_UPDATE_INTERVAL:
            output.activity_dirty = True
            return
        if output.activity_ts is None:
            sent = self.api.post_message(output.channel_id, text, thread_ts=output.thread_ts, blocks=blocks)
            output.activity_ts = str(sent.get("ts") or "")
            if not output.activity_ts:
                return
        else:
            self.api.update_message(output.channel_id, output.activity_ts, text, blocks=blocks)
        output.activity_last_rendered_text = text
        output.activity_last_update_at = now
        output.activity_dirty = False

    def render_activity_panel(self, output: SlackTurnOutput) -> tuple[str, list[dict[str, Any]] | None]:
        if output.activity_view == "details":
            pages = self.render_activity_detail_pages(output)
            if pages:
                page = min(max(0, output.activity_page), len(pages) - 1)
                output.activity_page = page
                text = pages[page]
                return text, self.activity_blocks(text, output.turn_id, "details", page, len(pages))
            output.activity_view = "summary"
        text = self.render_activity_summary(output)
        return text, self.activity_blocks(text, output.turn_id, "summary", 0, 1)

    def render_activity_summary(self, output: SlackTurnOutput) -> str:
        items = list(output.status_items.values())
        if not items:
            return ""
        counts: dict[str, int] = {}
        running: list[SlackStatusItem] = []
        failures: list[SlackStatusItem] = []
        for item in items:
            counts[item.item_type] = counts.get(item.item_type, 0) + 1
            if not item.completed:
                running.append(item)
            if item.failed:
                failures.append(item)
        done = sum(1 for item in items if item.completed)
        lines = [f"Activity: {done}/{len(items)} done"]
        parts = [f"{key} {value}" for key, value in sorted(counts.items())]
        if parts:
            lines.append(", ".join(parts))
        if running:
            lines.append(f"Running: {compact_label(running[-1].label)}")
        elif items:
            lines.append(f"Latest: {compact_label(items[-1].label)}")
        if failures:
            lines.append(f"Failures: {len(failures)}")
        return "\n".join(lines)

    def render_activity_detail_pages(self, output: SlackTurnOutput) -> list[str]:
        lines = ["Activity details", ""]
        items = [output.status_items[item_id] for item_id in output.status_order if item_id in output.status_items]
        for index, item in enumerate(items[:ACTIVITY_DETAIL_MAX_ITEMS], 1):
            marker = "failed" if item.failed else ("done" if item.completed else "running")
            lines.append(f"{index}. {item.item_type} {marker}")
            lines.append(truncate_middle(item.detail or item.label, 1000))
            lines.append("")
        if len(items) > ACTIVITY_DETAIL_MAX_ITEMS:
            lines.append(f"... {len(items) - ACTIVITY_DETAIL_MAX_ITEMS} more item(s) omitted")
        return chunk_text("\n".join(lines).strip(), ACTIVITY_DETAIL_PAGE_LIMIT)

    def activity_blocks(self, text: str, turn_id: str, view: str, page: int, page_count: int) -> list[dict[str, Any]] | None:
        if not turn_id:
            return None
        elements: list[dict[str, Any]] = []
        if view == "details":
            if page > 0:
                elements.append({"type": "button", "text": {"type": "plain_text", "text": "Prev"}, "action_id": "activity_prev", "value": f"details:{turn_id}:{page - 1}"})
            elements.append({"type": "button", "text": {"type": "plain_text", "text": f"{page + 1}/{max(1, page_count)}"}, "action_id": "activity_page", "value": f"details:{turn_id}:{page}"})
            if page + 1 < page_count:
                elements.append({"type": "button", "text": {"type": "plain_text", "text": "Next"}, "action_id": "activity_next", "value": f"details:{turn_id}:{page + 1}"})
            elements.append({"type": "button", "text": {"type": "plain_text", "text": "Summary"}, "action_id": "activity_summary", "value": f"summary:{turn_id}:0"})
        else:
            elements.append({"type": "button", "text": {"type": "plain_text", "text": "Details"}, "action_id": "activity_details", "value": f"details:{turn_id}:0"})
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{text[:2800]}```"}},
            {"type": "actions", "elements": elements[:5]},
        ]

    def handle_activity_action(self, payload: dict[str, Any], value: str) -> None:
        view, turn_id, raw_page = (value.split(":", 2) + ["0"])[:3]
        try:
            page = int(raw_page)
        except ValueError:
            page = 0
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        channel = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
        channel_id = str(channel.get("id") or "")
        ts = str(message.get("ts") or "")
        with self.runtime_lock:
            output = self.find_output_by_turn_id(turn_id)
            if output:
                output.activity_view = "details" if view == "details" else "summary"
                output.activity_page = page
                text, blocks = self.render_activity_panel(output)
                self.api.update_message(channel_id, ts, text, blocks=blocks)
                return
            cached = self.activity_details_by_turn.get(turn_id)
        if not cached:
            return
        _, cached_channel, _, summary, pages = cached
        if view == "summary":
            text = summary
            blocks = self.activity_blocks(text, turn_id, "summary", 0, max(1, len(pages)))
        else:
            if not pages:
                return
            safe_page = min(max(0, page), len(pages) - 1)
            text = pages[safe_page]
            blocks = self.activity_blocks(text, turn_id, "details", safe_page, len(pages))
        self.api.update_message(channel_id or cached_channel, ts, text, blocks=blocks)

    def find_output_by_turn_id(self, turn_id: str) -> SlackTurnOutput | None:
        for output in self.output_by_thread.values():
            if output.turn_id == turn_id:
                return output
        return None

    def flush_output(self, thread_id: str, *, final: bool) -> None:
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if not output:
                return
            if output.status_items:
                self.sync_activity_panel(output, force=True)
            if output.pending_active and not output.messages:
                self.api.update_message(output.channel_id, output.pending_ts, "Codex completed without an agent message.")
            for item_id in output.message_order:
                message = output.messages.get(item_id)
                if message:
                    self.sync_output_message(output, message)
            if final:
                if output.turn_id and output.status_items:
                    self.activity_details_by_turn[output.turn_id] = (
                        output.session_id,
                        output.channel_id,
                        output.thread_ts,
                        self.render_activity_summary(output),
                        self.render_activity_detail_pages(output),
                    )
                self.output_by_thread.pop(thread_id, None)

    def placeholder_loop(self) -> None:
        while True:
            time.sleep(SLACK_PLACEHOLDER_INTERVAL)
            updates: list[tuple[str, str, str]] = []
            now = time.monotonic()
            with self.runtime_lock:
                for output in self.output_by_thread.values():
                    if output.activity_dirty:
                        self.sync_activity_panel(output)
                    if output.pending_active:
                        output.pending_step = output.pending_step % SLACK_PLACEHOLDER_MAX_STEPS + 1
                        output.pending_text = "…" * output.pending_step
                        output.pending_last_update_at = now
                        updates.append((output.channel_id, output.pending_ts, output.pending_text))
                    for item_id in output.message_order:
                        message = output.messages.get(item_id)
                        if not message or message.text or message.completed:
                            continue
                        if now - message.last_update_at < SLACK_PLACEHOLDER_INTERVAL:
                            continue
                        message.placeholder_step = message.placeholder_step % SLACK_PLACEHOLDER_MAX_STEPS + 1
                        message.placeholder_text = "…" * message.placeholder_step
                        message.last_update_at = now
                        if message.placeholder_text != message.last_rendered_text:
                            message.last_rendered_text = message.placeholder_text
                            updates.append((output.channel_id, message.ts, message.placeholder_text))
            for channel_id, ts, text in updates:
                try:
                    self.api.update_message(channel_id, ts, text)
                except Exception as exc:
                    log(f"placeholder update failed: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slack single-bot bridge backed by codex app-server.")
    parser.add_argument("--slack-bot-token", default="", help="Slack bot token; defaults to SLACK_BOT_TOKEN")
    parser.add_argument("--slack-app-token", default="", help="Slack app-level token for Socket Mode; defaults to SLACK_APP_TOKEN")
    parser.add_argument("--bridge-home", default=os.environ.get("SLACK_BRIDGE_HOME", DEFAULT_HOME))
    parser.add_argument("--slack-timeout", type=int, default=env_int("SLACK_TIMEOUT", 30))
    parser.add_argument("--authorized-user-ids", default="", help="comma-separated Slack user IDs allowed to use the bridge")
    parser.add_argument("--authorized-team-ids", default="", help="comma-separated Slack team IDs allowed to use the bridge")
    parser.add_argument(
        "--codex-command",
        nargs="+",
        default=os.environ.get("CODEX_APP_SERVER_COMMAND", "codex app-server --listen stdio://").split(),
        help="command used to start codex app-server",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not shutil.which(args.codex_command[0]):
        raise SystemExit(f"missing command: {args.codex_command[0]}")
    bridge = SlackCodexBridge(args)
    try:
        bridge.run()
    except KeyboardInterrupt:
        bridge.app.stop()
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
