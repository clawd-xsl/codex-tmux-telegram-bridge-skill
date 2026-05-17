#!/usr/bin/env python3
"""Telegram <-> Codex tmux bridge.

Inbound:
  Long-poll Telegram and paste authorized text into a Codex tmux pane.

Outbound:
  Tail the latest Codex rollout JSONL under CODEX_SESSIONS and forward only
  structured assistant messages and structured errors to Telegram.

Run `python3 scripts/bridge.py --print-chat-ids` after messaging the bot
to discover chat ids without starting the bridge.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer")


def env_int_alias(primary: str, fallback: str, default: int) -> int:
    if os.environ.get(primary) not in (None, ""):
        return env_int(primary, default)
    return env_int(fallback, default)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number")


def env_float_alias(primary: str, fallback: str, default: float) -> float:
    if os.environ.get(primary) not in (None, ""):
        return env_float(primary, default)
    return env_float(fallback, default)


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("AUTHORIZED_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
TMUX_TARGET = os.environ.get("TMUX_TARGET", "codex-guardian:codex")
SESSIONS_DIR = os.path.expanduser(os.environ.get("CODEX_SESSIONS", "~/.codex/sessions"))
POLL_MS = env_int("POLL_MS", 500)
TG_TIMEOUT = env_int_alias("TG_TIMEOUT", "TELEGRAM_TIMEOUT", 30)
TYPING_INTERVAL = env_float_alias("TYPING_INTERVAL", "TELEGRAM_TYPING_INTERVAL", 4.0)
TELEGRAM_CHUNK_LIMIT = env_int("TELEGRAM_CHUNK_LIMIT", 4000)
API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""


def log(message: str) -> None:
    print(f"[bridge] {message}", file=sys.stderr, flush=True)


def require_token() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")


def require_chat_id() -> None:
    if not CHAT_ID:
        raise SystemExit("AUTHORIZED_CHAT_ID or TELEGRAM_CHAT_ID is required")


def tg_call(method: str, **kwargs: str) -> dict[str, Any] | None:
    require_token()
    url = f"{API}/{method}"
    data = urllib.parse.urlencode(kwargs).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=TG_TIMEOUT + 30) as response:
            payload = json.loads(response.read())
            if isinstance(payload, dict):
                return payload
            log(f"telegram {method} returned non-object payload")
    except urllib.error.HTTPError as exc:
        log(f"telegram {method} HTTP {exc.code}")
    except Exception as exc:
        log(f"telegram {method} error: {exc}")
    return None


def send_message(text: str) -> None:
    if not text or not CHAT_ID:
        return
    remaining = text
    while remaining:
        chunk = remaining[:TELEGRAM_CHUNK_LIMIT]
        remaining = remaining[TELEGRAM_CHUNK_LIMIT:]
        tg_call(
            "sendMessage",
            chat_id=str(CHAT_ID),
            text=chunk,
            disable_web_page_preview="true",
        )


def send_typing() -> None:
    if CHAT_ID:
        tg_call("sendChatAction", chat_id=str(CHAT_ID), action="typing")


def tmux(args: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["tmux", *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def tmux_target_exists() -> bool:
    result = tmux(["display-message", "-p", "-t", TMUX_TARGET, "#{pane_id}"])
    if result.returncode != 0:
        log(f"tmux target check failed: {result.stderr.decode(errors='replace').strip()}")
        return False
    return True


def send_to_codex(text: str) -> bool:
    if not text:
        return False
    buffer_name = "codex-bridge"
    load = tmux(["load-buffer", "-b", buffer_name, "-"], input_bytes=text.encode("utf-8"))
    if load.returncode != 0:
        log(f"tmux load-buffer failed: {load.stderr.decode(errors='replace').strip()}")
        return False
    paste = tmux(["paste-buffer", "-d", "-b", buffer_name, "-t", TMUX_TARGET])
    if paste.returncode != 0:
        log(f"tmux paste-buffer failed: {paste.stderr.decode(errors='replace').strip()}")
        return False
    time.sleep(0.25)
    enter = tmux(["send-keys", "-t", TMUX_TARGET, "Enter"])
    if enter.returncode != 0:
        log(f"tmux send-keys failed: {enter.stderr.decode(errors='replace').strip()}")
        return False
    return True


def find_latest_rollout() -> str | None:
    if not os.path.isdir(SESSIONS_DIR):
        return None
    latest = None
    latest_mtime = 0.0
    for root, _, files in os.walk(SESSIONS_DIR):
        for filename in files:
            if not filename.endswith(".jsonl"):
                continue
            path = os.path.join(root, filename)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = path
    return latest


def assistant_text(payload: dict[str, Any]) -> str | None:
    if payload.get("type") != "message" or payload.get("role") != "assistant":
        return None
    parts = []
    for content in payload.get("content", []) or []:
        if not isinstance(content, dict):
            continue
        if content.get("type") in ("output_text", "text"):
            parts.append(content.get("text") or "")
    text = "\n".join(part for part in parts if part).strip()
    return text or None


def format_event(record: dict[str, Any]) -> str | None:
    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        return None

    if record.get("type") == "response_item":
        return assistant_text(payload)

    if record.get("type") == "event_msg":
        event_type = payload.get("type")
        if event_type == "error":
            return f"codex error: {payload.get('message') or payload}"
        if event_type == "stream_error":
            return f"codex stream error: {payload.get('message') or payload}"

    return None


def starts_response(record: dict[str, Any]) -> bool:
    payload = record.get("payload") or {}
    return record.get("type") == "event_msg" and payload.get("type") == "task_started"


def stops_response(record: dict[str, Any]) -> bool:
    payload = record.get("payload") or {}
    return record.get("type") == "event_msg" and payload.get("type") in {
        "error",
        "stream_error",
        "task_complete",
        "turn_aborted",
    }


def parse_jsonl_lines(partial: bytes, chunk: bytes) -> tuple[bytes, list[dict[str, Any]]]:
    partial += chunk
    lines = partial.split(b"\n")
    records: list[dict[str, Any]] = []
    for raw in lines[:-1]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except Exception:
            continue
        if isinstance(record, dict):
            records.append(record)
    return lines[-1], records


def tail_rollouts() -> None:
    current_path = None
    file_handle = None
    partial = b""
    last_announced = None
    typing_active = False
    next_typing_at = 0.0

    def maybe_send_typing() -> None:
        nonlocal next_typing_at
        now = time.monotonic()
        if now < next_typing_at:
            return
        send_typing()
        next_typing_at = now + TYPING_INTERVAL

    while True:
        try:
            latest = find_latest_rollout()
            if latest != current_path:
                if file_handle:
                    file_handle.close()
                file_handle = None
                current_path = latest
                partial = b""
                if current_path:
                    file_handle = open(current_path, "rb")
                    file_handle.seek(0, os.SEEK_END)
                    if last_announced != current_path:
                        last_announced = current_path
                        log(f"tailing {current_path}")

            if file_handle:
                chunk = file_handle.read(65536)
                if chunk:
                    partial, records = parse_jsonl_lines(partial, chunk)
                    for record in records:
                        if starts_response(record):
                            typing_active = True
                            next_typing_at = 0.0
                            maybe_send_typing()
                        text = format_event(record)
                        if text:
                            send_message(text)
                            typing_active = False
                        elif stops_response(record):
                            typing_active = False
                    if typing_active:
                        maybe_send_typing()
                    continue

            if typing_active:
                maybe_send_typing()
            time.sleep(POLL_MS / 1000.0)
        except Exception as exc:
            log(f"tailer error: {exc}")
            typing_active = False
            time.sleep(1)


def tg_get_updates(offset: int | None = None) -> dict[str, Any] | None:
    require_token()
    params = {"timeout": str(TG_TIMEOUT), "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = str(offset)
    url = f"{API}/getUpdates?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=TG_TIMEOUT + 30) as response:
            payload = json.loads(response.read())
            if isinstance(payload, dict):
                return payload
            log("telegram getUpdates returned non-object payload")
    except urllib.error.HTTPError as exc:
        log(f"telegram getUpdates HTTP {exc.code}")
    except Exception as exc:
        log(f"telegram getUpdates error: {exc}")
    return None


def poll_telegram() -> None:
    offset = None
    while True:
        updates = tg_get_updates(offset)
        if not updates or not updates.get("ok"):
            time.sleep(1)
            continue
        for update in updates.get("result", []):
            if not isinstance(update, dict) or "update_id" not in update:
                continue
            offset = int(update["update_id"]) + 1
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            if not isinstance(chat, dict):
                continue
            incoming_chat_id = str(chat.get("id", ""))
            text = message.get("text")
            if incoming_chat_id != str(CHAT_ID):
                tg_call("sendMessage", chat_id=incoming_chat_id, text=f"unauthorized (chat_id={incoming_chat_id})")
                continue
            if isinstance(text, str) and text:
                log(f"inbound text chars={len(text)}")
                send_to_codex(text)


def print_chat_ids() -> int:
    updates = tg_get_updates(0)
    if not updates or not updates.get("ok"):
        return 1
    seen: set[str] = set()
    for update in updates.get("result", []):
        message = update.get("message") if isinstance(update, dict) else None
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(chat, dict):
            continue
        chat_id = str(chat.get("id", ""))
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or "unknown"
        print(f"chat_id={chat_id} title={title}")
    if not seen:
        print("No chat ids found. Send a message to the bot, then retry.", file=sys.stderr)
        return 1
    return 0


def run_bridge() -> int:
    require_token()
    require_chat_id()
    if not tmux_target_exists():
        return 1

    log(f"starting target={TMUX_TARGET} sessions={SESSIONS_DIR}")
    tailer = threading.Thread(target=tail_rollouts, daemon=True)
    tailer.start()
    poll_telegram()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge Telegram text to a Codex tmux pane.")
    parser.add_argument("--print-chat-ids", action="store_true", help="print Telegram chat ids from getUpdates")
    args = parser.parse_args()
    if args.print_chat_ids:
        return print_chat_ids()
    return run_bridge()


if __name__ == "__main__":
    raise SystemExit(main())
