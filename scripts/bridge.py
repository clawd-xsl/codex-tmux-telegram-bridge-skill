#!/usr/bin/env python3
"""Telegram multi-bot bridge backed by `codex app-server`.

This app-server bridge runs one manager bot plus any number of managed session
bots:

- Manager bot handles `/new` and the setup card UX.
- Each session bot is bound to exactly one Codex app-server thread.
- Session bots forward Telegram text to Codex and stream each assistant message
  item by editing its own Telegram message in place.

Tokens are stored in plaintext under BRIDGE_HOME by design. Keep that directory
private.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import queue
import random
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


DEFAULT_HOME = "~/.codex-telegram-bridge"
DEFAULT_MODELS = ["default", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gpt-5.2"]
REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]
EFFORTS = ["default", *REASONING_EFFORTS]
FAST_SERVICE_TIER = "priority"
MODEL_CACHE_TTL_SECONDS = 60.0
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
SESSION_DEVELOPER_INSTRUCTIONS = """This Codex thread is displayed through a Telegram bridge.
For non-trivial tasks, preserve the normal Codex rollout shape: send concise
commentary updates before meaningful tool batches and before a long final answer.
Do not save all intermediate progress for one large final message. Keep the final
answer focused on conclusions and next steps."""
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_EDIT_LIMIT = 4000
TELEGRAM_EDIT_INTERVAL = 1.2
TELEGRAM_TYPING_INTERVAL = 4.0
TELEGRAM_PLACEHOLDER_INTERVAL = 2.5
TELEGRAM_PLACEHOLDER_MAX_STEPS = 6
ACTIVITY_EDIT_INTERVAL = 4.0
ACTIVITY_DETAIL_PAGE_LIMIT = 3500
ACTIVITY_DETAIL_MAX_ITEMS = 40


SESSION_COMMANDS = [
    ("status", "show account and usage limits"),
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
]
PRIVATE_SESSION_COMMANDS = [("commands", "open Codex command card")]

COMMAND_CARD_ACTIONS = [
    ("account_status", "Account"),
    ("session_status", "Session"),
    ("interrupt", "Interrupt"),
    ("plan", "Plan Mode"),
    ("goal_menu", "Goal"),
    ("review", "Review"),
    ("compact", "Compact"),
    ("model_menu", "Model"),
    ("effort_menu", "Effort"),
    ("cwd_browser", "Work Dir"),
    ("fast_toggle", "Fast"),
]


def log(message: str) -> None:
    print(f"[multi-bot-bridge] {message}", file=sys.stderr, flush=True)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer")


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number")


def now_ms() -> int:
    return int(time.time() * 1000)


def rand_suffix(length: int = 5) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def normalize_username(value: str) -> str:
    value = value.strip().lstrip("@").lower()
    value = re.sub(r"[^a-z0-9_]", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = f"codex_{rand_suffix()}"
    if not value.endswith("bot"):
        value = f"{value}_bot"
    if len(value) < 5:
        value = f"{value}_{rand_suffix(3)}"
    return value[:32]


def normalize_display_name(value: str) -> str:
    return " ".join(value.split()).strip()[:64]


def display_name_key(value: str) -> str:
    return normalize_display_name(value).casefold()


def command_slug_from_name(value: str) -> str:
    normalized = normalize_display_name(value)
    slug = normalized.lower()
    slug = re.sub(r"[^a-z0-9_]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        if normalized:
            digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:6]
            slug = f"codex_{digest}"
        else:
            slug = "codex"
    if not re.match(r"^[a-z]", slug):
        slug = f"codex_{slug}"
    return slug[:32]


def username_from_name(name: str) -> str:
    return normalize_username(f"{name}_{rand_suffix(4)}")


def display_name_from_args(args: str) -> str:
    cleaned = normalize_display_name(args)
    return cleaned if cleaned else f"Codex {rand_suffix(4)}"


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


def newbot_url(manager_username: str, suggested_username: str, suggested_name: str) -> str:
    manager = manager_username.lstrip("@")
    username = suggested_username.lstrip("@")
    query = urllib.parse.urlencode({"name": suggested_name})
    return f"https://t.me/newbot/{manager}/{username}?{query}"


def bot_url(username: str | None) -> str | None:
    if not username:
        return None
    return f"https://t.me/{str(username).lstrip('@')}"


def startgroup_url(username: str | None) -> str | None:
    if not username:
        return None
    return f"https://t.me/{str(username).lstrip('@')}?startgroup=codex&admin=manage_topics"


def json_param(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, bool)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def text_input(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text, "text_elements": []}]


def chunk_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    if not text:
        return [""]
    chunks = []
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
    return chunks


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head - 20)
    return f"{text[:head]}\n... truncated ...\n{text[-tail:]}"


def format_unix_seconds(value: Any) -> str:
    if value in (None, ""):
        return "unknown"
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return str(value)
    local = time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))
    remaining = seconds - int(time.time())
    if remaining <= 0:
        return f"{local} (now)"
    mins = remaining // 60
    if mins < 60:
        return f"{local} ({mins}m)"
    hours = mins // 60
    if hours < 48:
        return f"{local} ({hours}h)"
    return f"{local} ({hours // 24}d)"


def format_reset_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return str(value)
    reset = time.localtime(seconds)
    captured = time.localtime()
    time_text = time.strftime("%H:%M", reset)
    if reset.tm_year == captured.tm_year and reset.tm_yday == captured.tm_yday:
        return time_text
    month = time.strftime("%b", reset)
    return f"{time_text} on {reset.tm_mday} {month}"


def limit_duration_label(value: Any, fallback: str) -> str:
    try:
        minutes = max(0, int(value))
    except (TypeError, ValueError):
        return fallback
    minutes_per_hour = 60
    minutes_per_day = 24 * minutes_per_hour
    minutes_per_week = 7 * minutes_per_day
    minutes_per_month = 30 * minutes_per_day
    rounding_bias_minutes = 3
    if minutes <= minutes_per_day + rounding_bias_minutes:
        hours = max(1, (minutes + rounding_bias_minutes) // minutes_per_hour)
        return f"{hours}h"
    if minutes <= minutes_per_week + rounding_bias_minutes:
        return "weekly"
    if minutes <= minutes_per_month + rounding_bias_minutes:
        return "monthly"
    return "annual"


def capitalize_first(text: str) -> str:
    if not text:
        return text
    return text[0].upper() + text[1:]


def numeric_percent(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_rate_limit_reached(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Usage limit reached"
    labels = {
        "rate_limit_reached": "Usage limit reached",
        "workspace_owner_credits_depleted": "Workspace credits depleted",
        "workspace_member_credits_depleted": "Workspace member credits depleted",
        "workspace_owner_usage_limit_reached": "Workspace usage limit reached",
        "workspace_member_usage_limit_reached": "Workspace member usage limit reached",
    }
    snake = re.sub(r"(?<!^)([A-Z])", r"_\1", text).replace("-", "_").lower()
    return labels.get(snake, snake.replace("_", " ").capitalize())


def format_credit_balance(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return text
    if value <= 0:
        return None
    return str(int(round(value)))


def render_credits_snapshot(credits: dict[str, Any] | None) -> str | None:
    if not isinstance(credits, dict) or not credits.get("hasCredits"):
        return None
    if credits.get("unlimited"):
        return "Credits: Unlimited"
    balance = format_credit_balance(credits.get("balance"))
    if not balance:
        return None
    return f"Credits: {balance} credits"


def format_goal_elapsed_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if hours >= 24:
        days = hours // 24
        remaining_hours = hours % 24
        return f"{days}d {remaining_hours}h {remaining_minutes}m"
    return f"{hours}h" if remaining_minutes == 0 else f"{hours}h {remaining_minutes}m"


def render_rate_window(fallback_duration: str, window: dict[str, Any] | None) -> str:
    label = capitalize_first(
        limit_duration_label(
            window.get("windowDurationMins") if isinstance(window, dict) else None,
            fallback_duration,
        )
    )
    if not isinstance(window, dict):
        return f"{label} limit: data not available"
    used = numeric_percent(window.get("usedPercent"))
    if used is None:
        summary = "data not available"
    else:
        remaining = max(0.0, min(100.0, 100.0 - used))
        summary = f"{remaining:.0f}% left"
    reset = format_reset_timestamp(window.get("resetsAt"))
    suffix = f" (resets {reset})" if reset else ""
    return f"{label} limit: {summary}{suffix}"


def append_rate_limit_rows(lines: list[str], snapshot: dict[str, Any], *, include_limit_name: bool = False) -> int:
    count = 0
    name = str(snapshot.get("limitName") or snapshot.get("limitId") or "codex")
    if include_limit_name and name.lower() != "codex":
        lines.append(f"{name} limit")
        count += 1
    primary = snapshot.get("primary") if isinstance(snapshot.get("primary"), dict) else None
    secondary = snapshot.get("secondary") if isinstance(snapshot.get("secondary"), dict) else None
    if primary:
        lines.append(render_rate_window("5h", primary))
        count += 1
    if secondary:
        lines.append(render_rate_window("weekly", secondary))
        count += 1
    credits = render_credits_snapshot(snapshot.get("credits") if isinstance(snapshot.get("credits"), dict) else None)
    if credits:
        lines.append(credits)
        count += 1
    return count


def display_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "default"
    if raw in {"default", "unknown", "none"}:
        return raw
    expanded_raw = os.path.expanduser(raw)
    if not os.path.isabs(expanded_raw):
        return truncate_middle(raw, 120)
    expanded = os.path.abspath(expanded_raw)
    home = os.path.abspath(os.path.expanduser("~"))
    if expanded == home:
        return "~"
    prefix = home + os.sep
    if expanded.startswith(prefix):
        expanded = "~/" + expanded[len(prefix) :]
    return truncate_middle(expanded, 120)


def plan_mode_message(enabled: bool) -> str:
    if enabled:
        return "Plan mode is ON. Future idle messages will start Codex in Plan mode."
    return "Plan mode is OFF. Future idle messages will use normal chat mode."


def normalize_cwd_path(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value.strip()))


class JsonStore:
    def __init__(self, home: str) -> None:
        self.home = os.path.abspath(os.path.expanduser(home))
        self.path = os.path.join(self.home, "state.json")
        self.lock = threading.RLock()
        self.data = {
            "drafts": {},
            "bots": {},
            "pending_inputs": {},
            "pending_session_inputs": {},
            "pending_goal_replacements": {},
            "path_browsers": {},
            "pending_creations": {},
            "manager": {},
        }
        self.load()

    def load(self) -> None:
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.home, 0o700)
        except OSError:
            pass
        if not os.path.exists(self.path):
            self.save()
            return
        with self.lock:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                for key in self.data:
                    if key not in loaded or not isinstance(loaded[key], dict):
                        loaded[key] = {}
                self.data = loaded

    def save(self) -> None:
        with self.lock:
            os.makedirs(self.home, mode=0o700, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def update(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        with self.lock:
            result = fn(self.data)
            self.save()
            return result

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.data))


class TelegramBot:
    def __init__(self, token: str, timeout: int) -> None:
        self.token = token
        self.timeout = timeout
        self.api = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, **kwargs: Any) -> dict[str, Any] | None:
        data = urllib.parse.urlencode(
            {key: json_param(value) for key, value in kwargs.items() if value is not None}
        ).encode()
        try:
            with urllib.request.urlopen(
                f"{self.api}/{method}",
                data=data,
                timeout=self.timeout + 30,
            ) as response:
                payload = json.loads(response.read())
                if isinstance(payload, dict):
                    if not payload.get("ok"):
                        log(f"telegram {method} failed: {payload}")
                    return payload
                log(f"telegram {method} returned non-object payload")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:500]
            log(f"telegram {method} HTTP {exc.code}: {body}")
        except Exception as exc:
            log(f"telegram {method} error: {exc}")
        return None

    def get_updates(self, offset: int | None, allowed_updates: list[str]) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "timeout": self.timeout,
            "allowed_updates": allowed_updates,
        }
        if offset is not None:
            params["offset"] = offset
        query = urllib.parse.urlencode({key: json_param(value) for key, value in params.items()})
        try:
            with urllib.request.urlopen(
                f"{self.api}/getUpdates?{query}",
                timeout=self.timeout + 30,
            ) as response:
                payload = json.loads(response.read())
                if isinstance(payload, dict):
                    return payload
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:500]
            log(f"telegram getUpdates HTTP {exc.code}: {body}")
        except Exception as exc:
            log(f"telegram getUpdates error: {exc}")
        return None

    def get_me(self) -> dict[str, Any] | None:
        payload = self.call("getMe")
        result = payload.get("result") if payload else None
        return result if isinstance(result, dict) else None

    def get_chat(self, chat_id: str | int) -> dict[str, Any] | None:
        payload = self.call("getChat", chat_id=chat_id)
        result = payload.get("result") if payload else None
        return result if isinstance(result, dict) else None

    def get_chat_member(self, chat_id: str | int, user_id: str | int) -> dict[str, Any] | None:
        payload = self.call("getChatMember", chat_id=chat_id, user_id=user_id)
        result = payload.get("result") if payload else None
        return result if isinstance(result, dict) else None

    def create_forum_topic(self, chat_id: str | int, name: str) -> dict[str, Any] | None:
        payload = self.call("createForumTopic", chat_id=chat_id, name=name[:128])
        result = payload.get("result") if payload else None
        return result if isinstance(result, dict) else None

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> dict[str, Any] | None:
        last = None
        for chunk in chunk_text(text):
            payload = self.call(
                "sendMessage",
                chat_id=chat_id,
                text=chunk,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
            )
            last = payload
            reply_markup = None
            reply_to_message_id = None
        return last

    def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text[:TELEGRAM_EDIT_LIMIT] or " ",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

    def delete_message(self, chat_id: str | int, message_id: int) -> dict[str, Any] | None:
        return self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
        url: str | None = None,
    ) -> None:
        self.call(
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text,
            show_alert=show_alert,
            url=url,
        )

    def send_typing(self, chat_id: str | int, message_thread_id: int | None = None) -> None:
        self.call(
            "sendChatAction",
            chat_id=chat_id,
            action="typing",
            message_thread_id=message_thread_id,
        )

    def set_my_commands(self, commands: list[tuple[str, str]], scope: dict[str, Any] | None = None) -> None:
        self.call(
            "setMyCommands",
            commands=[{"command": cmd, "description": desc} for cmd, desc in commands],
            scope=scope,
        )


class AppServerClient:
    def __init__(self, command: list[str], on_notification: Callable[[dict[str, Any]], None]) -> None:
        self.command = command
        self.on_notification = on_notification
        self.proc: subprocess.Popen[str] | None = None
        self.lock = threading.RLock()
        self.pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.next_id = 1
        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None

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
            self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
            self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
            self.reader_thread.start()
            self.stderr_thread.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "telegram-multi-bot-bridge",
                    "title": "Telegram Multi Bot Bridge",
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
class AgentMessageOutput:
    item_id: str
    message_id: int
    text: str = ""
    last_edit_at: float = 0.0
    last_rendered_text: str = ""
    placeholder_text: str = "…"
    placeholder_step: int = 1
    completed: bool = False
    extra_message_ids: list[int] = field(default_factory=list)


@dataclass
class StatusItemOutput:
    item_id: str
    item_type: str
    label: str
    status: str = "inProgress"
    output_text: str = ""
    completed: bool = False
    failed: bool = False
    detail: str = ""


@dataclass
class TurnOutputState:
    bot_key: str
    chat_id: str
    message_thread_id: int | None
    turn_id: str
    pending_message_id: int
    pending_active: bool = True
    pending_text: str = "…"
    pending_step: int = 1
    pending_last_edit_at: float = 0.0
    next_typing_at: float = 0.0
    messages: dict[str, AgentMessageOutput] = field(default_factory=dict)
    message_order: list[str] = field(default_factory=list)
    status_items: dict[str, StatusItemOutput] = field(default_factory=dict)
    status_order: list[str] = field(default_factory=list)
    activity_message_id: int | None = None
    activity_last_rendered_text: str = ""
    activity_last_edit_at: float = 0.0
    activity_dirty: bool = False
    activity_hidden: bool = False
    activity_view: str = "summary"
    activity_page: int = 0


class MultiBotBridge:
    def __init__(self, args: argparse.Namespace) -> None:
        manager_token = args.manager_token or os.environ.get("TELEGRAM_MANAGER_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not manager_token:
            raise SystemExit("TELEGRAM_MANAGER_BOT_TOKEN or TELEGRAM_BOT_TOKEN is required")
        self.store = JsonStore(args.bridge_home)
        self.timeout = args.telegram_timeout
        self.manager = TelegramBot(manager_token, self.timeout)
        self.manager_me = self.manager.get_me() or {}
        self.manager_username = str(self.manager_me.get("username") or "")
        self.authorized_user_ids = self._parse_id_set(args.authorized_user_ids or os.environ.get("AUTHORIZED_USER_IDS", ""))
        self.authorized_chat_ids = self._parse_id_set(args.authorized_chat_ids or os.environ.get("AUTHORIZED_CHAT_IDS", os.environ.get("AUTHORIZED_CHAT_ID", "")))
        self.app = AppServerClient(args.codex_command, self.on_app_notification)
        self.session_threads: dict[str, threading.Thread] = {}
        self.session_bots: dict[str, TelegramBot] = {}
        self.output_by_thread: dict[str, TurnOutputState] = {}
        self.activity_details_by_turn: dict[str, tuple[str, str, int | None, str, list[str]]] = {}
        self.loaded_threads: set[str] = set()
        self.default_model_id: str | None = None
        self.model_cache: tuple[float, list[dict[str, Any]]] | None = None
        self.runtime_lock = threading.RLock()

    @staticmethod
    def _parse_id_set(raw: str) -> set[str]:
        return {part.strip() for part in raw.split(",") if part.strip()}

    def allowed(self, user: dict[str, Any] | None, chat: dict[str, Any] | None) -> bool:
        user_id = str((user or {}).get("id", ""))
        chat_id = str((chat or {}).get("id", ""))
        if self.authorized_user_ids and user_id not in self.authorized_user_ids:
            return False
        if self.authorized_chat_ids and chat_id not in self.authorized_chat_ids and user_id not in self.authorized_chat_ids:
            return False
        return True

    def run(self) -> None:
        self.app.start()
        self.migrate_session_identities()
        self.manager.set_my_commands(
            [
                ("new", "create a Codex session bot"),
                ("sessions", "list managed Codex bots"),
            ]
        )
        threading.Thread(target=self.placeholder_animation_loop, name="telegram-placeholder-animation", daemon=True).start()
        self.start_session_bot_threads()
        self.ensure_existing_session_group_intros()
        self.refresh_all_draft_cards()
        self.poll_manager()

    def migrate_session_identities(self) -> None:
        def mutate(data: dict[str, Any]) -> None:
            used_slugs: set[str] = set()
            for bot_key, record in data["bots"].items():
                if not isinstance(record, dict):
                    continue
                name = normalize_display_name(str(record.get("name") or record.get("username") or f"Codex {bot_key}"))
                if name:
                    record["name"] = name
                record.setdefault("plan_mode", False)
                base_slug = command_slug_from_name(str(record.get("command_slug") or name))
                slug = base_slug
                suffix = 2
                while slug in used_slugs:
                    suffix_text = f"_{suffix}"
                    slug = f"{base_slug[: 32 - len(suffix_text)]}{suffix_text}"
                    suffix += 1
                record["command_slug"] = slug
                used_slugs.add(slug)
            for draft in data["drafts"].values():
                if not isinstance(draft, dict):
                    continue
                name = normalize_display_name(str(draft.get("name") or ""))
                if name:
                    draft["name"] = name
                    draft["command_slug"] = command_slug_from_name(name)

        self.store.update(mutate)

    def session_identity_error(
        self,
        snapshot: dict[str, Any],
        name: str,
        *,
        draft_id: str | None = None,
        bot_key: str | None = None,
    ) -> str | None:
        normalized = normalize_display_name(name)
        if not normalized:
            return "Name cannot be empty."
        name_key = display_name_key(normalized)
        slug = command_slug_from_name(normalized)
        for existing_key, draft in snapshot.get("drafts", {}).items():
            if draft_id is not None and str(existing_key) == str(draft_id):
                continue
            if not isinstance(draft, dict):
                continue
            other_name = normalize_display_name(str(draft.get("name") or ""))
            if other_name and display_name_key(other_name) == name_key:
                return f'Name "{normalized}" is already used by a draft.'
            other_slug = str(draft.get("command_slug") or command_slug_from_name(other_name))
            if other_slug == slug:
                return f'Command /{slug} is already used by another draft.'
        for existing_key, record in snapshot.get("bots", {}).items():
            if bot_key is not None and str(existing_key) == str(bot_key):
                continue
            if not isinstance(record, dict):
                continue
            other_name = normalize_display_name(str(record.get("name") or ""))
            if other_name and display_name_key(other_name) == name_key:
                return f'Name "{normalized}" is already used by @{record.get("username")}.'
            other_slug = str(record.get("command_slug") or command_slug_from_name(other_name or str(record.get("username") or "")))
            if other_slug == slug:
                return f'Command /{slug} is already used by @{record.get("username")}.'
        return None

    def choose_default_session_name(self, snapshot: dict[str, Any]) -> str:
        for _ in range(20):
            name = f"Codex {rand_suffix(4)}"
            if not self.session_identity_error(snapshot, name):
                return name
        return f"Codex {rand_suffix(8)}"

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

    def available_model_options(self) -> list[dict[str, str]]:
        options = [{"value": "default", "label": "default"}]
        seen = {"default"}
        for item in self.model_list():
            if item.get("hidden"):
                continue
            value = str(item.get("model") or item.get("id") or "").strip()
            if not value or value in seen:
                continue
            display = str(item.get("displayName") or value).strip()
            label = value if not display or display == value else f"{display} ({value})"
            options.append({"value": value, "label": label})
            seen.add(value)
        if len(options) > 1:
            return options
        return [{"value": value, "label": value} for value in DEFAULT_MODELS]

    def available_model_values(self) -> list[str]:
        return [option["value"] for option in self.available_model_options()]

    def resolve_model_value(self, value: str | None) -> str:
        model = str(value or "default")
        if model == "default":
            return self.resolve_default_model_id()
        return model

    def effort_options_for_model(self, model_value: str | None) -> list[dict[str, str]]:
        model = self.resolve_model_value(model_value)
        efforts: list[str] = []
        for item in self.model_list():
            if str(item.get("model") or item.get("id") or "") != model:
                continue
            for option in item.get("supportedReasoningEfforts") or []:
                if not isinstance(option, dict):
                    continue
                effort = str(option.get("reasoningEffort") or "").strip()
                if effort and effort not in efforts:
                    efforts.append(effort)
            if not efforts and item.get("defaultReasoningEffort"):
                efforts.append(str(item.get("defaultReasoningEffort")))
            break
        if not efforts:
            efforts = REASONING_EFFORTS
        return [{"value": "default", "label": "default"}] + [
            {"value": effort, "label": "extra high" if effort == "xhigh" else effort}
            for effort in efforts
        ]

    def effort_values_for_model(self, model_value: str | None) -> list[str]:
        return [option["value"] for option in self.effort_options_for_model(model_value)]

    def normalize_effort_for_model(self, model_value: str | None, effort: str | None) -> str:
        value = str(effort or "default")
        if value == "default":
            return "default"
        return value if value in self.effort_values_for_model(model_value) else "default"

    def fast_service_tier_for_model(self, model_value: str | None) -> str:
        model = self.resolve_model_value(model_value)
        for item in self.model_list():
            if str(item.get("model") or item.get("id") or "") != model:
                continue
            for tier in item.get("serviceTiers") or []:
                if not isinstance(tier, dict):
                    continue
                name = str(tier.get("name") or "").lower()
                tier_id = str(tier.get("id") or "")
                if name == "fast" or tier_id in {"fast", FAST_SERVICE_TIER}:
                    return tier_id or FAST_SERVICE_TIER
        return FAST_SERVICE_TIER

    def model_supports_fast(self, model_value: str | None) -> bool:
        models = self.model_list()
        if not models:
            return True
        model = self.resolve_model_value(model_value)
        for item in models:
            if str(item.get("model") or item.get("id") or "") != model:
                continue
            return any(
                isinstance(tier, dict)
                and (
                    str(tier.get("name") or "").lower() == "fast"
                    or str(tier.get("id") or "") in {"fast", FAST_SERVICE_TIER}
                )
                for tier in (item.get("serviceTiers") or [])
            )
        return False

    def start_session_bot_threads(self) -> None:
        snapshot = self.store.snapshot()
        for bot_key, record in snapshot["bots"].items():
            self.start_session_bot(bot_key, record)

    def ensure_existing_session_group_intros(self) -> None:
        snapshot = self.store.snapshot()
        for bot_key, record in snapshot["bots"].items():
            if not isinstance(record, dict):
                continue
            self.ensure_thread_loaded(record)
            chat_id = record.get("group_chat_id")
            if not chat_id:
                continue
            bot = self.session_bots.get(bot_key)
            if not bot:
                continue
            chat = bot.get_chat(chat_id) or {"id": chat_id}
            try:
                self.send_group_intro(bot_key, bot, chat, None)
            except Exception as exc:
                log(f"session bot {bot_key} group intro startup failed: {exc}")

    def start_session_bot(self, bot_key: str, record: dict[str, Any]) -> None:
        with self.runtime_lock:
            if bot_key in self.session_threads:
                return
            token = record.get("token")
            if not token:
                return
            bot = TelegramBot(token, self.timeout)
            self.session_bots[bot_key] = bot
            self.configure_session_bot(bot_key, record, bot=bot)
            thread = threading.Thread(
                target=self.session_poll_loop,
                args=(bot_key, bot),
                name=f"session-bot-{bot_key}",
                daemon=True,
            )
            self.session_threads[bot_key] = thread
            thread.start()

    def poll_manager(self) -> None:
        offset = self.store.snapshot().get("manager", {}).get("offset")
        allowed_updates = ["message", "callback_query", "managed_bot"]
        log(f"manager polling as @{self.manager_username or 'unknown'}")
        while True:
            updates = self.manager.get_updates(offset, allowed_updates)
            if not updates or not updates.get("ok"):
                time.sleep(1)
                continue
            for update in updates.get("result", []):
                if not isinstance(update, dict) or "update_id" not in update:
                    continue
                offset = int(update["update_id"]) + 1
                self.store.update(lambda data: data["manager"].update({"offset": offset}))
                try:
                    self.handle_manager_update(update)
                except Exception as exc:
                    log(f"manager update error: {exc}")

    def handle_manager_update(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("callback_query"), dict):
            self.handle_manager_callback(update["callback_query"])
            return
        if isinstance(update.get("managed_bot"), dict):
            self.handle_managed_bot_update(update["managed_bot"])
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        if isinstance(message.get("managed_bot_created"), dict):
            self.handle_managed_bot_created_message(message)
            return
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        user = message.get("from") if isinstance(message.get("from"), dict) else {}
        if not self.allowed(user, chat):
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        if self.handle_pending_text_input(message, text):
            return
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            payload = parts[1] if len(parts) == 2 else ""
            if payload.startswith("setup_"):
                self.send_setup_card(payload.removeprefix("setup_"), str(chat.get("id")), private=True)
            elif payload.startswith("create_"):
                self.request_managed_bot_creation(payload.removeprefix("create_"), str(user.get("id")))
            return
        if text.startswith("/new"):
            args = text.split(maxsplit=1)[1] if " " in text else ""
            self.create_draft_from_message(message, args)
            return
        if text.startswith("/sessions"):
            self.send_sessions(str(chat.get("id")))

    def create_draft_from_message(self, message: dict[str, Any], args: str) -> None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        user = message.get("from") if isinstance(message.get("from"), dict) else {}
        draft_id = rand_suffix(10)
        snapshot = self.store.snapshot()
        explicit_name = normalize_display_name(args)
        name = explicit_name or self.choose_default_session_name(snapshot)
        error = self.session_identity_error(snapshot, name)
        if error:
            self.manager.send_message(str(chat.get("id", "")), error)
            return
        username = username_from_name(name)
        draft = {
            "id": draft_id,
            "creator_user_id": str(user.get("id", "")),
            "creator_chat_id": str(chat.get("id", "")),
            "group_chat_id": str(chat.get("id", "")),
            "group_message_id": None,
            "private_message_id": None,
            "name": name,
            "username": username,
            "command_slug": command_slug_from_name(name),
            "model": "default",
            "effort": "default",
            "fast": False,
            "approval": "auto_review",
            "status": "draft",
            "created_at_ms": now_ms(),
        }
        self.store.update(lambda data: data["drafts"].__setitem__(draft_id, draft))
        markup = self.setup_keyboard(draft, private=False)
        sent = self.manager.send_message(str(chat.get("id")), self.render_setup_card(draft), reply_markup=markup)
        result = sent.get("result") if sent else None
        if isinstance(result, dict):
            message_id = result.get("message_id")
            self.store.update(lambda data: data["drafts"][draft_id].update({"group_message_id": message_id}))

    def setup_keyboard(self, draft: dict[str, Any], *, private: bool) -> dict[str, Any]:
        draft_id = draft["id"]
        rows: list[list[dict[str, Any]]] = [
            [
                {"text": "Name", "callback_data": f"d:{draft_id}:name"},
                {"text": "Username", "callback_data": f"d:{draft_id}:username"},
            ],
            [
                {"text": "Model", "callback_data": f"d:{draft_id}:model"},
                {"text": "Effort", "callback_data": f"d:{draft_id}:effort"},
                {"text": f"Fast: {'On' if draft.get('fast') else 'Off'}", "callback_data": f"d:{draft_id}:fast"},
            ],
            [
                {"text": f"Approval: {approval_label(draft.get('approval'))}", "callback_data": f"d:{draft_id}:approval"},
            ],
        ]
        create_button = {"text": "Create", "callback_data": f"d:{draft_id}:create"}
        if not private and self.manager_username:
            create_button = {
                "text": "Create",
                "url": f"https://t.me/{self.manager_username}?start=create_{draft_id}",
            }
        rows.append([create_button])
        rows.append([{"text": "Cancel", "callback_data": f"d:{draft_id}:cancel"}])
        return {"inline_keyboard": rows}

    def choice_keyboard(self, draft: dict[str, Any], field: str, *, private: bool) -> dict[str, Any]:
        draft_id = draft["id"]
        options = self.available_model_options() if field == "model" else self.effort_options_for_model(str(draft.get("model") or "default"))
        current = draft.get(field) or "default"
        rows = [
            [
                {
                    "text": f"{'[x]' if option['value'] == current else '[ ]'} {option['label']}",
                    "callback_data": f"d:{draft_id}:set_{field}:{option['value']}",
                }
            ]
            for option in options
        ]
        rows.append([{"text": "Back", "callback_data": f"d:{draft_id}:back"}])
        rows.append([{"text": "Cancel", "callback_data": f"d:{draft_id}:cancel"}])
        return {"inline_keyboard": rows}

    def approval_keyboard(self, draft: dict[str, Any], *, private: bool) -> dict[str, Any]:
        draft_id = draft["id"]
        current = draft.get("approval") or "auto_review"
        rows = [
            [
                {
                    "text": f"{'[x]' if key == current else '[ ]'} {approval_label(key)}",
                    "callback_data": f"d:{draft_id}:set_approval:{key}",
                }
            ]
            for key in APPROVAL_MODES
        ]
        rows += [
            [{"text": "Back", "callback_data": f"d:{draft_id}:back"}],
            [{"text": "Cancel", "callback_data": f"d:{draft_id}:cancel"}],
        ]
        return {"inline_keyboard": rows}

    def render_setup_card(self, draft: dict[str, Any]) -> str:
        fast = "on" if draft.get("fast") else "off"
        model = draft.get("model") or "default"
        effort = draft.get("effort") or "default"
        return "\n".join(
            [
                "New Codex Bot",
                "",
                f"Name: {draft.get('name') or 'not set'}",
                f"Username: @{draft.get('username') or 'not set'}",
                f"Command: /{draft.get('command_slug') or command_slug_from_name(str(draft.get('name') or 'codex'))}",
                f"Model: {model}",
                f"Effort: {effort}",
                f"Fast: {fast}",
                f"Approval: {approval_label(draft.get('approval'))}",
                "",
                f"Status: {draft.get('status', 'draft')}",
            ]
        )

    def render_choice_card(self, draft: dict[str, Any], field: str) -> str:
        current = draft.get(field) or "default"
        label = "model" if field == "model" else "reasoning effort"
        return "\n".join(
            [
                "New Codex Bot",
                "",
                f"Name: {draft.get('name') or 'not set'}",
                f"Username: @{draft.get('username') or 'not set'}",
                "",
                f"Select {label}",
                f"Current: {current}",
            ]
        )

    def render_approval_card(self, draft: dict[str, Any]) -> str:
        return "\n".join(
            [
                "New Codex Bot",
                "",
                f"Name: {draft.get('name') or 'not set'}",
                f"Username: @{draft.get('username') or 'not set'}",
                "",
                "Permissions",
                f"Current: {approval_label(draft.get('approval'))}",
            ]
        )

    def send_setup_card(self, draft_id: str, chat_id: str, *, private: bool) -> None:
        draft = self.store.snapshot()["drafts"].get(draft_id)
        if not isinstance(draft, dict):
            self.manager.send_message(chat_id, "Draft not found or expired.")
            return
        sent = self.manager.send_message(
            chat_id,
            self.render_setup_card(draft),
            reply_markup=self.setup_keyboard(draft, private=private),
        )
        result = sent.get("result") if sent else None
        if private and isinstance(result, dict):
            self.store.update(
                lambda data: data["drafts"][draft_id].update(
                    {
                        "private_chat_id": str(chat_id),
                        "private_message_id": result.get("message_id"),
                    }
                )
            )

    def refresh_draft_cards(self, draft_id: str) -> None:
        draft = self.store.snapshot()["drafts"].get(draft_id)
        if not isinstance(draft, dict):
            return
        text = self.render_setup_card(draft)
        for chat_key, message_key, private in (
            ("group_chat_id", "group_message_id", False),
            ("private_chat_id", "private_message_id", True),
        ):
            chat_id = draft.get(chat_key)
            message_id = draft.get(message_key)
            if chat_id and message_id:
                self.manager.edit_message_text(
                    chat_id,
                    int(message_id),
                    text,
                    reply_markup=self.setup_keyboard(draft, private=private),
                )

    def refresh_all_draft_cards(self) -> None:
        for draft_id in list(self.store.snapshot()["drafts"].keys()):
            self.refresh_draft_cards(str(draft_id))

    def handle_manager_callback(self, callback: dict[str, Any]) -> None:
        data = callback.get("data")
        if not isinstance(data, str) or not data.startswith("d:"):
            return
        user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if not self.allowed(user, chat):
            self.manager.answer_callback_query(str(callback.get("id")), "Unauthorized", show_alert=True)
            return
        parts = data.split(":", 3)
        if len(parts) < 3:
            return
        _, draft_id, action = parts[:3]
        draft = self.store.snapshot()["drafts"].get(draft_id)
        if not isinstance(draft, dict):
            self.manager.answer_callback_query(str(callback.get("id")), "Draft not found", show_alert=True)
            return
        chat_id = str(chat.get("id", user.get("id", "")))
        private = chat.get("type") == "private"
        cb_id = str(callback.get("id"))

        if action in {"name", "username"}:
            self.manager.answer_callback_query(cb_id, "Reply to the prompt I sent.")
            self.request_field_input(draft_id, action, user, chat, message)
            return
        if action == "model":
            self.manager.answer_callback_query(cb_id)
            self.manager.edit_message_text(
                chat_id,
                int(message.get("message_id")),
                self.render_choice_card(draft, "model"),
                reply_markup=self.choice_keyboard(draft, "model", private=private),
            )
            return
        elif action == "effort":
            self.manager.answer_callback_query(cb_id)
            self.manager.edit_message_text(
                chat_id,
                int(message.get("message_id")),
                self.render_choice_card(draft, "effort"),
                reply_markup=self.choice_keyboard(draft, "effort", private=private),
            )
            return
        elif action in {"set_model", "set_effort", "set_approval"}:
            if len(parts) != 4:
                self.manager.answer_callback_query(cb_id, "Missing value", show_alert=True)
                return
            field = action.removeprefix("set_")
            if field == "model":
                values = self.available_model_values()
            elif field == "effort":
                values = self.effort_values_for_model(str(draft.get("model") or "default"))
            else:
                values = list(APPROVAL_MODES)
            value = parts[3]
            if value not in values:
                self.manager.answer_callback_query(cb_id, "Unknown option", show_alert=True)
                return
            if field == "model":
                normalized_effort = self.normalize_effort_for_model(value, str(draft.get("effort") or "default"))
                fast = bool(draft.get("fast")) and self.model_supports_fast(value)

                def mutate_model(data: dict[str, Any]) -> None:
                    draft_record = data["drafts"].get(draft_id)
                    if isinstance(draft_record, dict):
                        draft_record.update({"model": value, "effort": normalized_effort, "fast": fast})

                self.store.update(mutate_model)
            else:
                self.store.update(lambda data: data["drafts"][draft_id].update({field: value}))
            label = "Approval" if field == "approval" else field.title()
            self.manager.answer_callback_query(cb_id, f"{label}: {value}")
            self.refresh_draft_cards(draft_id)
            return
        elif action == "back":
            self.manager.answer_callback_query(cb_id)
            self.manager.edit_message_text(
                chat_id,
                int(message.get("message_id")),
                self.render_setup_card(draft),
                reply_markup=self.setup_keyboard(draft, private=private),
            )
            return
        elif action == "fast":
            next_value = not bool(draft.get("fast"))
            if next_value and not self.model_supports_fast(str(draft.get("model") or "default")):
                self.manager.answer_callback_query(cb_id, "Fast is not available for this model.", show_alert=True)
                return
            self.store.update(lambda data: data["drafts"][draft_id].update({"fast": next_value}))
        elif action == "approval":
            self.manager.answer_callback_query(cb_id)
            self.manager.edit_message_text(
                chat_id,
                int(message.get("message_id")),
                self.render_approval_card(draft),
                reply_markup=self.approval_keyboard(draft, private=private),
            )
            return
        elif action == "create":
            if private:
                self.manager.answer_callback_query(cb_id)
            else:
                self.manager.answer_callback_query(
                    cb_id,
                    "Open the manager private chat to continue creation.",
                    show_alert=True,
                )
            self.request_managed_bot_creation(draft_id, str(user.get("id")))
            return
        elif action == "cancel":
            self.store.update(lambda data: data["drafts"].pop(draft_id, None))
            self.manager.answer_callback_query(cb_id, "Canceled")
            self.manager.edit_message_text(chat_id, int(message.get("message_id")), "Canceled.")
            return
        else:
            return
        self.manager.answer_callback_query(cb_id)
        self.refresh_draft_cards(draft_id)

    def request_field_input(
        self,
        draft_id: str,
        field: str,
        user: dict[str, Any],
        chat: dict[str, Any],
        source_message: dict[str, Any],
    ) -> None:
        user_id = str(user.get("id", ""))
        chat_id = str(chat.get("id", user_id))
        chat_type = chat.get("type")
        label = "display name" if field == "name" else "username"
        first_name = str(user.get("first_name") or user.get("username") or "there")
        prompt = f'<a href="tg://user?id={html.escape(user_id)}">{html.escape(first_name)}</a>, reply with the bot {label}.'
        sent = self.manager.send_message(
            chat_id,
            prompt,
            parse_mode="HTML",
            reply_markup={
                "force_reply": True,
                "input_field_placeholder": "Research Codex" if field == "name" else "research_codex_bot",
                "selective": True,
            },
            message_thread_id=source_message.get("message_thread_id") if chat_type != "private" else None,
        )
        result = sent.get("result") if sent else None
        prompt_message_id = result.get("message_id") if isinstance(result, dict) else None
        self.store.update(
            lambda data: data["pending_inputs"].__setitem__(
                user_id,
                {
                    "kind": "draft_field",
                    "draft_id": draft_id,
                    "field": field,
                    "prompt_chat_id": chat_id,
                    "prompt_message_id": prompt_message_id,
                    "created_at_ms": now_ms(),
                },
            )
        )

    def handle_pending_text_input(self, message: dict[str, Any], text: str) -> bool:
        user = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        user_id = str(user.get("id", ""))
        pending = self.store.snapshot()["pending_inputs"].get(user_id)
        if not isinstance(pending, dict):
            return False
        if pending.get("kind") not in {None, "draft_field"}:
            return False
        prompt_chat_id = pending.get("prompt_chat_id")
        if prompt_chat_id and str(chat.get("id", "")) != str(prompt_chat_id):
            return False
        prompt_message_id = pending.get("prompt_message_id")
        if prompt_message_id and chat.get("type") != "private":
            reply_to = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
            if reply_to.get("message_id") != prompt_message_id:
                return False
        draft_id = pending.get("draft_id")
        field = pending.get("field")
        value = text.strip()
        if value.lower() in {"cancel", "/cancel"}:
            self.store.update(lambda data: data["pending_inputs"].pop(user_id, None))
            self.manager.send_message(str(chat.get("id", user_id)), "Canceled.")
            return True
        if not value:
            self.manager.send_message(str(chat.get("id", user_id)), "Value cannot be empty.")
            return True
        snapshot = self.store.snapshot()
        if field == "name":
            value = normalize_display_name(value)
            error = self.session_identity_error(snapshot, value, draft_id=str(draft_id))
            if error:
                self.manager.send_message(str(chat.get("id", user_id)), error)
                return True
        elif field == "username":
            value = normalize_username(value)
            if not value:
                self.manager.send_message(str(chat.get("id", user_id)), "Username cannot be empty.")
                return True

        def mutate(data: dict[str, Any]) -> None:
            draft = data["drafts"].get(draft_id)
            if isinstance(draft, dict):
                if field == "name":
                    draft["name"] = value
                    draft["command_slug"] = command_slug_from_name(value)
                    if not draft.get("username"):
                        draft["username"] = username_from_name(value)
                elif field == "username":
                    draft["username"] = value
            data["pending_inputs"].pop(user_id, None)

        self.store.update(mutate)
        self.refresh_draft_cards(str(draft_id))
        self.manager.send_message(str(chat.get("id", user_id)), "Updated.")
        return True

    def request_managed_bot_creation(self, draft_id: str, user_id: str) -> None:
        snapshot = self.store.snapshot()
        draft = snapshot["drafts"].get(draft_id)
        if not isinstance(draft, dict):
            self.manager.send_message(user_id, "Draft not found or expired.")
            return
        name = normalize_display_name(str(draft.get("name") or ""))
        error = self.session_identity_error(snapshot, name, draft_id=draft_id)
        if error:
            self.manager.send_message(user_id, error)
            self.refresh_draft_cards(draft_id)
            return
        if str(draft.get("command_slug") or "") != command_slug_from_name(name):
            self.store.update(lambda data: data["drafts"][draft_id].update({"name": name, "command_slug": command_slug_from_name(name)}))
            draft = self.store.snapshot()["drafts"].get(draft_id)
            if not isinstance(draft, dict):
                self.manager.send_message(user_id, "Draft not found or expired.")
                return
        if not self.manager_has_bot_management():
            username = f"@{self.manager_username}" if self.manager_username else "the manager bot"
            self.manager.send_message(
                user_id,
                "\n".join(
                    [
                        f"{username} cannot create managed bots yet.",
                        "",
                        "Open the @BotFather Mini App and enable management of other bots for this manager bot, then tap Create again.",
                    ]
                ),
            )
            return
        request_id = random.randint(1, 2_000_000_000)
        suggested_name = str(draft.get("name") or f"Codex {rand_suffix(4)}")
        suggested_username = normalize_username(str(draft.get("username") or username_from_name(suggested_name)))
        self.store.update(
            lambda data: data["pending_creations"].__setitem__(
                user_id,
                {
                    "draft_id": draft_id,
                    "request_id": request_id,
                    "suggested_username": suggested_username,
                    "created_at_ms": now_ms(),
                },
            )
        )
        url = newbot_url(self.manager_username, suggested_username, suggested_name)
        self.manager.send_message(user_id, "Switched to Telegram's managed-bot creation link.", reply_markup={"remove_keyboard": True})
        self.manager.send_message(
            user_id,
            "\n".join(
                [
                    "Create the managed Telegram bot with the button below.",
                    "",
                    f"Suggested username: @{suggested_username}",
                    "After you confirm in Telegram, I will bind it to a new Codex session.",
                ]
            ),
            reply_markup={"inline_keyboard": [[{"text": "Create managed bot", "url": url}]]},
        )

    def manager_has_bot_management(self) -> bool:
        me = self.manager.get_me() or {}
        self.manager_me = me
        username = me.get("username")
        if username:
            self.manager_username = str(username)
        return me.get("can_manage_bots") is True

    def handle_managed_bot_created_message(self, message: dict[str, Any]) -> None:
        user = message.get("from") if isinstance(message.get("from"), dict) else {}
        user_id = str(user.get("id", ""))
        created = message.get("managed_bot_created") or {}
        bot_info = created.get("bot") if isinstance(created, dict) else None
        if not isinstance(bot_info, dict):
            return
        self.finish_managed_bot(user_id, bot_info)

    def handle_managed_bot_update(self, update: dict[str, Any]) -> None:
        user = update.get("user") if isinstance(update.get("user"), dict) else {}
        bot_info = update.get("bot") if isinstance(update.get("bot"), dict) else None
        if isinstance(bot_info, dict):
            self.finish_managed_bot(str(user.get("id", "")), bot_info)

    def finish_managed_bot(self, user_id: str, bot_info: dict[str, Any]) -> None:
        pending = self.store.snapshot()["pending_creations"].get(user_id)
        if not isinstance(pending, dict):
            log("received managed bot without pending creation")
            return
        draft_id = str(pending.get("draft_id"))
        draft = self.store.snapshot()["drafts"].get(draft_id)
        if not isinstance(draft, dict):
            self.manager.send_message(user_id, "Draft not found or expired.")
            return
        bot_user_id = bot_info.get("id")
        token_payload = self.manager.call("getManagedBotToken", user_id=bot_user_id)
        token = token_payload.get("result") if token_payload else None
        if not isinstance(token, str) or not token:
            self.manager.send_message(user_id, "Could not fetch managed bot token.")
            return

        try:
            thread_result = self.create_codex_thread(draft)
        except Exception as exc:
            self.manager.send_message(user_id, f"Codex thread creation failed: {exc}")
            return

        bot_key = str(bot_user_id)
        record = {
            "bot_user_id": bot_user_id,
            "token": token,
            "username": bot_info.get("username") or draft.get("username"),
            "name": draft.get("name"),
            "command_slug": draft.get("command_slug") or command_slug_from_name(str(draft.get("name") or bot_info.get("username") or bot_key)),
            "thread_id": thread_result["thread_id"],
            "session_id": thread_result["session_id"],
            "cwd": thread_result.get("cwd"),
            "model": draft.get("model", "default"),
            "effort": draft.get("effort", "default"),
            "fast": bool(draft.get("fast")),
            "plan_mode": False,
            "approval": draft.get("approval", "auto_review"),
            "group_chat_id": draft.get("group_chat_id"),
            "active_turn_id": None,
            "offset": None,
            "created_at_ms": now_ms(),
        }

        def mutate(data: dict[str, Any]) -> None:
            data["bots"][bot_key] = record
            data["drafts"].pop(draft_id, None)
            data["pending_creations"].pop(user_id, None)

        self.store.update(mutate)
        self.configure_session_bot(bot_key, record)
        self.start_session_bot(bot_key, record)
        username = record.get("username")
        ready = f"@{username} ready\nthread: {record['thread_id']}\nwork dir: {display_path(record.get('cwd') or 'default')}"
        ready_markup = self.session_ready_keyboard(username)
        self.manager.send_message(user_id, ready, reply_markup={"remove_keyboard": True})
        self.manager.send_message(user_id, "Open the bot or add it to your group.", reply_markup=ready_markup)
        try:
            session_bot = TelegramBot(record["token"], self.timeout)
            session_bot.send_message(user_id, self.session_welcome_text(record), reply_markup=ready_markup)
        except Exception as exc:
            log(f"session bot welcome failed: {exc}")
        group_chat_id = draft.get("group_chat_id")
        group_message_id = draft.get("group_message_id")
        if group_chat_id and group_message_id:
            self.manager.edit_message_text(group_chat_id, int(group_message_id), ready, reply_markup=ready_markup)

    def session_ready_keyboard(self, username: str | None) -> dict[str, Any] | None:
        rows = []
        open_url = bot_url(username)
        add_url = startgroup_url(username)
        if open_url:
            rows.append([{"text": "Open bot", "url": open_url}])
        if add_url:
            rows.append([{"text": "Add to group / Grant topics", "url": add_url}])
        return {"inline_keyboard": rows} if rows else None

    def session_welcome_text(self, record: dict[str, Any]) -> str:
        slug = record.get("command_slug") or command_slug_from_name(str(record.get("name") or record.get("username") or "codex"))
        return "\n".join(
            [
                f"@{record.get('username')} is bound to a Codex session.",
                f"Command: /{slug}",
                "",
                self.render_session_status(record),
                "",
                "Send a message here, or add me to the group and use my dedicated topic there.",
            ]
        )

    def group_intro_text(self, record: dict[str, Any]) -> str:
        slug = record.get("command_slug") or command_slug_from_name(str(record.get("name") or record.get("username") or "codex"))
        return "\n".join(
            [
                f"@{record.get('username')} ready",
                f"Command: /{slug}",
                "",
                f"Thread: {record.get('thread_id')}",
                f"Model: {record.get('model') or 'default'}",
                f"Effort: {record.get('effort') or 'default'}",
                "",
                "Talk in this topic to use this Codex session.",
            ]
        )

    def topic_name(self, record: dict[str, Any]) -> str:
        name = str(record.get("name") or record.get("username") or "Codex")
        return f"{name} / @{record.get('username')}"[:128]

    def create_codex_thread(self, draft: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = approval_thread_params(draft.get("approval"))
        params["developerInstructions"] = SESSION_DEVELOPER_INSTRUCTIONS
        model = draft.get("model")
        if model and model != "default":
            params["model"] = model
        if draft.get("fast"):
            params["serviceTier"] = self.fast_service_tier_for_model(str(model or "default"))
        result = self.app.request("thread/start", params, timeout=120)
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            raise RuntimeError(f"unexpected thread/start response: {result}")
        name = draft.get("name")
        if name:
            try:
                self.app.request("thread/name/set", {"threadId": thread["id"], "name": name}, timeout=30)
            except Exception as exc:
                log(f"thread/name/set failed: {exc}")
        return {
            "thread_id": thread["id"],
            "session_id": thread.get("sessionId"),
            "cwd": thread.get("cwd") or result.get("cwd"),
        }

    def configure_session_bot(self, bot_key: str, record: dict[str, Any], *, bot: TelegramBot | None = None) -> None:
        bot = bot or TelegramBot(record["token"], self.timeout)
        name = normalize_display_name(str(record.get("name") or record.get("username") or f"Codex {bot_key}"))
        slug = str(record.get("command_slug") or command_slug_from_name(name))
        self.store.update(lambda data: data["bots"].get(bot_key, {}).update({"name": name, "command_slug": slug}))
        group_commands = [(slug, f"open {name} commands")]
        bot.set_my_commands([], scope={"type": "default"})
        bot.set_my_commands(PRIVATE_SESSION_COMMANDS, scope={"type": "all_private_chats"})
        bot.set_my_commands(group_commands, scope={"type": "all_group_chats"})
        bot.set_my_commands(group_commands, scope={"type": "all_chat_administrators"})
        if name:
            bot.call("setMyName", name=name)

    def send_sessions(self, chat_id: str) -> None:
        bots = self.store.snapshot()["bots"]
        if not bots:
            self.manager.send_message(chat_id, "No session bots yet.")
            return
        lines = ["Session bots", ""]
        for record in bots.values():
            slug = record.get("command_slug") or command_slug_from_name(str(record.get("name") or record.get("username") or "codex"))
            lines.append(f"/{slug} @{record.get('username')} -> {record.get('thread_id')}")
        self.manager.send_message(chat_id, "\n".join(lines))

    def session_poll_loop(self, bot_key: str, bot: TelegramBot) -> None:
        me = bot.get_me() or {}
        username = str(me.get("username") or "")
        log(f"session bot polling @{username or bot_key}")
        while True:
            snapshot = self.store.snapshot()
            record = snapshot["bots"].get(bot_key)
            if not isinstance(record, dict):
                return
            offset = record.get("offset")
            updates = bot.get_updates(offset, ["message", "callback_query", "my_chat_member"])
            if not updates or not updates.get("ok"):
                time.sleep(1)
                continue
            for update in updates.get("result", []):
                if not isinstance(update, dict) or "update_id" not in update:
                    continue
                offset = int(update["update_id"]) + 1
                self.store.update(lambda data: data["bots"][bot_key].update({"offset": offset}))
                callback = update.get("callback_query")
                if isinstance(callback, dict):
                    try:
                        self.handle_session_callback(bot_key, bot, callback)
                    except Exception as exc:
                        log(f"session bot {bot_key} callback error: {exc}")
                    continue
                member_update = update.get("my_chat_member")
                if isinstance(member_update, dict):
                    try:
                        self.handle_session_member_update(bot_key, bot, member_update)
                    except Exception as exc:
                        log(f"session bot {bot_key} member update error: {exc}")
                message = update.get("message")
                if isinstance(message, dict):
                    try:
                        self.handle_session_message(bot_key, bot, username, message)
                    except Exception as exc:
                        log(f"session bot {bot_key} message error: {exc}")

    def handle_session_message(self, bot_key: str, bot: TelegramBot, bot_username: str, message: dict[str, Any]) -> None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        user = message.get("from") if isinstance(message.get("from"), dict) else {}
        if not self.allowed(user, chat):
            return
        if self.message_added_this_bot(bot_key, message):
            self.send_group_intro(bot_key, bot, chat, message.get("message_thread_id"))
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        record = self.store.snapshot()["bots"].get(bot_key)
        if not isinstance(record, dict):
            return
        if self.handle_pending_session_input(bot_key, bot, message, text, record):
            return
        chat_type = chat.get("type")
        routed_text = self.route_text_for_session(text, bot_username, chat_type, message, record, bot_key)
        if routed_text is None:
            return
        command, args = parse_command(routed_text, bot_username)
        if command:
            self.handle_session_command(bot_key, bot, message, command, args)
            return
        self.start_or_steer_turn(bot_key, bot, message, routed_text)

    def handle_session_callback(self, bot_key: str, bot: TelegramBot, callback: dict[str, Any]) -> None:
        data = callback.get("data")
        if not isinstance(data, str):
            return
        if data.startswith("cwd:"):
            self.handle_cwd_browser_callback(bot_key, bot, callback)
            return
        if data.startswith("cmd:"):
            self.handle_session_command_callback(bot_key, bot, callback)
            return
        if not data.startswith("a:"):
            return
        user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if not self.allowed(user, chat):
            bot.answer_callback_query(str(callback.get("id")), "Unauthorized", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) < 3:
            bot.answer_callback_query(str(callback.get("id")), "Bad activity action", show_alert=True)
            return
        _, action, turn_id = parts[:3]
        chat_id = str(chat.get("id", ""))
        message_id = message.get("message_id")
        if action == "hide":
            bot.answer_callback_query(str(callback.get("id")), "Hidden")
            if chat_id and message_id:
                bot.delete_message(chat_id, int(message_id))
            with self.runtime_lock:
                output = self.find_output_by_turn_id(turn_id)
                if output:
                    output.activity_hidden = True
            return
        if action not in {"summary", "details", "page"}:
            bot.answer_callback_query(str(callback.get("id")), "Unknown action", show_alert=True)
            return
        page = 0
        if action == "page" and len(parts) >= 4:
            try:
                page = max(0, int(parts[3]))
            except ValueError:
                page = 0
        elif action == "details":
            page = 0
        rendered = self.render_activity_callback_view(turn_id, view="summary" if action == "summary" else "details", page=page)
        if not rendered:
            bot.answer_callback_query(str(callback.get("id")), "No activity details available.", show_alert=True)
            return
        text, markup = rendered
        bot.answer_callback_query(str(callback.get("id")))
        if chat_id and message_id:
            bot.edit_message_text(chat_id, int(message_id), text, reply_markup=markup)

    @staticmethod
    def task_running(record: dict[str, Any]) -> bool:
        return bool(record.get("active_turn_id"))

    def command_requires_idle_message(self, command: str) -> str:
        return f"'/{command}' is disabled while a task is in progress."

    def handle_session_command_callback(self, bot_key: str, bot: TelegramBot, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        parts = data.split(":", 3)
        if len(parts) < 3:
            bot.answer_callback_query(str(callback.get("id")), "Bad command action", show_alert=True)
            return
        _, target_key, action = parts[:3]
        value = parts[3] if len(parts) == 4 else ""
        cb_id = str(callback.get("id"))
        if target_key != bot_key:
            bot.answer_callback_query(cb_id, "That command belongs to another Codex bot.", show_alert=True)
            return
        user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if not self.allowed(user, chat):
            bot.answer_callback_query(cb_id, "Unauthorized", show_alert=True)
            return
        record = self.store.snapshot()["bots"].get(bot_key)
        if not isinstance(record, dict):
            bot.answer_callback_query(cb_id, "Session not found", show_alert=True)
            return
        chat_id = str(chat.get("id", ""))
        message_id = message.get("message_id")
        message_thread_id = message.get("message_thread_id")
        thread_id = str(record.get("thread_id") or "")
        if not chat_id or not message_id:
            bot.answer_callback_query(cb_id, "Missing message context", show_alert=True)
            return

        if action in {"goal_replace", "goal_cancel"}:
            pending = self.store.snapshot().get("pending_goal_replacements", {}).get(value)
            if not isinstance(pending, dict) or str(pending.get("bot_key")) != bot_key:
                bot.answer_callback_query(cb_id, "Goal confirmation expired.", show_alert=True)
                return
            if action == "goal_cancel":
                self.store.update(lambda data: data["pending_goal_replacements"].pop(value, None))
                bot.answer_callback_query(cb_id, "Canceled")
                bot.edit_message_text(chat_id, int(message_id), "Goal unchanged.", reply_markup=self.session_back_keyboard(bot_key))
                return
            objective = str(pending.get("objective") or "").strip()
            pending_thread_id = str(pending.get("thread_id") or thread_id)
            if not objective:
                bot.answer_callback_query(cb_id, "Goal objective is empty.", show_alert=True)
                return
            try:
                self.app.request("thread/goal/clear", {"threadId": pending_thread_id}, timeout=30)
                result = self.app.request(
                    "thread/goal/set",
                    {"threadId": pending_thread_id, "objective": objective, "status": "active"},
                    timeout=30,
                )
                goal = result.get("goal") if isinstance(result, dict) else result
            except Exception as exc:
                bot.answer_callback_query(cb_id, "Goal replace failed.", show_alert=True)
                bot.edit_message_text(chat_id, int(message_id), f"Goal replace failed: {exc}", reply_markup=self.session_back_keyboard(bot_key))
                return
            self.store.update(lambda data: data["pending_goal_replacements"].pop(value, None))
            bot.answer_callback_query(cb_id, "Goal replaced")
            bot.edit_message_text(chat_id, int(message_id), render_goal(goal), reply_markup=self.session_back_keyboard(bot_key))
            return

        if action in {"commands", "back"}:
            bot.answer_callback_query(cb_id)
            bot.edit_message_text(
                chat_id,
                int(message_id),
                self.render_session_command_card(record),
                reply_markup=self.session_command_card_keyboard(bot_key),
            )
            return
        if action in {"account_status", "status"}:
            bot.answer_callback_query(cb_id)
            bot.edit_message_text(chat_id, int(message_id), self.render_codex_status(record), reply_markup=self.session_back_keyboard(bot_key))
            return
        if action in {"session_status", "bridge_status"}:
            bot.answer_callback_query(cb_id)
            bot.edit_message_text(chat_id, int(message_id), self.render_session_status(record), reply_markup=self.session_back_keyboard(bot_key))
            return
        if action == "goal_menu":
            bot.answer_callback_query(cb_id)
            bot.edit_message_text(
                chat_id,
                int(message_id),
                self.render_goal_menu(record),
                reply_markup=self.goal_menu_keyboard(bot_key),
            )
            return
        if action == "goal_status":
            bot.answer_callback_query(cb_id)
            try:
                goal = self.read_thread_goal(thread_id)
                text = render_goal(goal)
            except Exception as exc:
                text = f"Goal command failed: {exc}"
            bot.edit_message_text(chat_id, int(message_id), text, reply_markup=self.session_back_keyboard(bot_key))
            return
        if action in {"goal_clear", "goal_pause", "goal_resume"}:
            bot.answer_callback_query(cb_id)
            try:
                if action == "goal_clear":
                    result = self.app.request("thread/goal/clear", {"threadId": thread_id}, timeout=30)
                    cleared = result.get("cleared") if isinstance(result, dict) else None
                    text = "Goal cleared." if cleared else "No goal to clear."
                else:
                    status = "paused" if action == "goal_pause" else "active"
                    result = self.app.request("thread/goal/set", {"threadId": thread_id, "status": status}, timeout=30)
                    goal = result.get("goal") if isinstance(result, dict) else result
                    text = render_goal(goal)
            except Exception as exc:
                text = f"Goal command failed: {exc}"
            bot.edit_message_text(chat_id, int(message_id), text, reply_markup=self.session_back_keyboard(bot_key))
            return
        if action in {"model_menu", "effort_menu"}:
            if self.task_running(record):
                blocked_command = "model" if action == "model_menu" else "effort"
                bot.answer_callback_query(cb_id, self.command_requires_idle_message(blocked_command), show_alert=True)
                return
            field = "model" if action == "model_menu" else "effort"
            bot.answer_callback_query(cb_id)
            bot.edit_message_text(
                chat_id,
                int(message_id),
                self.render_session_choice_card(record, field),
                reply_markup=self.session_choice_keyboard(bot_key, field, str(record.get(field) or "default"), record),
            )
            return
        if action in {"set_model", "set_effort"}:
            if self.task_running(record):
                blocked_command = action.removeprefix("set_")
                bot.answer_callback_query(cb_id, self.command_requires_idle_message(blocked_command), show_alert=True)
                return
            field = action.removeprefix("set_")
            values = self.available_model_values() if field == "model" else self.effort_values_for_model(str(record.get("model") or "default"))
            if value not in values:
                bot.answer_callback_query(cb_id, "Unknown option", show_alert=True)
                return
            if field == "model":
                normalized_effort = self.normalize_effort_for_model(value, str(record.get("effort") or "default"))
                fast = bool(record.get("fast")) and self.model_supports_fast(value)

                def mutate_model(data: dict[str, Any]) -> None:
                    bot_record = data["bots"].get(bot_key)
                    if isinstance(bot_record, dict):
                        bot_record.update({"model": value, "effort": normalized_effort, "fast": fast})

                self.store.update(mutate_model)
            else:
                self.update_bot_setting(bot_key, field, value)
            updated = self.store.snapshot()["bots"].get(bot_key, record)
            bot.answer_callback_query(cb_id, f"{field.title()}: {value}")
            bot.edit_message_text(
                chat_id,
                int(message_id),
                self.render_session_command_card(updated if isinstance(updated, dict) else record),
                reply_markup=self.session_command_card_keyboard(bot_key),
            )
            return
        if action == "fast_toggle":
            next_value = not bool(record.get("fast"))
            if next_value and not self.model_supports_fast(str(record.get("model") or "default")):
                bot.answer_callback_query(cb_id, "Fast is not available for this model.", show_alert=True)
                return
            self.update_bot_setting(bot_key, "fast", next_value)
            updated = self.store.snapshot()["bots"].get(bot_key, record)
            bot.answer_callback_query(cb_id, "Fast toggled")
            bot.edit_message_text(
                chat_id,
                int(message_id),
                self.render_session_command_card(updated if isinstance(updated, dict) else record),
                reply_markup=self.session_command_card_keyboard(bot_key),
            )
            return
        if action == "cwd_browser":
            bot.answer_callback_query(cb_id)
            self.open_cwd_browser(bot_key, bot, chat_id, int(message_id), message_thread_id, user, record)
            return
        if action == "cancel_pending":
            pending = self.store.snapshot().get("pending_session_inputs", {}).get(value)
            if not isinstance(pending, dict) or str(pending.get("bot_key") or "") != bot_key:
                bot.answer_callback_query(cb_id, "Nothing to cancel.", show_alert=True)
                return
            self.store.update(lambda data: data["pending_session_inputs"].pop(value, None))
            bot.answer_callback_query(cb_id, "Canceled")
            label = "Goal" if pending.get("action") == "goal_input" else "Input"
            cancel_markup = self.goal_menu_keyboard(bot_key) if pending.get("action") == "goal_input" else self.session_back_keyboard(bot_key)
            bot.edit_message_text(
                chat_id,
                int(message_id),
                f"{label} input canceled.",
                reply_markup=cancel_markup,
            )
            return
        if action in {"goal_input", "cwd_input"}:
            pending_key = self.request_session_command_input(bot_key, bot, user, chat, message, action)
            bot.answer_callback_query(cb_id, "Reply to the prompt I sent.")
            if pending_key and action == "goal_input":
                bot.edit_message_text(
                    chat_id,
                    int(message_id),
                    self.render_goal_input_pending(record),
                    reply_markup=self.pending_session_input_keyboard(bot_key, pending_key, "Cancel"),
                )
            return
        if action == "interrupt":
            turn_id = record.get("active_turn_id")
            if not turn_id:
                bot.answer_callback_query(cb_id, "No active turn.")
                bot.edit_message_text(chat_id, int(message_id), "No active turn.", reply_markup=self.session_back_keyboard(bot_key))
                return
            self.app.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=30)
            bot.answer_callback_query(cb_id, "Interrupted")
            bot.edit_message_text(
                chat_id,
                int(message_id),
                "Interrupt sent.\n\nCodex will stop the active turn for this session. Your next message will start or steer the next turn.",
                reply_markup=self.session_back_keyboard(bot_key),
            )
            return
        if action == "compact":
            if self.task_running(record):
                bot.answer_callback_query(cb_id, self.command_requires_idle_message("compact"), show_alert=True)
                return
            self.app.request("thread/compact/start", {"threadId": thread_id}, timeout=60)
            bot.answer_callback_query(cb_id, "Compaction started")
            bot.edit_message_text(chat_id, int(message_id), "Compaction started.", reply_markup=self.session_back_keyboard(bot_key))
            return
        if action == "review":
            if self.task_running(record):
                bot.answer_callback_query(cb_id, self.command_requires_idle_message("review"), show_alert=True)
                return
            result = self.app.request("review/start", {"threadId": thread_id, "target": {"type": "uncommittedChanges"}}, timeout=60)
            review_thread_id = result.get("reviewThreadId") if isinstance(result, dict) else None
            bot.answer_callback_query(cb_id, "Review started")
            bot.edit_message_text(
                chat_id,
                int(message_id),
                "\n".join(
                    [
                        "Review started.",
                        "",
                        "Target: uncommitted changes",
                        f"Review thread: {review_thread_id or thread_id}",
                        "Findings will stream back here as Codex output.",
                    ]
                ),
                reply_markup=self.session_back_keyboard(bot_key),
            )
            return
        if action == "plan":
            if self.task_running(record):
                bot.answer_callback_query(cb_id, self.command_requires_idle_message("plan"), show_alert=True)
                return
            new_value = not bool(record.get("plan_mode"))
            self.update_bot_setting(bot_key, "plan_mode", new_value)
            updated = self.store.snapshot()["bots"].get(bot_key, record)
            bot.answer_callback_query(cb_id, f"Plan mode: {'on' if new_value else 'off'}")
            bot.send_message(chat_id, plan_mode_message(new_value), message_thread_id=message_thread_id)
            bot.edit_message_text(
                chat_id,
                int(message_id),
                self.render_session_command_card(updated if isinstance(updated, dict) else record),
                reply_markup=self.session_command_card_keyboard(bot_key, updated if isinstance(updated, dict) else record),
            )
            return
        bot.answer_callback_query(cb_id, "Unknown command action", show_alert=True)

    def render_session_command_card(self, record: dict[str, Any]) -> str:
        slug = record.get("command_slug") or command_slug_from_name(str(record.get("name") or record.get("username") or "codex"))
        plan_label = "ON - next idle turn uses Plan mode" if record.get("plan_mode") else "off - normal chat mode"
        return "\n".join(
            [
                "Codex Commands",
                "",
                f"Bot: @{record.get('username')}",
                f"Command: /{slug}",
                f"Model: {record.get('model') or 'default'}",
                f"Effort: {record.get('effort') or 'default'}",
                f"Fast: {'on' if record.get('fast') else 'off'}",
                f"Plan mode: {plan_label}",
                f"Current work dir: {display_path(record.get('cwd') or 'default')}",
            ]
        )

    def session_command_card_keyboard(self, bot_key: str, record: dict[str, Any] | None = None) -> dict[str, Any]:
        if record is None:
            snapshot_record = self.store.snapshot().get("bots", {}).get(bot_key)
            record = snapshot_record if isinstance(snapshot_record, dict) else {}
        rows: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for action, label in COMMAND_CARD_ACTIONS:
            if action == "plan":
                label = "Plan: ON" if record.get("plan_mode") else "Plan: Off"
            elif action == "fast_toggle":
                label = "Fast: On" if record.get("fast") else "Fast: Off"
            current.append({"text": label, "callback_data": f"cmd:{bot_key}:{action}"})
            if len(current) == 2:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        return {"inline_keyboard": rows}

    def render_session_choice_card(self, record: dict[str, Any], field: str) -> str:
        current = record.get(field) or "default"
        label = "Model" if field == "model" else "Effort"
        return "\n".join(
            [
                f"Select {label}",
                "",
                f"Bot: @{record.get('username')}",
                f"Current: {current}",
            ]
        )

    def session_choice_keyboard(self, bot_key: str, field: str, current: str, record: dict[str, Any]) -> dict[str, Any]:
        options = self.available_model_options() if field == "model" else self.effort_options_for_model(str(record.get("model") or "default"))
        rows = [
            [
                {
                    "text": f"{'[x]' if option['value'] == current else '[ ]'} {option['label']}",
                    "callback_data": f"cmd:{bot_key}:set_{field}:{option['value']}",
                }
            ]
            for option in options
        ]
        rows.append([{"text": "Back", "callback_data": f"cmd:{bot_key}:back"}])
        return {"inline_keyboard": rows}

    def session_back_keyboard(self, bot_key: str) -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "Back", "callback_data": f"cmd:{bot_key}:back"}]]}

    def pending_session_input_keyboard(self, bot_key: str, pending_key: str, label: str) -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": label, "callback_data": f"cmd:{bot_key}:cancel_pending:{pending_key}"}]]}

    def render_goal_menu(self, record: dict[str, Any]) -> str:
        return "\n".join(
            [
                "Goal",
                "",
                f"Bot: @{record.get('username')}",
                "Choose a goal action.",
            ]
        )

    def goal_menu_keyboard(self, bot_key: str) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Set Goal", "callback_data": f"cmd:{bot_key}:goal_input"}],
                [
                    {"text": "Current", "callback_data": f"cmd:{bot_key}:goal_status"},
                    {"text": "Clear", "callback_data": f"cmd:{bot_key}:goal_clear"},
                ],
                [
                    {"text": "Pause", "callback_data": f"cmd:{bot_key}:goal_pause"},
                    {"text": "Resume", "callback_data": f"cmd:{bot_key}:goal_resume"},
                ],
                [{"text": "Cancel", "callback_data": f"cmd:{bot_key}:back"}],
            ]
        }

    def render_goal_input_pending(self, record: dict[str, Any]) -> str:
        return "\n".join(
            [
                "Goal",
                "",
                f"Bot: @{record.get('username')}",
                "Reply to the prompt I sent with the new goal text.",
                "",
                "Tap Cancel to leave the goal unchanged.",
            ]
        )

    def send_cwd_browser(
        self,
        bot_key: str,
        bot: TelegramBot,
        chat_id: str,
        message_thread_id: int | None,
        user: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        sent = bot.send_message(chat_id, "Loading working directory picker...", message_thread_id=message_thread_id)
        result = sent.get("result") if sent else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            return
        self.open_cwd_browser(bot_key, bot, chat_id, int(message_id), message_thread_id, user, record)

    def open_cwd_browser(
        self,
        bot_key: str,
        bot: TelegramBot,
        chat_id: str,
        message_id: int,
        message_thread_id: int | None,
        user: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        browser_id = rand_suffix(6)
        start_path = str(record.get("cwd") or os.path.expanduser("~"))
        start_path = normalize_cwd_path(start_path)
        self.store.update(
            lambda data: data["path_browsers"].__setitem__(
                browser_id,
                {
                    "bot_key": bot_key,
                    "user_id": str(user.get("id", "")),
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "message_thread_id": message_thread_id,
                    "path": start_path,
                    "page": 0,
                    "created_at_ms": now_ms(),
                },
            )
        )
        text, markup = self.render_cwd_browser(bot_key, browser_id, start_path, 0, record)
        bot.edit_message_text(chat_id, message_id, text, reply_markup=markup)

    def handle_cwd_browser_callback(self, bot_key: str, bot: TelegramBot, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        parts = data.split(":")
        cb_id = str(callback.get("id"))
        if len(parts) < 4:
            bot.answer_callback_query(cb_id, "Bad cwd action", show_alert=True)
            return
        _, target_key, browser_id, action = parts[:4]
        value = parts[4] if len(parts) >= 5 else ""
        if target_key != bot_key:
            bot.answer_callback_query(cb_id, "That picker belongs to another Codex bot.", show_alert=True)
            return
        user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if not self.allowed(user, chat):
            bot.answer_callback_query(cb_id, "Unauthorized", show_alert=True)
            return
        snapshot = self.store.snapshot()
        browser = snapshot.get("path_browsers", {}).get(browser_id)
        record = snapshot.get("bots", {}).get(bot_key)
        if not isinstance(browser, dict) or not isinstance(record, dict):
            bot.answer_callback_query(cb_id, "Picker expired", show_alert=True)
            return
        if str(browser.get("user_id") or "") and str(browser.get("user_id")) != str(user.get("id", "")):
            bot.answer_callback_query(cb_id, "This picker belongs to another user.", show_alert=True)
            return
        chat_id = str(chat.get("id", browser.get("chat_id", "")))
        message_id = int(message.get("message_id") or browser.get("message_id") or 0)
        if not chat_id or not message_id:
            bot.answer_callback_query(cb_id, "Missing message context", show_alert=True)
            return
        path = normalize_cwd_path(str(browser.get("path") or os.path.expanduser("~")))
        page = int(browser.get("page") or 0)
        if action == "back":
            bot.answer_callback_query(cb_id)
            self.store.update(lambda data: data["path_browsers"].pop(browser_id, None))
            bot.edit_message_text(chat_id, message_id, self.render_session_command_card(record), reply_markup=self.session_command_card_keyboard(bot_key))
            return
        if action == "noop":
            bot.answer_callback_query(cb_id)
            return
        if action == "manual":
            bot.answer_callback_query(cb_id, "Reply to the prompt I sent.")
            self.request_session_command_input(bot_key, bot, user, chat, message, "cwd_input")
            return
        if action == "select":
            error = self.validate_cwd_path(path)
            if error:
                bot.answer_callback_query(cb_id, error, show_alert=True)
                return
            self.update_bot_setting(bot_key, "cwd", path)
            self.store.update(lambda data: data["path_browsers"].pop(browser_id, None))
            bot.answer_callback_query(cb_id, "Work dir updated")
            bot.edit_message_text(chat_id, message_id, f"Work dir set:\n{display_path(path)}", reply_markup=self.session_back_keyboard(bot_key))
            return
        if action == "up":
            path = os.path.dirname(path.rstrip(os.sep)) or os.sep
            page = 0
        elif action == "home":
            path = normalize_cwd_path(os.path.expanduser("~"))
            page = 0
        elif action == "next":
            page += 1
        elif action == "prev":
            page = max(0, page - 1)
        elif action == "dir":
            dirs, error = self.cwd_directory_entries(path)
            if error:
                bot.answer_callback_query(cb_id, error, show_alert=True)
                return
            try:
                index = int(value)
                child = dirs[index]
            except (TypeError, ValueError, IndexError):
                bot.answer_callback_query(cb_id, "Directory not found", show_alert=True)
                return
            path = os.path.join(path, child)
            page = 0
        else:
            bot.answer_callback_query(cb_id, "Unknown cwd action", show_alert=True)
            return
        path = normalize_cwd_path(path)
        self.store.update(
            lambda store: store["path_browsers"].get(browser_id, {}).update(
                {"path": path, "page": page, "updated_at_ms": now_ms()}
            )
        )
        text, markup = self.render_cwd_browser(bot_key, browser_id, path, page, record)
        bot.answer_callback_query(cb_id)
        bot.edit_message_text(chat_id, message_id, text, reply_markup=markup)

    def render_cwd_browser(
        self,
        bot_key: str,
        browser_id: str,
        path: str,
        page: int,
        record: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        dirs, error = self.cwd_directory_entries(path)
        page_size = 8
        page_count = max(1, (len(dirs) + page_size - 1) // page_size)
        page = min(max(0, page), page_count - 1)
        start = page * page_size
        visible = dirs[start : start + page_size]
        text_lines = [
            "Working Directory",
            "",
            f"Bot: @{record.get('username')}",
            f"Current work dir: {display_path(record.get('cwd') or 'default')}",
            f"Browsing: {display_path(path)}",
            "",
            "Choose a folder below, then tap Use This Folder.",
        ]
        if error:
            text_lines += ["", f"Error: {error}"]
        elif not dirs:
            text_lines += ["", "This folder has no child folders."]
        rows: list[list[dict[str, Any]]] = []
        for offset, name in enumerate(visible):
            index = start + offset
            label = name if len(name) <= 28 else f"{name[:25]}..."
            rows.append([{"text": f"{label}/", "callback_data": f"cwd:{bot_key}:{browser_id}:dir:{index}"}])
        nav: list[dict[str, Any]] = []
        if page > 0:
            nav.append({"text": "Prev", "callback_data": f"cwd:{bot_key}:{browser_id}:prev"})
        nav.append({"text": f"Page {page + 1}/{page_count}", "callback_data": f"cwd:{bot_key}:{browser_id}:noop"})
        if page + 1 < page_count:
            nav.append({"text": "Next", "callback_data": f"cwd:{bot_key}:{browser_id}:next"})
        rows.append(nav)
        rows.append(
            [
                {"text": "Parent", "callback_data": f"cwd:{bot_key}:{browser_id}:up"},
                {"text": "Home", "callback_data": f"cwd:{bot_key}:{browser_id}:home"},
            ]
        )
        rows.append(
            [
                {"text": "Use This Folder", "callback_data": f"cwd:{bot_key}:{browser_id}:select"},
                {"text": "Type Path", "callback_data": f"cwd:{bot_key}:{browser_id}:manual"},
            ]
        )
        rows.append([{"text": "Back", "callback_data": f"cwd:{bot_key}:{browser_id}:back"}])
        return "\n".join(text_lines), {"inline_keyboard": rows}

    def cwd_directory_entries(self, path: str) -> tuple[list[str], str | None]:
        try:
            result = self.app.request("fs/readDirectory", {"path": path}, timeout=20)
        except Exception as exc:
            return [], str(exc)
        entries = result.get("entries") if isinstance(result, dict) else []
        dirs = [
            str(entry.get("fileName"))
            for entry in entries
            if isinstance(entry, dict) and entry.get("isDirectory") and entry.get("fileName")
        ]
        dirs.sort(key=lambda value: (value.startswith("."), value.lower()))
        return dirs, None

    def validate_cwd_path(self, path: str) -> str | None:
        try:
            result = self.app.request("fs/getMetadata", {"path": path}, timeout=20)
        except Exception as exc:
            return str(exc)
        if not isinstance(result, dict) or result.get("isDirectory") is not True:
            return "Path is not a directory."
        return None

    def send_session_command_card(
        self,
        bot_key: str,
        bot: TelegramBot,
        chat_id: str,
        message_thread_id: int | None,
        record: dict[str, Any],
    ) -> None:
        bot.send_message(
            chat_id,
            self.render_session_command_card(record),
            message_thread_id=message_thread_id,
            reply_markup=self.session_command_card_keyboard(bot_key),
        )

    def request_session_command_input(
        self,
        bot_key: str,
        bot: TelegramBot,
        user: dict[str, Any],
        chat: dict[str, Any],
        source_message: dict[str, Any],
        action: str,
    ) -> str | None:
        user_id = str(user.get("id", ""))
        chat_id = str(chat.get("id", user_id))
        chat_type = chat.get("type")
        if action == "goal_input":
            label = "new goal text"
            placeholder = "Ship the current fix"
        else:
            label = "working directory path"
            placeholder = "/home/xsling/Code/project"
        first_name = str(user.get("first_name") or user.get("username") or "there")
        prompt = f'<a href="tg://user?id={html.escape(user_id)}">{html.escape(first_name)}</a>, reply with the {label}.'
        sent = bot.send_message(
            chat_id,
            prompt,
            parse_mode="HTML",
            reply_markup={
                "force_reply": True,
                "input_field_placeholder": placeholder,
                "selective": True,
            },
            message_thread_id=source_message.get("message_thread_id") if chat_type != "private" else None,
        )
        result = sent.get("result") if sent else None
        prompt_message_id = result.get("message_id") if isinstance(result, dict) else None
        pending_key = f"{user_id}:{bot_key}"
        self.store.update(
            lambda data: data["pending_session_inputs"].__setitem__(
                pending_key,
                {
                    "bot_key": bot_key,
                    "action": action,
                    "prompt_chat_id": chat_id,
                    "prompt_message_id": prompt_message_id,
                    "message_thread_id": source_message.get("message_thread_id"),
                    "created_at_ms": now_ms(),
                },
            )
        )
        return pending_key

    def handle_pending_session_input(
        self,
        bot_key: str,
        bot: TelegramBot,
        message: dict[str, Any],
        text: str,
        record: dict[str, Any],
    ) -> bool:
        user = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        user_id = str(user.get("id", ""))
        pending_key = f"{user_id}:{bot_key}"
        pending = self.store.snapshot()["pending_session_inputs"].get(pending_key)
        if not isinstance(pending, dict):
            return False
        prompt_chat_id = pending.get("prompt_chat_id")
        if prompt_chat_id and str(chat.get("id", "")) != str(prompt_chat_id):
            return False
        prompt_message_id = pending.get("prompt_message_id")
        if prompt_message_id and chat.get("type") != "private":
            reply_to = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
            if reply_to.get("message_id") != prompt_message_id:
                return False
        message_thread_id = message.get("message_thread_id")
        expected_thread_id = pending.get("message_thread_id")
        if expected_thread_id is not None and message_thread_id is not None:
            try:
                if int(expected_thread_id) != int(message_thread_id):
                    return False
            except (TypeError, ValueError):
                if str(expected_thread_id) != str(message_thread_id):
                    return False
        value = text.strip()
        chat_id = str(chat.get("id", user_id))
        if value.lower() in {"cancel", "/cancel"}:
            self.store.update(lambda data: data["pending_session_inputs"].pop(pending_key, None))
            bot.send_message(chat_id, "Canceled.", message_thread_id=message_thread_id)
            return True
        action = pending.get("action")
        if action == "goal_input":
            self.store.update(lambda data: data["pending_session_inputs"].pop(pending_key, None))
            self.handle_goal_command(bot_key, bot, chat_id, message_thread_id, str(record.get("thread_id") or ""), value)
            return True
        if action == "cwd_input":
            if not value:
                bot.send_message(chat_id, "Path cannot be empty.", message_thread_id=message_thread_id)
                return True
            cwd = normalize_cwd_path(value)
            error = self.validate_cwd_path(cwd)
            if error:
                bot.send_message(chat_id, f"Work dir update failed: {error}", message_thread_id=message_thread_id)
                return True
            self.store.update(lambda data: data["pending_session_inputs"].pop(pending_key, None))
            self.update_bot_setting(bot_key, "cwd", cwd)
            bot.send_message(chat_id, f"Work dir: {display_path(cwd)}", message_thread_id=message_thread_id)
            return True
        return False

    def route_text_for_session(
        self,
        text: str,
        bot_username: str,
        chat_type: str | None,
        message: dict[str, Any],
        record: dict[str, Any],
        bot_key: str,
    ) -> str | None:
        text = text.strip()
        if chat_type == "private":
            return text
        topic_owner = self.topic_owner_for_message(message)
        if topic_owner and topic_owner != bot_key:
            return None
        command, target_username, _ = split_slash_command(text)
        if command:
            if target_username:
                if not bot_username or target_username != bot_username.lower():
                    return None
                return text
            if topic_owner == bot_key:
                return text
            slug = str(record.get("command_slug") or command_slug_from_name(str(record.get("name") or record.get("username") or "codex")))
            if command == slug:
                return text
            return None
        mention = f"@{bot_username}".lower()
        if text.startswith("@") and not text.lower().startswith(mention):
            return None
        if text.lower().startswith(mention):
            return text[len(mention) :].strip()
        if topic_owner == bot_key or self.is_session_thread_reply(message, record, bot_key):
            return text
        return None

    def topic_owner_for_message(self, message: dict[str, Any]) -> str | None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id", ""))
        message_thread_id = message.get("message_thread_id")
        if not chat_id or message_thread_id is None:
            return None
        for bot_key, record in self.store.snapshot().get("bots", {}).items():
            if not isinstance(record, dict):
                continue
            thread_map = record.get("group_message_threads")
            if not isinstance(thread_map, dict):
                continue
            expected = thread_map.get(chat_id)
            if expected is None:
                continue
            try:
                if int(expected) == int(message_thread_id):
                    return str(bot_key)
            except (TypeError, ValueError):
                if str(expected) == str(message_thread_id):
                    return str(bot_key)
        return None

    def message_added_this_bot(self, bot_key: str, message: dict[str, Any]) -> bool:
        members = message.get("new_chat_members")
        if not isinstance(members, list):
            return False
        for member in members:
            if isinstance(member, dict) and str(member.get("id")) == bot_key:
                return True
        return False

    def handle_session_member_update(self, bot_key: str, bot: TelegramBot, update: dict[str, Any]) -> None:
        new_member = update.get("new_chat_member") if isinstance(update.get("new_chat_member"), dict) else {}
        user = new_member.get("user") if isinstance(new_member.get("user"), dict) else {}
        if str(user.get("id", "")) != bot_key:
            return
        status = str(new_member.get("status") or "")
        if status not in {"member", "administrator"}:
            return
        chat = update.get("chat") if isinstance(update.get("chat"), dict) else {}
        actor = update.get("from") if isinstance(update.get("from"), dict) else {}
        if not self.allowed(actor, chat):
            return
        self.send_group_intro(bot_key, bot, chat, None)

    def session_topic_keyboard(self, bot_key: str) -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "Commands", "callback_data": f"cmd:{bot_key}:commands"}]]}

    def send_group_intro(
        self,
        bot_key: str,
        bot: TelegramBot,
        chat: dict[str, Any],
        message_thread_id: int | None,
    ) -> None:
        record = self.store.snapshot()["bots"].get(bot_key)
        if not isinstance(record, dict):
            return
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return
        message_thread_id = self.ensure_group_topic(bot_key, bot, chat, record, message_thread_id)
        if message_thread_id is None:
            return
        intro_key = self.group_intro_key(chat_id, message_thread_id)
        intro_messages = record.get("group_intro_messages")
        if isinstance(intro_messages, dict) and str(intro_messages.get(intro_key, "")):
            return
        sent = bot.send_message(
            chat_id,
            self.group_intro_text(record),
            message_thread_id=message_thread_id,
            reply_markup=self.session_topic_keyboard(bot_key),
        )
        result = sent.get("result") if sent else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            return

        def mutate(data: dict[str, Any]) -> None:
            target = data["bots"].get(bot_key)
            if not isinstance(target, dict):
                return
            target["group_chat_id"] = chat_id
            target.setdefault("group_intro_messages", {})[intro_key] = message_id
            if message_thread_id is not None:
                target.setdefault("group_message_threads", {})[chat_id] = message_thread_id

        self.store.update(mutate)

    def ensure_group_topic(
        self,
        bot_key: str,
        bot: TelegramBot,
        chat: dict[str, Any],
        record: dict[str, Any],
        message_thread_id: int | None,
    ) -> int | None:
        if message_thread_id is not None:
            return message_thread_id
        chat_id = str(chat.get("id", ""))
        thread_map = record.get("group_message_threads")
        if isinstance(thread_map, dict) and thread_map.get(chat_id) is not None:
            try:
                return int(thread_map[chat_id])
            except (TypeError, ValueError):
                pass
        chat_info = bot.get_chat(chat_id) or chat
        if not chat_info.get("is_forum"):
            bot.send_message(
                chat_id,
                "\n".join(
                    [
                        f"@{record.get('username')} requires a topics-enabled supergroup.",
                        "Enable Topics for this group, then add/promote the bot again.",
                    ]
                ),
            )
            return None
        member = bot.get_chat_member(chat_id, bot_key) or {}
        if member.get("status") != "administrator" or member.get("can_manage_topics") is not True:
            bot.send_message(
                chat_id,
                "\n".join(
                    [
                        f"@{record.get('username')} is in this topics group.",
                        "",
                        "Promote this bot to admin with Manage Topics permission to create a dedicated Codex topic.",
                    ]
                ),
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Grant Manage Topics",
                                "url": startgroup_url(record.get("username")) or bot_url(record.get("username")),
                            }
                        ]
                    ]
                },
            )
            return None
        topic = bot.create_forum_topic(chat_id, self.topic_name(record))
        thread_id = topic.get("message_thread_id") if isinstance(topic, dict) else None
        if thread_id is None:
            return None
        try:
            thread_id = int(thread_id)
        except (TypeError, ValueError):
            return None

        def mutate(data: dict[str, Any]) -> None:
            target = data["bots"].get(bot_key)
            if not isinstance(target, dict):
                return
            target["group_chat_id"] = chat_id
            target.setdefault("group_message_threads", {})[chat_id] = thread_id
            target.setdefault("group_topics", {})[chat_id] = {
                "message_thread_id": thread_id,
                "name": topic.get("name") if isinstance(topic, dict) else self.topic_name(record),
            }

        self.store.update(mutate)
        return thread_id

    @staticmethod
    def group_intro_key(chat_id: str, message_thread_id: int | None) -> str:
        if message_thread_id is None:
            raise ValueError("group intro requires a forum topic message_thread_id")
        return f"{chat_id}:{message_thread_id}"

    def is_session_thread_reply(self, message: dict[str, Any], record: dict[str, Any], bot_key: str) -> bool:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return False
        thread_map = record.get("group_message_threads")
        if isinstance(thread_map, dict):
            expected_thread_id = thread_map.get(chat_id)
            if expected_thread_id is not None and message.get("message_thread_id") == expected_thread_id:
                return True
        return False

    def handle_session_command(
        self,
        bot_key: str,
        bot: TelegramBot,
        message: dict[str, Any],
        command: str,
        args: str,
    ) -> None:
        record = self.store.snapshot()["bots"].get(bot_key)
        if not isinstance(record, dict):
            return
        chat_id, thread_id = msg_chat_id(message), record["thread_id"]
        message_thread_id = message.get("message_thread_id")
        slug = str(record.get("command_slug") or command_slug_from_name(str(record.get("name") or record.get("username") or "codex")))
        if command in {"commands", slug}:
            self.send_session_command_card(bot_key, bot, chat_id, message_thread_id, record)
        elif command in {"status", "start", "account", "account_status"}:
            bot.send_message(chat_id, self.render_codex_status(record), message_thread_id=message_thread_id)
        elif command in {"session", "session_status", "bridge", "bridge_status"}:
            bot.send_message(chat_id, self.render_session_status(record), message_thread_id=message_thread_id)
        elif command == "interrupt":
            turn_id = record.get("active_turn_id")
            if not turn_id:
                bot.send_message(chat_id, "No active turn.", message_thread_id=message_thread_id)
                return
            self.app.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=30)
            bot.send_message(
                chat_id,
                "Interrupt sent.\n\nCodex will stop the active turn for this session. Your next message will start or steer the next turn.",
                message_thread_id=message_thread_id,
            )
        elif command == "model":
            if self.task_running(record):
                bot.send_message(chat_id, self.command_requires_idle_message("model"), message_thread_id=message_thread_id)
                return
            value = args.strip()
            if not value:
                bot.send_message(
                    chat_id,
                    self.render_session_choice_card(record, "model"),
                    message_thread_id=message_thread_id,
                    reply_markup=self.session_choice_keyboard(bot_key, "model", str(record.get("model") or "default"), record),
                )
                return
            if value not in self.available_model_values():
                bot.send_message(chat_id, f"Unknown model: {value}. Use /model to pick one.", message_thread_id=message_thread_id)
                return
            normalized_effort = self.normalize_effort_for_model(value, str(record.get("effort") or "default"))
            fast = bool(record.get("fast")) and self.model_supports_fast(value)

            def mutate_model(data: dict[str, Any]) -> None:
                bot_record = data["bots"].get(bot_key)
                if isinstance(bot_record, dict):
                    bot_record.update({"model": value, "effort": normalized_effort, "fast": fast})

            self.store.update(mutate_model)
            bot.send_message(chat_id, f"Model: {value}", message_thread_id=message_thread_id)
        elif command == "effort":
            if self.task_running(record):
                bot.send_message(chat_id, self.command_requires_idle_message("effort"), message_thread_id=message_thread_id)
                return
            value = args.strip()
            if not value:
                bot.send_message(
                    chat_id,
                    self.render_session_choice_card(record, "effort"),
                    message_thread_id=message_thread_id,
                    reply_markup=self.session_choice_keyboard(bot_key, "effort", str(record.get("effort") or "default"), record),
                )
                return
            if value not in self.effort_values_for_model(str(record.get("model") or "default")):
                bot.send_message(chat_id, "Unknown effort for the current model. Use /effort to pick one.", message_thread_id=message_thread_id)
                return
            self.update_bot_setting(bot_key, "effort", value)
            bot.send_message(chat_id, f"Effort: {value}", message_thread_id=message_thread_id)
        elif command == "fast":
            value = args.strip().lower()
            if value in {"", "status"}:
                bot.send_message(chat_id, f"Fast: {'on' if record.get('fast') else 'off'}", message_thread_id=message_thread_id)
                return
            if value not in {"on", "off"}:
                bot.send_message(chat_id, "Usage: /fast [on|off|status]", message_thread_id=message_thread_id)
                return
            if value == "on" and not self.model_supports_fast(str(record.get("model") or "default")):
                bot.send_message(chat_id, "Fast is not available for the current model.", message_thread_id=message_thread_id)
                return
            self.update_bot_setting(bot_key, "fast", value == "on")
            bot.send_message(chat_id, f"Fast: {value}", message_thread_id=message_thread_id)
        elif command == "cwd":
            value = args.strip()
            if not value:
                user = message.get("from") if isinstance(message.get("from"), dict) else {}
                self.send_cwd_browser(bot_key, bot, chat_id, message_thread_id, user, record)
                return
            cwd = normalize_cwd_path(value)
            error = self.validate_cwd_path(cwd)
            if error:
                bot.send_message(chat_id, f"Work dir update failed: {error}", message_thread_id=message_thread_id)
                return
            self.update_bot_setting(bot_key, "cwd", cwd)
            bot.send_message(chat_id, f"Work dir: {display_path(cwd)}", message_thread_id=message_thread_id)
        elif command == "goal":
            self.handle_goal_command(bot_key, bot, chat_id, message_thread_id, thread_id, args)
        elif command == "compact":
            if self.task_running(record):
                bot.send_message(chat_id, self.command_requires_idle_message("compact"), message_thread_id=message_thread_id)
                return
            self.app.request("thread/compact/start", {"threadId": thread_id}, timeout=60)
            bot.send_message(chat_id, "Compaction started.", message_thread_id=message_thread_id)
        elif command == "review":
            if self.task_running(record):
                bot.send_message(chat_id, self.command_requires_idle_message("review"), message_thread_id=message_thread_id)
                return
            target = {"type": "custom", "instructions": args.strip()} if args.strip() else {"type": "uncommittedChanges"}
            result = self.app.request("review/start", {"threadId": thread_id, "target": target}, timeout=60)
            review_thread_id = result.get("reviewThreadId") if isinstance(result, dict) else None
            target_label = "custom instructions" if args.strip() else "uncommitted changes"
            bot.send_message(
                chat_id,
                "\n".join(
                    [
                        "Review started.",
                        "",
                        f"Target: {target_label}",
                        f"Review thread: {review_thread_id or thread_id}",
                        "Findings will stream back here as Codex output.",
                    ]
                ),
                message_thread_id=message_thread_id,
            )
        elif command == "plan":
            if self.task_running(record):
                bot.send_message(chat_id, self.command_requires_idle_message("plan"), message_thread_id=message_thread_id)
                return
            value = args.strip().lower()
            if value in {"status", "?"}:
                bot.send_message(chat_id, plan_mode_message(bool(record.get("plan_mode"))), message_thread_id=message_thread_id)
                return
            if value in {"on", "true", "1"}:
                enabled = True
            elif value in {"off", "false", "0"}:
                enabled = False
            elif not value:
                enabled = True
            else:
                self.update_bot_setting(bot_key, "plan_mode", True)
                bot.send_message(chat_id, plan_mode_message(True), message_thread_id=message_thread_id)
                self.start_or_steer_turn(bot_key, bot, message, args.strip())
                return
            self.update_bot_setting(bot_key, "plan_mode", enabled)
            bot.send_message(chat_id, plan_mode_message(enabled), message_thread_id=message_thread_id)
        else:
            bot.send_message(chat_id, "Unknown command. Try /status.", message_thread_id=message_thread_id)

    def update_bot_setting(self, bot_key: str, key: str, value: Any) -> None:
        self.store.update(lambda data: data["bots"][bot_key].update({key: value}))

    def read_thread_goal(self, thread_id: str) -> Any:
        result = self.app.request("thread/goal/get", {"threadId": thread_id}, timeout=30)
        return result.get("goal") if isinstance(result, dict) else result

    def send_goal_replace_confirmation(
        self,
        bot_key: str,
        bot: TelegramBot,
        chat_id: str,
        message_thread_id: int | None,
        thread_id: str,
        current_goal: dict[str, Any],
        objective: str,
    ) -> None:
        pending_id = rand_suffix(10)
        self.store.update(
            lambda data: data["pending_goal_replacements"].__setitem__(
                pending_id,
                {
                    "bot_key": bot_key,
                    "thread_id": thread_id,
                    "objective": objective,
                    "created_at_ms": now_ms(),
                },
            )
        )
        current = truncate_middle(str(current_goal.get("objective") or ""), 700)
        new = truncate_middle(objective, 700)
        bot.send_message(
            chat_id,
            "\n".join(["Replace current goal?", "", f"Current: {current}", "", f"New: {new}"]),
            message_thread_id=message_thread_id,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Replace current goal", "callback_data": f"cmd:{bot_key}:goal_replace:{pending_id}"}],
                    [{"text": "Cancel", "callback_data": f"cmd:{bot_key}:goal_cancel:{pending_id}"}],
                ]
            },
        )

    def handle_goal_command(
        self,
        bot_key: str,
        bot: TelegramBot,
        chat_id: str,
        message_thread_id: int | None,
        thread_id: str,
        args: str,
    ) -> None:
        arg = args.strip()
        try:
            if not arg:
                goal = self.read_thread_goal(thread_id)
                bot.send_message(chat_id, render_goal(goal), message_thread_id=message_thread_id)
            elif arg.lower() == "clear":
                result = self.app.request("thread/goal/clear", {"threadId": thread_id}, timeout=30)
                cleared = result.get("cleared") if isinstance(result, dict) else None
                if cleared:
                    bot.send_message(chat_id, "Goal cleared.", message_thread_id=message_thread_id)
                else:
                    bot.send_message(chat_id, "No goal to clear.", message_thread_id=message_thread_id)
            elif arg.lower() in {"pause", "resume"}:
                status = "paused" if arg.lower() == "pause" else "active"
                result = self.app.request(
                    "thread/goal/set",
                    {"threadId": thread_id, "status": status},
                    timeout=30,
                )
                goal = result.get("goal") if isinstance(result, dict) else result
                bot.send_message(chat_id, render_goal(goal), message_thread_id=message_thread_id)
            elif arg.lower() == "edit":
                bot.send_message(
                    chat_id,
                    "Use /goal <new objective> to edit the current goal.",
                    message_thread_id=message_thread_id,
                )
            else:
                current_goal = self.read_thread_goal(thread_id)
                if isinstance(current_goal, dict):
                    self.send_goal_replace_confirmation(bot_key, bot, chat_id, message_thread_id, thread_id, current_goal, arg)
                    return
                result = self.app.request(
                    "thread/goal/set",
                    {"threadId": thread_id, "objective": arg, "status": "active"},
                    timeout=30,
                )
                goal = result.get("goal") if isinstance(result, dict) else result
                bot.send_message(chat_id, render_goal(goal) or "Goal set.", message_thread_id=message_thread_id)
        except Exception as exc:
            bot.send_message(chat_id, f"Goal command failed: {exc}", message_thread_id=message_thread_id)

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

    def plan_collaboration_mode(self, record: dict[str, Any]) -> dict[str, Any]:
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
        model = str(record.get("model") or "default")
        if model == "default":
            model = str(mask.get("model") or self.resolve_default_model_id())
        effort = str(record.get("effort") or "default")
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

    def start_or_steer_turn(self, bot_key: str, bot: TelegramBot, message: dict[str, Any], text: str) -> None:
        record = self.store.snapshot()["bots"].get(bot_key)
        if not isinstance(record, dict):
            return
        chat_id = msg_chat_id(message)
        message_thread_id = message.get("message_thread_id")
        thread_id = record["thread_id"]
        bot.send_typing(chat_id, message_thread_id)
        created_placeholder = False
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
        if output is None:
            output_msg = bot.send_message(chat_id, "…", message_thread_id=message_thread_id)
            output_result = output_msg.get("result") if output_msg else None
            output_message_id = output_result.get("message_id") if isinstance(output_result, dict) else None
            if not output_message_id:
                return
            created_placeholder = True
            with self.runtime_lock:
                self.output_by_thread[thread_id] = TurnOutputState(
                    bot_key=bot_key,
                    chat_id=chat_id,
                    pending_message_id=int(output_message_id),
                    message_thread_id=message_thread_id,
                    turn_id=str(record.get("active_turn_id") or ""),
                    pending_last_edit_at=time.monotonic(),
                    next_typing_at=0.0,
                )
        else:
            output_message_id = output.pending_message_id
        params = {
            "threadId": thread_id,
            "input": text_input(text),
        }
        try:
            self.ensure_thread_loaded(record)
            record = self.store.snapshot()["bots"].get(bot_key)
            if not isinstance(record, dict):
                return
            active_turn_id = record.get("active_turn_id")
            if active_turn_id:
                params["expectedTurnId"] = active_turn_id
                result = self.app.request("turn/steer", params, timeout=60)
                turn = result.get("turn") if isinstance(result, dict) else None
                turn_id = active_turn_id
                if isinstance(turn, dict) and turn.get("id"):
                    turn_id = turn["id"]
            else:
                model = record.get("model")
                effort = record.get("effort")
                cwd = record.get("cwd")
                params["serviceTier"] = self.fast_service_tier_for_model(str(model or "default")) if record.get("fast") else None
                if record.get("plan_mode"):
                    params["collaborationMode"] = self.plan_collaboration_mode(record)
                elif model and model != "default":
                    params["model"] = model
                if not record.get("plan_mode") and effort and effort != "default":
                    params["effort"] = effort
                if cwd:
                    params["cwd"] = cwd
                result = self.app.request("turn/start", params, timeout=60)
                turn = result.get("turn") if isinstance(result, dict) else None
                turn_id = turn.get("id") if isinstance(turn, dict) else None
        except Exception as exc:
            if created_placeholder:
                with self.runtime_lock:
                    self.output_by_thread.pop(thread_id, None)
                bot.edit_message_text(chat_id, int(output_message_id), f"Codex request failed: {exc}")
            else:
                bot.send_message(chat_id, f"Codex request failed: {exc}", message_thread_id=message_thread_id)
            return
        if not turn_id:
            if created_placeholder:
                with self.runtime_lock:
                    self.output_by_thread.pop(thread_id, None)
                bot.edit_message_text(chat_id, int(output_message_id), "Codex did not return a turn id.")
            else:
                bot.send_message(chat_id, "Codex did not return a turn id.", message_thread_id=message_thread_id)
            return
        self.store.update(lambda data: data["bots"][bot_key].update({"active_turn_id": turn_id}))
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if output:
                output.turn_id = str(turn_id)

    def ensure_thread_loaded(self, record: dict[str, Any]) -> None:
        thread_id = str(record.get("thread_id") or "")
        if not thread_id:
            return
        with self.runtime_lock:
            if thread_id in self.loaded_threads:
                return
        try:
            result = self.app.request("thread/resume", {"threadId": thread_id}, timeout=60)
        except Exception as exc:
            log(f"thread/resume failed for {thread_id}: {exc}")
            raise
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

    def render_codex_status(self, record: dict[str, Any]) -> str:
        lines = ["Account", ""]
        try:
            account_result = self.app.request("account/read", {}, timeout=30)
            account = account_result.get("account") if isinstance(account_result, dict) else None
            if isinstance(account, dict):
                account_type = account.get("type") or "unknown"
                lines.append(f"Account: {account_type}")
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
                name = snapshot.get("limitName") or snapshot.get("limitId") or "codex"
                lines += ["", "Usage limits:"]
                if str(name).lower() != "codex":
                    lines.append(f"{name} limit")
                if snapshot.get("rateLimitReachedType"):
                    lines.append(format_rate_limit_reached(snapshot.get("rateLimitReachedType")))
                if append_rate_limit_rows(lines, snapshot) == 0:
                    lines.append("Limits: not available for this account")
            else:
                lines += ["", "Usage limits: data not available yet"]
        except Exception as exc:
            lines += ["", f"Usage limits: error: {exc}"]
        return "\n".join(lines)

    def render_session_status(self, record: dict[str, Any]) -> str:
        slug = record.get("command_slug") or command_slug_from_name(str(record.get("name") or record.get("username") or "codex"))
        thread_status = "unknown"
        active_flags: list[Any] = []
        try:
            thread_result = self.app.request("thread/read", {"threadId": record.get("thread_id"), "includeTurns": False}, timeout=30)
            thread = thread_result.get("thread") if isinstance(thread_result, dict) else None
            if isinstance(thread, dict):
                status = thread.get("status") if isinstance(thread.get("status"), dict) else {}
                thread_status = str(status.get("type") or "unknown")
                flags = status.get("activeFlags")
                if isinstance(flags, list):
                    active_flags = flags
        except Exception as exc:
            thread_status = f"error: {exc}"
        lines = [
            "Session",
            "",
            f"Bot: @{record.get('username')}",
            f"Command: /{slug}",
            f"Thread: {record.get('thread_id')}",
            f"Thread status: {thread_status}",
            f"Codex session: {record.get('session_id')}",
            "",
            "Settings:",
            f"Model: {record.get('model') or 'default'}",
            f"Effort: {record.get('effort') or 'default'}",
            f"Fast: {'on' if record.get('fast') else 'off'}",
            f"Plan mode: {'ON' if record.get('plan_mode') else 'off'}",
            f"Approval: {approval_label(record.get('approval'))}",
            f"Work dir: {display_path(record.get('cwd') or 'default')}",
            f"Active turn: {record.get('active_turn_id') or 'none'}",
        ]
        if active_flags:
            lines.append(f"Active flags: {', '.join(str(flag) for flag in active_flags)}")
        return "\n".join(
            lines
        )

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

    def set_active_turn_by_thread(self, thread_id: str, turn_id: str | None) -> None:
        def mutate(data: dict[str, Any]) -> None:
            for record in data["bots"].values():
                if record.get("thread_id") == thread_id:
                    record["active_turn_id"] = turn_id

        self.store.update(mutate)

    def set_output_turn_id(self, thread_id: str, turn_id: str) -> None:
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if output:
                output.turn_id = turn_id

    def find_output_by_turn_id(self, turn_id: str) -> TurnOutputState | None:
        for output in self.output_by_thread.values():
            if output.turn_id == turn_id:
                return output
        return None

    def ensure_agent_message_output(
        self,
        thread_id: str,
        turn_id: str,
        item_id: str,
    ) -> AgentMessageOutput | None:
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
                message = AgentMessageOutput(
                    item_id=item_id,
                    message_id=output.pending_message_id,
                    last_edit_at=output.pending_last_edit_at,
                    last_rendered_text=output.pending_text,
                    placeholder_text=output.pending_text,
                    placeholder_step=output.pending_step,
                )
            else:
                bot = self.session_bots.get(output.bot_key)
                if not bot:
                    return None
                sent = bot.send_message(output.chat_id, "…", message_thread_id=output.message_thread_id)
                result = sent.get("result") if sent else None
                message_id = result.get("message_id") if isinstance(result, dict) else None
                if not message_id:
                    return None
                message = AgentMessageOutput(item_id=item_id, message_id=int(message_id), last_edit_at=time.monotonic())
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
            bot = self.session_bots.get(output.bot_key)
            if not bot:
                return
            if now >= output.next_typing_at:
                bot.send_typing(output.chat_id, output.message_thread_id)
                output.next_typing_at = now + TELEGRAM_TYPING_INTERVAL
            if was_empty or now - message.last_edit_at >= TELEGRAM_EDIT_INTERVAL:
                self.sync_output_message(bot, output, message)

    def start_status_item(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = str(item.get("id") or "")
        if not item_id:
            return
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
            if item_id in output.status_items:
                return
            output.status_items[item_id] = status
            output.status_order.append(item_id)
            bot = self.session_bots.get(output.bot_key)
            if bot:
                self.sync_activity_panel(bot, output)

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
            status = output.status_items.get(item_id)
            bot = self.session_bots.get(output.bot_key)
            if not bot:
                return
            output_text = status.output_text if status else ""
            completed_status = self.build_status_item(item, completed=True, output_text=output_text)
            if not completed_status:
                return
            if status:
                status.completed = True
                status.status = completed_status.status
                status.failed = completed_status.failed
                status.label = completed_status.label
                status.detail = completed_status.detail
                status.output_text = completed_status.output_text
            else:
                output.status_items[item_id] = completed_status
                output.status_order.append(item_id)
            self.sync_activity_panel(bot, output)

    def build_status_item(self, item: dict[str, Any], *, completed: bool, output_text: str) -> StatusItemOutput | None:
        item_id = str(item.get("id") or "")
        if not item_id:
            return None
        item_type = item.get("type")
        if item_type == "reasoning":
            return None
        if item_type == "commandExecution":
            command = str(item.get("command") or "").strip()
            status = str(item.get("status") or ("completed" if completed else "inProgress"))
            exit_code = item.get("exitCode")
            aggregated = str(item.get("aggregatedOutput") or output_text or "").strip()
            failed = completed and (status in {"failed", "declined"} or (exit_code not in (None, 0)))
            detail_lines = [f"command: {command or '(command)'}", f"status: {status}"]
            if exit_code is not None:
                detail_lines.append(f"exit: {exit_code}")
            if aggregated:
                detail_lines.extend(["output:", truncate_middle(aggregated, 1800)])
            return StatusItemOutput(
                item_id=item_id,
                item_type="command",
                label=command or "(command)",
                status=status,
                output_text=aggregated,
                completed=completed,
                failed=failed,
                detail="\n".join(detail_lines),
            )
        if item_type == "webSearch":
            query = str(item.get("query") or "").strip()
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            url = str(action.get("url") or "").strip()
            target = query or url or "(search)"
            return StatusItemOutput(
                item_id=item_id,
                item_type="search",
                label=target,
                status="completed" if completed else "inProgress",
                completed=completed,
                detail=f"web search: {target}",
            )
        if item_type == "dynamicToolCall":
            namespace = str(item.get("namespace") or "").strip()
            tool = str(item.get("tool") or "").strip()
            status = str(item.get("status") or ("completed" if completed else "inProgress"))
            name = f"{namespace}.{tool}" if namespace else tool or "tool"
            success = item.get("success")
            failed = completed and (status == "failed" or success is False)
            return StatusItemOutput(
                item_id=item_id,
                item_type="tool",
                label=name,
                status=status,
                completed=completed,
                failed=failed,
                detail=f"tool: {name}\nstatus: {status}",
            )
        if item_type == "mcpToolCall":
            server = str(item.get("server") or "").strip()
            tool = str(item.get("tool") or "").strip()
            status = str(item.get("status") or ("completed" if completed else "inProgress"))
            name = f"{server}.{tool}" if server else tool or "mcp tool"
            failed = completed and status == "failed"
            return StatusItemOutput(
                item_id=item_id,
                item_type="mcp",
                label=name,
                status=status,
                completed=completed,
                failed=failed,
                detail=f"mcp: {name}\nstatus: {status}",
            )
        if item_type == "fileChange":
            status = str(item.get("status") or ("completed" if completed else "inProgress"))
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            failed = completed and status not in {"completed", "success"}
            return StatusItemOutput(
                item_id=item_id,
                item_type="file",
                label=f"{len(changes)} change(s)",
                status=status,
                completed=completed,
                failed=failed,
                detail=f"file change: {status}\n{len(changes)} change(s)",
            )
        if item_type == "plan":
            text = str(item.get("text") or "").strip()
            return StatusItemOutput(
                item_id=item_id,
                item_type="plan",
                label=text or "plan",
                status="completed" if completed else "inProgress",
                completed=completed,
                detail=text,
            )
        if item_type == "collabAgentToolCall":
            tool = str(item.get("tool") or "agent").strip()
            status = str(item.get("status") or ("completed" if completed else "inProgress"))
            failed = completed and status == "failed"
            return StatusItemOutput(
                item_id=item_id,
                item_type="agent",
                label=tool,
                status=status,
                completed=completed,
                failed=failed,
                detail=f"agent tool: {tool}\nstatus: {status}",
            )
        return None

    def sync_activity_panel(self, bot: TelegramBot, output: TurnOutputState, *, force: bool = False) -> None:
        if output.activity_hidden:
            return
        text, markup = self.render_activity_panel(output)
        if not text or text == output.activity_last_rendered_text:
            output.activity_dirty = False
            return
        now = time.monotonic()
        if output.activity_message_id is not None and not force and now - output.activity_last_edit_at < ACTIVITY_EDIT_INTERVAL:
            output.activity_dirty = True
            return
        if output.activity_message_id is None:
            sent = bot.send_message(output.chat_id, text, message_thread_id=output.message_thread_id, reply_markup=markup)
            result = sent.get("result") if sent else None
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if message_id is None:
                return
            output.activity_message_id = int(message_id)
        else:
            bot.edit_message_text(output.chat_id, output.activity_message_id, text, reply_markup=markup)
        output.activity_last_rendered_text = text
        output.activity_last_edit_at = now
        output.activity_dirty = False

    def render_activity_panel(self, output: TurnOutputState) -> tuple[str, dict[str, Any] | None]:
        if output.activity_view == "details":
            pages = self.render_activity_detail_pages(output)
            if not pages:
                output.activity_view = "summary"
            else:
                page = min(max(0, output.activity_page), len(pages) - 1)
                output.activity_page = page
                return pages[page], self.activity_keyboard(output.turn_id, view="details", page=page, page_count=len(pages))
        return self.render_activity_summary(output), self.activity_keyboard(output.turn_id, view="summary", page=0, page_count=1)

    def render_activity_summary(self, output: TurnOutputState) -> str:
        items = list(output.status_items.values())
        if not items:
            return ""
        counts: dict[str, int] = {}
        running: list[StatusItemOutput] = []
        failures: list[StatusItemOutput] = []
        for item in items:
            counts[item.item_type] = counts.get(item.item_type, 0) + 1
            if not item.completed:
                running.append(item)
            if item.failed:
                failures.append(item)
        done = sum(1 for item in items if item.completed)
        lines = [f"Activity: {done}/{len(items)} done"]
        type_labels = [
            ("command", "commands"),
            ("search", "searches"),
            ("tool", "tools"),
            ("mcp", "mcp"),
            ("file", "files"),
            ("plan", "plans"),
            ("agent", "agents"),
        ]
        parts = [f"{label} {counts[key]}" for key, label in type_labels if counts.get(key)]
        if parts:
            lines.append(", ".join(parts))
        if running:
            latest = running[-1]
            lines.append(f"Running: {self.compact_label(latest.label)}")
        elif items:
            latest = items[-1]
            lines.append(f"Latest: {self.compact_label(latest.label)}")
        if failures:
            lines.append(f"Failures: {len(failures)}")
            for failed in failures[:3]:
                lines.append(f"- {self.compact_label(failed.label, 120)}")
        return "\n".join(lines)

    def render_activity_callback_view(
        self,
        turn_id: str,
        *,
        view: str,
        page: int,
    ) -> tuple[str, dict[str, Any] | None] | None:
        with self.runtime_lock:
            output = self.find_output_by_turn_id(turn_id)
            if output:
                output.activity_view = view
                output.activity_page = page
                text, markup = self.render_activity_panel(output)
                output.activity_last_rendered_text = text
                output.activity_last_edit_at = time.monotonic()
                output.activity_dirty = False
                return text, markup
            cached = self.activity_details_by_turn.get(turn_id)
            if cached:
                summary, pages = cached[3], cached[4]
                if view == "summary":
                    return summary, self.activity_keyboard(turn_id, view="summary", page=0, page_count=max(1, len(pages)))
                if not pages:
                    return None
                safe_page = min(max(0, page), len(pages) - 1)
                return pages[safe_page], self.activity_keyboard(turn_id, view="details", page=safe_page, page_count=len(pages))
        return None

    def render_activity_detail_pages(self, output: TurnOutputState) -> list[str]:
        lines = ["Activity details", ""]
        items = [output.status_items[item_id] for item_id in output.status_order if item_id in output.status_items]
        for index, item in enumerate(items[:ACTIVITY_DETAIL_MAX_ITEMS], 1):
            marker = "failed" if item.failed else ("done" if item.completed else "running")
            lines.append(f"{index}. {item.item_type} {marker}")
            detail = item.detail or item.label
            if detail:
                lines.append(truncate_middle(detail, 1000))
            lines.append("")
        if len(items) > ACTIVITY_DETAIL_MAX_ITEMS:
            lines.append(f"... {len(items) - ACTIVITY_DETAIL_MAX_ITEMS} more item(s) omitted")
        return chunk_text("\n".join(lines).strip(), ACTIVITY_DETAIL_PAGE_LIMIT)

    @staticmethod
    def activity_keyboard(turn_id: str, *, view: str, page: int, page_count: int) -> dict[str, Any] | None:
        if not turn_id:
            return None
        if view == "details":
            rows: list[list[dict[str, Any]]] = []
            nav: list[dict[str, Any]] = []
            if page > 0:
                nav.append({"text": "Prev", "callback_data": f"a:page:{turn_id}:{page - 1}"})
            nav.append({"text": f"{page + 1}/{max(1, page_count)}", "callback_data": f"a:page:{turn_id}:{page}"})
            if page + 1 < page_count:
                nav.append({"text": "Next", "callback_data": f"a:page:{turn_id}:{page + 1}"})
            rows.append(nav)
            rows.append(
                [
                    {"text": "Summary", "callback_data": f"a:summary:{turn_id}"},
                    {"text": "Hide", "callback_data": f"a:hide:{turn_id}"},
                ]
            )
            return {"inline_keyboard": rows}
        return {
            "inline_keyboard": [
                [
                    {"text": "Details", "callback_data": f"a:details:{turn_id}"},
                    {"text": "Hide", "callback_data": f"a:hide:{turn_id}"},
                ]
            ]
        }

    @staticmethod
    def compact_label(text: str, limit: int = 160) -> str:
        text = " ".join(str(text).split())
        return truncate_middle(text, limit)

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
            bot = self.session_bots.get(output.bot_key)
            if bot:
                self.sync_output_message(bot, output, message)

    def flush_output(self, thread_id: str, *, final: bool) -> None:
        with self.runtime_lock:
            output = self.output_by_thread.get(thread_id)
            if not output:
                return
            bot = self.session_bots.get(output.bot_key)
            if bot:
                if output.status_items:
                    self.sync_activity_panel(bot, output, force=True)
                if output.pending_active and not output.messages:
                    bot.edit_message_text(output.chat_id, output.pending_message_id, "Codex completed without an assistant message.")
                for item_id in output.message_order:
                    message = output.messages.get(item_id)
                    if message:
                        self.sync_output_message(bot, output, message)
            if final:
                if output.turn_id and output.status_items:
                    self.activity_details_by_turn[output.turn_id] = (
                        output.bot_key,
                        output.chat_id,
                        output.message_thread_id,
                        self.render_activity_summary(output),
                        self.render_activity_detail_pages(output),
                    )
                self.output_by_thread.pop(thread_id, None)

    def sync_output_message(self, bot: TelegramBot, output: TurnOutputState, message: AgentMessageOutput) -> None:
        text = message.text.strip() or message.placeholder_text
        chunks = chunk_text(text, TELEGRAM_EDIT_LIMIT)
        first_chunk = chunks[0] if chunks else " "
        if first_chunk != message.last_rendered_text:
            bot.edit_message_text(output.chat_id, message.message_id, first_chunk)
            message.last_rendered_text = first_chunk
            message.last_edit_at = time.monotonic()
        if not message.completed:
            return
        for chunk in chunks[1 + len(message.extra_message_ids) :]:
            sent = bot.send_message(output.chat_id, chunk, message_thread_id=output.message_thread_id)
            result = sent.get("result") if sent else None
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not message_id:
                break
            message.extra_message_ids.append(int(message_id))

    def placeholder_animation_loop(self) -> None:
        while True:
            time.sleep(TELEGRAM_PLACEHOLDER_INTERVAL)
            edits: list[tuple[TelegramBot, str, int, str]] = []
            now = time.monotonic()
            with self.runtime_lock:
                for output in self.output_by_thread.values():
                    bot = self.session_bots.get(output.bot_key)
                    if not bot:
                        continue
                    if output.activity_dirty:
                        self.sync_activity_panel(bot, output)
                    if output.pending_active:
                        output.pending_step = output.pending_step % TELEGRAM_PLACEHOLDER_MAX_STEPS + 1
                        output.pending_text = "…" * output.pending_step
                        output.pending_last_edit_at = now
                        edits.append((bot, output.chat_id, output.pending_message_id, output.pending_text))
                    for item_id in output.message_order:
                        message = output.messages.get(item_id)
                        if not message or message.text or message.completed:
                            continue
                        if now - message.last_edit_at < TELEGRAM_PLACEHOLDER_INTERVAL:
                            continue
                        message.placeholder_step = message.placeholder_step % TELEGRAM_PLACEHOLDER_MAX_STEPS + 1
                        message.placeholder_text = "…" * message.placeholder_step
                        message.last_edit_at = now
                        if message.placeholder_text != message.last_rendered_text:
                            message.last_rendered_text = message.placeholder_text
                            edits.append((bot, output.chat_id, message.message_id, message.placeholder_text))
            for bot, chat_id, message_id, text in edits:
                bot.edit_message_text(chat_id, message_id, text)


def msg_chat_id(message: dict[str, Any]) -> str:
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    return str(chat.get("id", ""))


def split_slash_command(text: str) -> tuple[str | None, str | None, str]:
    text = text.strip()
    if not text.startswith("/"):
        return None, None, text
    token, _, args = text.partition(" ")
    token = token[1:]
    if not token:
        return None, None, args.strip()
    target = None
    if "@" in token:
        token, target = token.split("@", 1)
        target = target.lower()
    command = token.replace("-", "_").lower()
    return command, target, args.strip()


def parse_command(text: str, bot_username: str) -> tuple[str | None, str]:
    text = text.strip()
    if not text.startswith("/"):
        return None, text
    command, target, args = split_slash_command(text)
    if target and bot_username and target != bot_username.lower():
        return None, text
    return command, args


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram multi-bot bridge backed by codex app-server.")
    parser.add_argument("--manager-token", default="", help="manager bot token; defaults to TELEGRAM_MANAGER_BOT_TOKEN")
    parser.add_argument("--bridge-home", default=os.environ.get("BRIDGE_HOME", DEFAULT_HOME))
    parser.add_argument("--telegram-timeout", type=int, default=env_int("TELEGRAM_TIMEOUT", 30))
    parser.add_argument("--authorized-user-ids", default="", help="comma-separated Telegram user ids allowed to control the manager/session bots")
    parser.add_argument("--authorized-chat-ids", default="", help="comma-separated Telegram chat ids allowed to use the bridge")
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
    bridge = MultiBotBridge(args)
    try:
        bridge.run()
    except KeyboardInterrupt:
        bridge.app.stop()
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
