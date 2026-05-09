#!/usr/bin/env python3
"""Telegram <-> tmux pane bridge.

Required environment:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  TMUX_TARGET

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


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TMUX_TARGET = os.environ.get("TMUX_TARGET", "")
TELEGRAM_TIMEOUT = env_int("TELEGRAM_TIMEOUT", 30)
POLL_MS = env_int("POLL_MS", 1000)
TMUX_HISTORY_LINES = env_int("TMUX_HISTORY_LINES", 120)
TELEGRAM_CHUNK_LIMIT = env_int("TELEGRAM_CHUNK_LIMIT", 3800)
SEND_ENTER = os.environ.get("SEND_ENTER", "1") != "0"
OUTBOUND_ENABLED = os.environ.get("OUTBOUND_ENABLED", "1") != "0"
API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""


def log(message: str) -> None:
    print(f"[bridge] {message}", file=sys.stderr, flush=True)


def require_token() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")


def tg_call(method: str, **kwargs: str) -> dict[str, Any] | None:
    require_token()
    url = f"{API}/{method}"
    data = urllib.parse.urlencode(kwargs).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=TELEGRAM_TIMEOUT + 30) as response:
            payload = json.loads(response.read())
            if isinstance(payload, dict):
                return payload
            log(f"telegram {method} returned non-object payload")
            return None
    except urllib.error.HTTPError as exc:
        log(f"telegram {method} HTTP {exc.code}")
    except Exception as exc:
        log(f"telegram {method} error: {exc}")
    return None


def tg_get_updates(offset: int | None = None) -> dict[str, Any] | None:
    require_token()
    params = {
        "timeout": str(TELEGRAM_TIMEOUT),
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        params["offset"] = str(offset)
    url = f"{API}/getUpdates?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=TELEGRAM_TIMEOUT + 30) as response:
            payload = json.loads(response.read())
            if isinstance(payload, dict):
                return payload
            log("telegram getUpdates returned non-object payload")
    except urllib.error.HTTPError as exc:
        log(f"telegram getUpdates HTTP {exc.code}")
    except Exception as exc:
        log(f"telegram getUpdates error: {exc}")
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


def tmux(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def tmux_target_exists() -> bool:
    result = tmux(["display-message", "-p", "-t", TMUX_TARGET, "#{pane_id}"])
    if result.returncode != 0:
        log(f"tmux target check failed: {result.stderr.strip()}")
        return False
    return True


def paste_to_tmux(text: str) -> None:
    if not text:
        return
    buffer_name = "telegram-bridge"
    load = tmux(["load-buffer", "-b", buffer_name, "-"], input_text=text)
    if load.returncode != 0:
        log(f"tmux load-buffer failed: {load.stderr.strip()}")
        return
    paste = tmux(["paste-buffer", "-d", "-b", buffer_name, "-t", TMUX_TARGET])
    if paste.returncode != 0:
        log(f"tmux paste-buffer failed: {paste.stderr.strip()}")
        return
    if SEND_ENTER:
        time.sleep(0.25)
        enter = tmux(["send-keys", "-t", TMUX_TARGET, "Enter"])
        if enter.returncode != 0:
            log(f"tmux send-keys failed: {enter.stderr.strip()}")


def capture_pane() -> list[str] | None:
    result = tmux(["capture-pane", "-t", TMUX_TARGET, "-p", "-S", f"-{TMUX_HISTORY_LINES}"])
    if result.returncode != 0:
        log(f"tmux capture-pane failed: {result.stderr.strip()}")
        return None
    return result.stdout.splitlines()


def overlap_index(previous: list[str], current: list[str]) -> int:
    max_overlap = min(len(previous), len(current))
    for size in range(max_overlap, 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


def format_new_output(lines: list[str]) -> str:
    cleaned = [line.rstrip() for line in lines]
    while cleaned and not cleaned[0]:
        cleaned.pop(0)
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned)


def outbound_loop(state: dict[str, Any]) -> None:
    if not OUTBOUND_ENABLED:
        return
    current = capture_pane()
    if current is None:
        return
    previous = state.get("previous_pane")
    if previous is None:
        state["previous_pane"] = current
        return
    overlap = overlap_index(previous, current)
    new_lines = current[overlap:]
    state["previous_pane"] = current
    text = format_new_output(new_lines)
    if text:
        send_message(text)


def inbound_loop(state: dict[str, Any]) -> None:
    offset = state.get("offset")
    updates = tg_get_updates(offset if isinstance(offset, int) else None)
    if not updates or not updates.get("ok"):
        return
    for update in updates.get("result", []):
        if not isinstance(update, dict) or "update_id" not in update:
            continue
        state["offset"] = int(update["update_id"]) + 1
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue
        incoming_chat_id = str(chat.get("id", ""))
        text = message.get("text")
        if incoming_chat_id != str(CHAT_ID):
            tg_call(
                "sendMessage",
                chat_id=incoming_chat_id,
                text="unauthorized",
                disable_web_page_preview="true",
            )
            continue
        if isinstance(text, str) and text:
            log(f"inbound text chars={len(text)}")
            paste_to_tmux(text)


def inbound_forever() -> None:
    state: dict[str, Any] = {"offset": None}
    while True:
        try:
            inbound_loop(state)
        except Exception as exc:
            log(f"inbound loop error: {exc}")
            time.sleep(2)


def outbound_forever() -> None:
    state: dict[str, Any] = {"previous_pane": None}
    while True:
        try:
            outbound_loop(state)
        except Exception as exc:
            log(f"outbound loop error: {exc}")
            time.sleep(2)
        time.sleep(POLL_MS / 1000.0)


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
    if not CHAT_ID:
        raise SystemExit("TELEGRAM_CHAT_ID is required")
    if not TMUX_TARGET:
        raise SystemExit("TMUX_TARGET is required")
    if not tmux_target_exists():
        return 1

    log(
        "starting "
        f"target={TMUX_TARGET} "
        f"outbound={'on' if OUTBOUND_ENABLED else 'off'} "
        f"send_enter={'on' if SEND_ENTER else 'off'}"
    )
    inbound_thread = threading.Thread(target=inbound_forever, daemon=True)
    inbound_thread.start()
    outbound_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge Telegram text to a tmux pane.")
    parser.add_argument("--print-chat-ids", action="store_true", help="print Telegram chat ids from getUpdates")
    args = parser.parse_args()
    if args.print_chat_ids:
        return print_chat_ids()
    return run_bridge()


if __name__ == "__main__":
    raise SystemExit(main())
