import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Callable
from typing import Any
from typing import Iterator
from uuid import uuid4

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, stream_with_context
from waitress import serve

from wodex_config import CONFIG_DIR, WodexConfigError, load_wodex_config, resolve_config_path

PROJECT_ROOT = Path(__file__).resolve().parent
APP_CONFIG: dict[str, Any] | None = None
FONT_FILE_PATHS: dict[str, Path] = {}


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RpcTrafficLogger:
    def __init__(self, root_directory: Path) -> None:
        self._lock = threading.Lock()
        now = datetime.now()
        dated_directory = root_directory / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
        dated_directory.mkdir(parents=True, exist_ok=True)

        filename = f"codex-{now.strftime('%Y%m%d%H%M')}_{uuid4()}.log"
        self.path = dated_directory / filename
        self._handle = self.path.open("a", encoding="utf-8")

        latest_link = CONFIG_DIR / "codex-latest.log"
        latest_link.parent.mkdir(parents=True, exist_ok=True)
        temp_link = latest_link.with_name(f".{latest_link.name}.{uuid4().hex}.tmp")
        try:
            if temp_link.exists() or temp_link.is_symlink():
                temp_link.unlink()
            temp_link.symlink_to(self.path)
            os.replace(temp_link, latest_link)
        finally:
            if temp_link.exists() or temp_link.is_symlink():
                temp_link.unlink(missing_ok=True)

    def write(self, origin: str, message: dict[str, Any]) -> None:
        with self._lock:
            if self._handle.closed:
                return
            entry = [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                origin,
                message,
            ]
            line = json.dumps(entry, ensure_ascii=True)
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._handle.closed:
                return
            self._handle.close()


class BrowserWindowRegistry:
    def __init__(
        self,
        stale_seconds: float,
        shutdown_grace_seconds: float,
        poll_seconds: float = 1.0,
    ) -> None:
        self._stale_seconds = stale_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._windows: dict[str, float] = {}
        self._has_seen_window = False
        self._zero_windows_since: float | None = None
        self._shutdown_callback: Callable[[], None] | None = None
        self._shutdown_requested = False
        self._stop_event = threading.Event()
        self._monitor_thread = threading.Thread(
            target=self._monitor,
            name="wodex-window-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def set_shutdown_callback(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self._shutdown_callback = callback

    def touch(self, window_id: str) -> int:
        now = time.monotonic()
        with self._lock:
            self._prune_stale_locked(now)
            self._windows[window_id] = now
            self._has_seen_window = True
            self._zero_windows_since = None
            return len(self._windows)

    def close(self, window_id: str) -> int:
        now = time.monotonic()
        with self._lock:
            self._prune_stale_locked(now)
            self._windows.pop(window_id, None)
            self._update_zero_windows_since_locked(now)
            return len(self._windows)

    def stop(self) -> None:
        self._stop_event.set()
        if self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)

    def _monitor(self) -> None:
        while not self._stop_event.wait(self._poll_seconds):
            callback: Callable[[], None] | None = None
            with self._lock:
                now = time.monotonic()
                self._prune_stale_locked(now)
                self._update_zero_windows_since_locked(now)

                should_shutdown = (
                    self._has_seen_window
                    and not self._windows
                    and not self._shutdown_requested
                    and self._zero_windows_since is not None
                    and now - self._zero_windows_since >= self._shutdown_grace_seconds
                    and self._shutdown_callback is not None
                )
                if should_shutdown:
                    self._shutdown_requested = True
                    callback = self._shutdown_callback

            if callback is not None:
                callback()
                return

    def _prune_stale_locked(self, now: float) -> None:
        stale_before = now - self._stale_seconds
        stale_window_ids = [
            window_id for window_id, last_seen in self._windows.items() if last_seen < stale_before
        ]
        for window_id in stale_window_ids:
            self._windows.pop(window_id, None)

    def _update_zero_windows_since_locked(self, now: float) -> None:
        if self._windows:
            self._zero_windows_since = None
            return
        if self._has_seen_window and self._zero_windows_since is None:
            self._zero_windows_since = now


class EventStreamHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriber_counter = count(1)
        self._subscribers: dict[int, queue.Queue[dict[str, Any]]] = {}

    def subscribe(self) -> tuple[int, queue.Queue[dict[str, Any]]]:
        subscriber_id = next(self._subscriber_counter)
        subscriber_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subscribers[subscriber_id] = subscriber_queue
        return subscriber_id, subscriber_queue

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers.values())
        for subscriber_queue in subscribers:
            subscriber_queue.put(event)


class CodexAppServerClient:
    def __init__(
        self,
        default_cwd: str,
        thread_list_limit: int,
        rpc_log_directory: Path,
    ) -> None:
        self.default_cwd = default_cwd
        self.thread_list_limit = thread_list_limit
        self._id_counter = count(1)
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._loaded_threads: set[str] = set()
        self._loaded_threads_lock = threading.Lock()
        self._event_hub = EventStreamHub()
        self._live_state_lock = threading.Lock()
        self._live_threads: dict[str, dict[str, Any]] = {}
        self._synthetic_item_counter = count(1)
        self._rpc_logger = RpcTrafficLogger(rpc_log_directory)

        self._process = subprocess.Popen(
            ["codex", "app-server"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            name="wodex-codex-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="wodex-codex-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._initialize()

    def close(self) -> None:
        if self._process.poll() is not None:
            self._rpc_logger.close()
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        self._stdout_thread.join(timeout=2)
        self._stderr_thread.join(timeout=2)
        self._rpc_logger.close()

    def ensure_thread(self, thread_id: str | None) -> str:
        if thread_id:
            with self._loaded_threads_lock:
                is_loaded = thread_id in self._loaded_threads
            if not is_loaded:
                self._request("thread/resume", {"threadId": thread_id})
                with self._loaded_threads_lock:
                    self._loaded_threads.add(thread_id)
            return thread_id

        result = self._request(
            "thread/start",
            {
                "cwd": self.default_cwd,
                "serviceName": "wodex",
            },
        )
        new_thread_id = result["thread"]["id"]
        with self._loaded_threads_lock:
            self._loaded_threads.add(new_thread_id)
        return new_thread_id

    def list_threads(self, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is None:
            limit = self.thread_list_limit
        threads: list[dict[str, Any]] = []
        cursor: str | None = None

        while len(threads) < limit:
            remaining = limit - len(threads)
            params: dict[str, Any] = {
                "limit": remaining,
                "sortKey": "updated_at",
            }
            if cursor:
                params["cursor"] = cursor

            result = self._request("thread/list", params)
            page = result.get("data", [])
            threads.extend(page)
            cursor = result.get("nextCursor")
            if not page or cursor is None:
                break

        threads.sort(key=lambda thread: thread.get("updatedAt") or 0, reverse=True)
        return threads

    def read_thread(self, thread_id: str, include_turns: bool = False) -> dict[str, Any]:
        result = self._request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": include_turns,
            },
        )
        return result["thread"]

    def thread_messages(self, thread: dict[str, Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for turn in thread.get("turns", []):
            for item in turn.get("items", []):
                item_type = item.get("type")
                if item_type == "userMessage":
                    text_parts = [
                        content.get("text", "")
                        for content in item.get("content", [])
                        if content.get("type") == "text" and content.get("text")
                    ]
                    text = "\n".join(text_parts).strip()
                    if text:
                        messages.append({"role": "user", "text": text})
                elif item_type == "agentMessage":
                    text = item.get("text", "").strip()
                    if text:
                        messages.append({"role": "assistant", "text": text})
                elif item_type == "plan":
                    text = item.get("text", "").strip()
                    if text:
                        messages.append({"role": "plan", "text": text})
                elif item_type == "reasoning":
                    text = self._format_reasoning_text(
                        item.get("summary", []),
                        item.get("content", []),
                    )
                    if text:
                        messages.append({"role": "reasoning", "text": text})
                elif item_type == "commandExecution":
                    message = self._command_message_payload(
                        command=item.get("command"),
                        cwd=item.get("cwd"),
                        status=item.get("status"),
                        output=item.get("aggregatedOutput"),
                        exit_code=item.get("exitCode"),
                        duration_ms=item.get("durationMs"),
                        process_id=item.get("processId"),
                    )
                    if message["command"] or message["text"] or message["status"]:
                        messages.append(message)
                else:
                    message = self._structured_item_message_payload(
                        item_type,
                        item,
                        completed=True,
                        item_id=item.get("id"),
                    )
                    if message is not None:
                        messages.append(message)
        return messages

    def subscribe_events(self) -> tuple[int, queue.Queue[dict[str, Any]]]:
        return self._event_hub.subscribe()

    def unsubscribe_events(self, subscriber_id: int) -> None:
        self._event_hub.unsubscribe(subscriber_id)

    def get_live_state(self, thread_id: str) -> dict[str, Any]:
        with self._live_state_lock:
            state = self._live_threads.get(thread_id)
            if state is None:
                return {
                    "activeTurnId": None,
                    "thinkingSince": None,
                    "items": [],
                }
            return self._serialize_live_state_locked(state)

    def send_prompt(
        self,
        thread_id: str | None,
        prompt: str,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        active_thread_id = self.ensure_thread(thread_id)
        active_turn_id = self._active_turn_id(active_thread_id)
        input_items = [{"type": "text", "text": prompt}]

        if active_turn_id:
            try:
                result = self._request(
                    "turn/steer",
                    {
                        "threadId": active_thread_id,
                        "expectedTurnId": active_turn_id,
                        "input": input_items,
                    },
                )
                turn_id = result["turnId"]
                mode = "steer"
            except JsonRpcError:
                active_turn_id = None
            else:
                self._publish_user_message(active_thread_id, prompt, client_message_id, turn_id, mode)
                return {
                    "threadId": active_thread_id,
                    "turnId": turn_id,
                    "mode": mode,
                }

        result = self._request(
            "turn/start",
            {
                "threadId": active_thread_id,
                "input": input_items,
            },
        )
        turn_id = result["turn"]["id"]
        self._mark_turn_started(active_thread_id, turn_id, publish_event=False)
        self._publish_user_message(active_thread_id, prompt, client_message_id, turn_id, "start")
        return {
            "threadId": active_thread_id,
            "turnId": turn_id,
            "mode": "start",
        }

    def _active_turn_id(self, thread_id: str) -> str | None:
        with self._live_state_lock:
            state = self._live_threads.get(thread_id)
            if state is None:
                return None
            return state.get("activeTurnId")

    def _publish_user_message(
        self,
        thread_id: str,
        prompt: str,
        client_message_id: str | None,
        turn_id: str,
        mode: str,
    ) -> None:
        self._event_hub.publish(
            {
                "kind": "user_message",
                "threadId": thread_id,
                "turnId": turn_id,
                "mode": mode,
                "text": prompt,
                "clientMessageId": client_message_id,
            }
        )

    def _get_or_create_live_state_locked(self, thread_id: str) -> dict[str, Any]:
        state = self._live_threads.get(thread_id)
        if state is None:
            state = {
                "activeTurnId": None,
                "thinkingSince": None,
                "itemOrder": [],
                "items": {},
            }
            self._live_threads[thread_id] = state
        return state

    def _serialize_live_state_locked(self, state: dict[str, Any]) -> dict[str, Any]:
        items = []
        for item_id in state["itemOrder"]:
            item = state["items"].get(item_id)
            if item is None or not item.get("visible"):
                continue
            item_type = item.get("type")
            if item_type == "commandExecution":
                items.append(
                    self._command_message_payload(
                        command=item.get("command"),
                        cwd=item.get("cwd"),
                        status=item.get("status"),
                        output=item.get("text"),
                        exit_code=item.get("exitCode"),
                        duration_ms=item.get("durationMs"),
                        process_id=item.get("processId"),
                        terminal_input_count=int(item.get("terminalInputCount", 0) or 0),
                        completed=item.get("completed", False),
                        item_id=item_id,
                    )
                )
                continue
            if item_type not in {"agentMessage", "reasoning", "plan"}:
                payload = self._structured_live_item_message_payload(item_id, item)
                if payload is not None:
                    items.append(payload)
                continue
            if item_type == "agentMessage":
                role = "assistant"
            elif item_type == "reasoning":
                role = "reasoning"
            else:
                role = "plan"
            items.append(
                {
                    "itemId": item_id,
                    "type": item_type,
                    "role": role,
                    "text": item.get("text", ""),
                    "phase": item.get("phase"),
                    "completed": item.get("completed", False),
                }
            )
        return {
            "activeTurnId": state.get("activeTurnId"),
            "thinkingSince": state.get("thinkingSince"),
            "items": items,
        }

    def _mark_turn_started(self, thread_id: str, turn_id: str, publish_event: bool = True) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            state["thinkingSince"] = time.time()
            state["itemOrder"] = []
            state["items"] = {}
            live_state = self._serialize_live_state_locked(state)
        if publish_event:
            self._event_hub.publish(
                {
                    "kind": "turn_started",
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "live": live_state,
                }
            )

    def _get_or_create_live_item_locked(
        self,
        state: dict[str, Any],
        item_id: str,
        item_type: str,
        *,
        phase: str | None = None,
    ) -> dict[str, Any]:
        item = state["items"].get(item_id)
        if item is None:
            item = {
                "type": item_type,
                "text": "",
                "phase": phase,
                "completed": False,
                "visible": False,
            }
            if item_type == "reasoning":
                item["summary"] = []
                item["content"] = []
            if item_type == "commandExecution":
                item["command"] = ""
                item["cwd"] = ""
                item["status"] = None
                item["processId"] = None
                item["exitCode"] = None
                item["durationMs"] = None
                item["terminalInputCount"] = 0
            if item_type not in {"agentMessage", "plan", "reasoning", "commandExecution"}:
                item["rawItem"] = {}
            if item_type == "fileChange":
                item["deltaText"] = ""
            if item_type == "mcpToolCall":
                item["progressMessages"] = []
            state["items"][item_id] = item
            state["itemOrder"].append(item_id)
            return item

        item["type"] = item_type
        if phase is not None or item_type == "agentMessage":
            item["phase"] = phase
        if item_type == "reasoning":
            item.setdefault("summary", [])
            item.setdefault("content", [])
        if item_type == "commandExecution":
            item.setdefault("command", "")
            item.setdefault("cwd", "")
            item.setdefault("status", None)
            item.setdefault("processId", None)
            item.setdefault("exitCode", None)
            item.setdefault("durationMs", None)
            item.setdefault("terminalInputCount", 0)
        if item_type not in {"agentMessage", "plan", "reasoning", "commandExecution"}:
            item.setdefault("rawItem", {})
        if item_type == "fileChange":
            item.setdefault("deltaText", "")
        if item_type == "mcpToolCall":
            item.setdefault("progressMessages", [])
        return item

    def _remove_live_item_locked(self, state: dict[str, Any], item_id: str) -> None:
        state["items"].pop(item_id, None)
        state["itemOrder"] = [existing_item_id for existing_item_id in state["itemOrder"] if existing_item_id != item_id]

    def _format_turn_plan_text(
        self,
        explanation: str | None,
        plan_steps: list[dict[str, Any]],
    ) -> str:
        lines: list[str] = []
        explanation_text = (explanation or "").strip()
        if explanation_text:
            lines.append(explanation_text)

        status_prefixes = {
            "pending": "[ ]",
            "inProgress": "[~]",
            "completed": "[x]",
        }
        for step in plan_steps:
            step_text = str(step.get("step", "")).strip()
            if not step_text:
                continue
            prefix = status_prefixes.get(step.get("status"), "[-]")
            lines.append(f"{prefix} {step_text}")
        return "\n".join(lines).strip()

    def _format_reasoning_text(
        self,
        summary_parts: list[Any],
        content_parts: list[Any],
    ) -> str:
        summary_lines = [str(part).strip() for part in summary_parts if str(part).strip()]
        content_lines = [str(part).strip() for part in content_parts if str(part).strip()]

        sections: list[str] = []
        if summary_lines:
            sections.append("Summary:\n" + "\n".join(f"- {line}" for line in summary_lines))
        if content_lines:
            sections.append("Content:\n" + "\n\n".join(content_lines))
        return "\n\n".join(sections).strip()

    def _command_message_payload(
        self,
        *,
        command: str | None,
        cwd: str | None,
        status: str | None,
        output: str | None,
        exit_code: Any,
        duration_ms: Any,
        process_id: str | None,
        terminal_input_count: int = 0,
        completed: bool = True,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": "command",
            "text": str(output or ""),
            "command": str(command or ""),
            "cwd": str(cwd or ""),
            "status": status,
            "exitCode": exit_code,
            "durationMs": duration_ms,
            "processId": process_id,
            "terminalInputCount": terminal_input_count,
            "completed": completed,
        }
        if item_id:
            payload["itemId"] = item_id
            payload["type"] = "commandExecution"
        return payload

    def _merge_command_item_data(
        self,
        existing: dict[str, Any],
        item: dict[str, Any],
    ) -> None:
        existing["command"] = str(item.get("command") or "")
        existing["cwd"] = str(item.get("cwd") or "")
        existing["status"] = item.get("status")
        existing["processId"] = item.get("processId")
        existing["exitCode"] = item.get("exitCode")
        existing["durationMs"] = item.get("durationMs")
        aggregated_output = item.get("aggregatedOutput")
        if aggregated_output is not None:
            existing["text"] = str(aggregated_output)

    def _json_text(self, value: Any) -> str:
        try:
            return json.dumps(value, indent=2, ensure_ascii=True)
        except TypeError:
            return json.dumps(str(value), indent=2, ensure_ascii=True)

    def _compact_meta_parts(self, *parts: Any) -> list[str]:
        values: list[str] = []
        for part in parts:
            text = str(part or "").strip()
            if text:
                values.append(text)
        return values

    def _structured_message_visible(self, payload: dict[str, Any]) -> bool:
        if payload.get("text"):
            return True
        if payload.get("title"):
            return True
        return bool(payload.get("metaParts"))

    def _structured_message_payload(
        self,
        *,
        role: str,
        label: str,
        title: str | None = None,
        text: str | None = None,
        meta_parts: list[str] | None = None,
        body_is_code: bool = False,
        completed: bool = True,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": role,
            "type": role,
            "label": label,
            "title": str(title or ""),
            "text": str(text or ""),
            "metaParts": meta_parts or [],
            "bodyIsCode": body_is_code,
            "completed": completed,
        }
        if item_id:
            payload["itemId"] = item_id
        return payload

    def _format_patch_change_header(self, change: dict[str, Any]) -> str:
        path = str(change.get("path") or "").strip() or "(unknown path)"
        kind = change.get("kind", {})
        kind_type = str(kind.get("type") or "update")
        if kind_type == "add":
            return f"add {path}"
        if kind_type == "delete":
            return f"delete {path}"
        move_path = str(kind.get("move_path") or "").strip()
        if move_path:
            return f"update {path} -> {move_path}"
        return f"update {path}"

    def _format_file_change_text(self, changes: list[dict[str, Any]], delta_text: str | None = None) -> str:
        streamed = str(delta_text or "").rstrip()
        if streamed:
            return streamed

        blocks: list[str] = []
        for change in changes:
            header = self._format_patch_change_header(change)
            diff = str(change.get("diff") or "").rstrip()
            blocks.append(f"{header}\n{diff}".rstrip())
        return "\n\n".join(blocks).strip()

    def _format_dynamic_tool_content(self, content_items: list[dict[str, Any]] | None) -> str:
        if not content_items:
            return ""
        lines: list[str] = []
        for item in content_items:
            item_type = item.get("type")
            if item_type == "inputText":
                lines.append(str(item.get("text") or ""))
            elif item_type == "inputImage":
                lines.append(f"[image] {item.get('imageUrl')}")
            else:
                lines.append(self._json_text(item))
        return "\n".join(line for line in lines if line)

    def _structured_item_message_payload(
        self,
        item_type: str,
        item: dict[str, Any],
        *,
        completed: bool,
        item_id: str | None = None,
        delta_text: str | None = None,
        progress_messages: list[str] | None = None,
    ) -> dict[str, Any] | None:
        if item_type == "fileChange":
            changes = item.get("changes", [])
            text = self._format_file_change_text(changes, delta_text)
            payload = self._structured_message_payload(
                role="fileChange",
                label="File Change",
                title=f"{len(changes)} change{'s' if len(changes) != 1 else ''}",
                text=text,
                meta_parts=self._compact_meta_parts(item.get("status")),
                body_is_code=bool(text),
                completed=completed,
                item_id=item_id,
            )
            return payload if self._structured_message_visible(payload) else None

        if item_type == "mcpToolCall":
            body_sections: list[str] = []
            arguments = item.get("arguments")
            if arguments is not None:
                body_sections.append("Arguments:\n" + self._json_text(arguments))
            if progress_messages:
                body_sections.append("Progress:\n" + "\n".join(progress_messages))
            error = item.get("error")
            result = item.get("result")
            if error:
                body_sections.append("Error:\n" + self._json_text(error))
            elif result is not None:
                body_sections.append("Result:\n" + self._json_text(result))
            payload = self._structured_message_payload(
                role="mcpToolCall",
                label="MCP Tool",
                title=".".join(part for part in [str(item.get("server") or ""), str(item.get("tool") or "")] if part),
                text="\n\n".join(section for section in body_sections if section).strip(),
                meta_parts=self._compact_meta_parts(item.get("status"), f"{item.get('durationMs')}ms" if item.get("durationMs") is not None else None),
                body_is_code=bool(body_sections),
                completed=completed,
                item_id=item_id,
            )
            return payload if self._structured_message_visible(payload) else None

        if item_type == "dynamicToolCall":
            body_sections = []
            arguments = item.get("arguments")
            if arguments is not None:
                body_sections.append("Arguments:\n" + self._json_text(arguments))
            content_text = self._format_dynamic_tool_content(item.get("contentItems"))
            if content_text:
                body_sections.append("Output:\n" + content_text)
            success = item.get("success")
            success_text = None if success is None else f"success={success}"
            payload = self._structured_message_payload(
                role="dynamicToolCall",
                label="Dynamic Tool",
                title=str(item.get("tool") or ""),
                text="\n\n".join(section for section in body_sections if section).strip(),
                meta_parts=self._compact_meta_parts(item.get("status"), success_text, f"{item.get('durationMs')}ms" if item.get("durationMs") is not None else None),
                body_is_code=bool(body_sections),
                completed=completed,
                item_id=item_id,
            )
            return payload if self._structured_message_visible(payload) else None

        if item_type == "collabAgentToolCall":
            body_sections = []
            prompt = str(item.get("prompt") or "").strip()
            if prompt:
                body_sections.append("Prompt:\n" + prompt)
            receiver_ids = item.get("receiverThreadIds", [])
            if receiver_ids:
                body_sections.append("Receivers:\n" + "\n".join(str(receiver_id) for receiver_id in receiver_ids))
            agents_states = item.get("agentsStates")
            if agents_states:
                body_sections.append("Agent States:\n" + self._json_text(agents_states))
            payload = self._structured_message_payload(
                role="collabAgentToolCall",
                label="Collab Agent",
                title=str(item.get("tool") or ""),
                text="\n\n".join(section for section in body_sections if section).strip(),
                meta_parts=self._compact_meta_parts(item.get("status"), f"from {item.get('senderThreadId')}" if item.get("senderThreadId") else None, item.get("model"), item.get("reasoningEffort")),
                body_is_code=bool(agents_states),
                completed=completed,
                item_id=item_id,
            )
            return payload if self._structured_message_visible(payload) else None

        if item_type == "webSearch":
            action = item.get("action")
            action_type = action.get("type") if isinstance(action, dict) else None
            text = ""
            if action is not None:
                text = self._json_text(action)
            payload = self._structured_message_payload(
                role="webSearch",
                label="Web Search",
                title=str(item.get("query") or ""),
                text=text,
                meta_parts=self._compact_meta_parts(action_type),
                body_is_code=bool(text),
                completed=completed,
                item_id=item_id,
            )
            return payload if self._structured_message_visible(payload) else None

        if item_type == "imageView":
            payload = self._structured_message_payload(
                role="imageView",
                label="Image View",
                title=str(item.get("path") or ""),
                completed=completed,
                item_id=item_id,
            )
            return payload if self._structured_message_visible(payload) else None

        if item_type == "imageGeneration":
            body_sections = []
            revised_prompt = str(item.get("revisedPrompt") or "").strip()
            if revised_prompt:
                body_sections.append("Revised Prompt:\n" + revised_prompt)
            result = str(item.get("result") or "").strip()
            if result:
                body_sections.append("Result:\n" + result)
            payload = self._structured_message_payload(
                role="imageGeneration",
                label="Image Generation",
                title=str(item.get("status") or ""),
                text="\n\n".join(body_sections).strip(),
                meta_parts=self._compact_meta_parts(item.get("status")),
                body_is_code=False,
                completed=completed,
                item_id=item_id,
            )
            return payload if self._structured_message_visible(payload) else None

        if item_type == "enteredReviewMode":
            payload = self._structured_message_payload(
                role="enteredReviewMode",
                label="Review Mode",
                title="Entered",
                text=str(item.get("review") or "").strip(),
                completed=completed,
                item_id=item_id,
            )
            return payload if self._structured_message_visible(payload) else None

        if item_type == "exitedReviewMode":
            payload = self._structured_message_payload(
                role="exitedReviewMode",
                label="Review Mode",
                title="Exited",
                text=str(item.get("review") or "").strip(),
                completed=completed,
                item_id=item_id,
            )
            return payload if self._structured_message_visible(payload) else None

        if item_type == "contextCompaction":
            return self._structured_message_payload(
                role="contextCompaction",
                label="Context",
                title="Compaction",
                text="Conversation context was compacted.",
                completed=completed,
                item_id=item_id,
            )

        return None

    def _structured_live_item_message_payload(self, item_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
        raw_item = item.get("rawItem", {})
        return self._structured_item_message_payload(
            item.get("type", ""),
            raw_item if isinstance(raw_item, dict) else {},
            completed=item.get("completed", False),
            item_id=item_id,
            delta_text=str(item.get("deltaText") or ""),
            progress_messages=[str(message) for message in item.get("progressMessages", [])],
        )

    def _structured_live_item_visible(self, item: dict[str, Any]) -> bool:
        payload = self._structured_live_item_message_payload("", item)
        return payload is not None and self._structured_message_visible(payload)

    def _merge_structured_item_data(self, existing: dict[str, Any], item: dict[str, Any]) -> None:
        existing["rawItem"] = deepcopy(item)

    def _ensure_text_part(self, parts: list[str], index: int) -> None:
        while len(parts) <= index:
            parts.append("")

    def _record_agent_message_started(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        if not item_id:
            return
        text = item.get("text", "") or ""
        phase = item.get("phase")
        reset_timer = False
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            existing = self._get_or_create_live_item_locked(
                state,
                item_id,
                "agentMessage",
                phase=phase,
            )
            was_visible = existing["visible"]
            existing["phase"] = phase
            existing["completed"] = False
            if text:
                existing["text"] = text
                existing["visible"] = True
            if existing["visible"] and not was_visible:
                state["thinkingSince"] = time.time()
                reset_timer = True
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "agent_message_started",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "text": text,
                "phase": phase,
                "resetThinkingTimer": reset_timer,
                "live": live_state,
            }
        )

    def _record_agent_message_delta(
        self,
        thread_id: str,
        turn_id: str,
        item_id: str,
        delta: str,
    ) -> None:
        reset_timer = False
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            item = self._get_or_create_live_item_locked(state, item_id, "agentMessage")
            was_visible = item["visible"]
            item["text"] = f'{item["text"]}{delta}'
            if item["text"]:
                item["visible"] = True
            if item["visible"] and not was_visible:
                state["thinkingSince"] = time.time()
                reset_timer = True
            live_state = self._serialize_live_state_locked(state)
            full_text = item["text"]
        self._event_hub.publish(
            {
                "kind": "agent_message_delta",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "delta": delta,
                "text": full_text,
                "resetThinkingTimer": reset_timer,
                "live": live_state,
            }
        )

    def _record_agent_message_completed(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        if not item_id:
            return
        text = item.get("text", "") or ""
        phase = item.get("phase")
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            existing = self._get_or_create_live_item_locked(
                state,
                item_id,
                "agentMessage",
                phase=phase,
            )
            if text:
                existing["text"] = text
                existing["visible"] = True
            existing["phase"] = phase
            existing["completed"] = True
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "agent_message_completed",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "text": text,
                "phase": phase,
                "live": live_state,
            }
        )

    def _record_plan_started(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        if not item_id:
            return
        text = item.get("text", "") or ""
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            self._remove_live_item_locked(state, f"turn-plan:{turn_id}")
            existing = self._get_or_create_live_item_locked(state, item_id, "plan")
            existing["completed"] = False
            if text:
                existing["text"] = text
                existing["visible"] = True
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "plan_started",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "text": text,
                "live": live_state,
            }
        )

    def _record_plan_delta(self, thread_id: str, turn_id: str, item_id: str, delta: str) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            self._remove_live_item_locked(state, f"turn-plan:{turn_id}")
            item = self._get_or_create_live_item_locked(state, item_id, "plan")
            item["text"] = f'{item["text"]}{delta}'
            if item["text"]:
                item["visible"] = True
            live_state = self._serialize_live_state_locked(state)
            full_text = item["text"]
        self._event_hub.publish(
            {
                "kind": "plan_delta",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "delta": delta,
                "text": full_text,
                "live": live_state,
            }
        )

    def _record_plan_completed(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        if not item_id:
            return
        text = item.get("text", "") or ""
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            self._remove_live_item_locked(state, f"turn-plan:{turn_id}")
            existing = self._get_or_create_live_item_locked(state, item_id, "plan")
            if text:
                existing["text"] = text
                existing["visible"] = True
            existing["completed"] = True
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "plan_completed",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "text": text,
                "live": live_state,
            }
        )

    def _record_turn_plan_updated(
        self,
        thread_id: str,
        turn_id: str,
        explanation: str | None,
        plan_steps: list[dict[str, Any]],
    ) -> None:
        item_id = f"turn-plan:{turn_id}"
        text = self._format_turn_plan_text(explanation, plan_steps)
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            has_explicit_plan = any(
                existing_item.get("type") == "plan" and existing_item_id != item_id
                for existing_item_id, existing_item in state["items"].items()
            )
            if has_explicit_plan:
                live_state = self._serialize_live_state_locked(state)
            else:
                existing = self._get_or_create_live_item_locked(state, item_id, "plan")
                existing["text"] = text
                existing["visible"] = bool(text)
                existing["completed"] = False
                live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "turn_plan_updated",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "explanation": explanation,
                "plan": plan_steps,
                "text": text,
                "live": live_state,
            }
        )

    def _record_reasoning_started(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        if not item_id:
            return
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            existing = self._get_or_create_live_item_locked(state, item_id, "reasoning")
            existing["summary"] = [str(part) for part in item.get("summary", [])]
            existing["content"] = [str(part) for part in item.get("content", [])]
            existing["text"] = self._format_reasoning_text(existing["summary"], existing["content"])
            existing["visible"] = bool(existing["text"])
            existing["completed"] = False
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "reasoning_started",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "text": existing["text"],
                "live": live_state,
            }
        )

    def _record_reasoning_summary_part_added(
        self,
        thread_id: str,
        turn_id: str,
        item_id: str,
        summary_index: int,
    ) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            item = self._get_or_create_live_item_locked(state, item_id, "reasoning")
            summary = item.setdefault("summary", [])
            self._ensure_text_part(summary, summary_index)
            item["text"] = self._format_reasoning_text(summary, item.setdefault("content", []))
            item["visible"] = bool(item["text"])
            live_state = self._serialize_live_state_locked(state)
            full_text = item["text"]
        self._event_hub.publish(
            {
                "kind": "reasoning_updated",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "text": full_text,
                "live": live_state,
            }
        )

    def _record_reasoning_summary_delta(
        self,
        thread_id: str,
        turn_id: str,
        item_id: str,
        summary_index: int,
        delta: str,
    ) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            item = self._get_or_create_live_item_locked(state, item_id, "reasoning")
            summary = item.setdefault("summary", [])
            self._ensure_text_part(summary, summary_index)
            summary[summary_index] = f'{summary[summary_index]}{delta}'
            item["text"] = self._format_reasoning_text(summary, item.setdefault("content", []))
            item["visible"] = bool(item["text"])
            live_state = self._serialize_live_state_locked(state)
            full_text = item["text"]
        self._event_hub.publish(
            {
                "kind": "reasoning_updated",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "text": full_text,
                "live": live_state,
            }
        )

    def _record_reasoning_content_delta(
        self,
        thread_id: str,
        turn_id: str,
        item_id: str,
        content_index: int,
        delta: str,
    ) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            item = self._get_or_create_live_item_locked(state, item_id, "reasoning")
            content = item.setdefault("content", [])
            self._ensure_text_part(content, content_index)
            content[content_index] = f'{content[content_index]}{delta}'
            item["text"] = self._format_reasoning_text(item.setdefault("summary", []), content)
            item["visible"] = bool(item["text"])
            live_state = self._serialize_live_state_locked(state)
            full_text = item["text"]
        self._event_hub.publish(
            {
                "kind": "reasoning_updated",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "text": full_text,
                "live": live_state,
            }
        )

    def _record_reasoning_completed(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        if not item_id:
            return
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            existing = self._get_or_create_live_item_locked(state, item_id, "reasoning")
            existing["summary"] = [str(part) for part in item.get("summary", [])]
            existing["content"] = [str(part) for part in item.get("content", [])]
            existing["text"] = self._format_reasoning_text(existing["summary"], existing["content"])
            existing["visible"] = bool(existing["text"])
            existing["completed"] = True
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "reasoning_completed",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "text": existing["text"],
                "live": live_state,
            }
        )

    def _record_command_started(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        if not item_id:
            return
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            existing = self._get_or_create_live_item_locked(state, item_id, "commandExecution")
            self._merge_command_item_data(existing, item)
            existing["completed"] = False
            existing["visible"] = bool(existing["command"] or existing["text"] or existing["status"])
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "command_execution_started",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "live": live_state,
            }
        )

    def _record_command_output_delta(
        self,
        thread_id: str,
        turn_id: str,
        item_id: str,
        delta: str,
    ) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            item = self._get_or_create_live_item_locked(state, item_id, "commandExecution")
            item["status"] = item.get("status") or "inProgress"
            item["text"] = f'{item["text"]}{delta}'
            item["visible"] = bool(item["command"] or item["text"] or item["status"])
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "command_execution_updated",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "live": live_state,
            }
        )

    def _record_command_terminal_interaction(
        self,
        thread_id: str,
        turn_id: str,
        item_id: str,
        process_id: str | None,
    ) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            item = self._get_or_create_live_item_locked(state, item_id, "commandExecution")
            item["processId"] = process_id
            item["terminalInputCount"] = int(item.get("terminalInputCount", 0) or 0) + 1
            item["visible"] = bool(item["command"] or item["text"] or item["status"])
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "command_execution_updated",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "live": live_state,
            }
        )

    def _record_command_completed(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        if not item_id:
            return
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            existing = self._get_or_create_live_item_locked(state, item_id, "commandExecution")
            self._merge_command_item_data(existing, item)
            existing["completed"] = True
            existing["visible"] = bool(existing["command"] or existing["text"] or existing["status"])
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "command_execution_completed",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "live": live_state,
            }
        )

    def _record_structured_item_started(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        item_type = item.get("type")
        if not item_id or not item_type:
            return
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            existing = self._get_or_create_live_item_locked(state, item_id, item_type)
            self._merge_structured_item_data(existing, item)
            existing["completed"] = False
            existing["visible"] = self._structured_live_item_visible(existing)
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "structured_item_started",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "itemType": item_type,
                "live": live_state,
            }
        )

    def _record_structured_item_completed(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        item_id = item.get("id")
        item_type = item.get("type")
        if not item_id or not item_type:
            return
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            existing = self._get_or_create_live_item_locked(state, item_id, item_type)
            self._merge_structured_item_data(existing, item)
            existing["completed"] = True
            existing["visible"] = self._structured_live_item_visible(existing)
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "structured_item_completed",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "itemType": item_type,
                "live": live_state,
            }
        )

    def _record_file_change_delta(self, thread_id: str, turn_id: str, item_id: str, delta: str) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            item = self._get_or_create_live_item_locked(state, item_id, "fileChange")
            item["deltaText"] = f'{item.get("deltaText", "")}{delta}'
            item["visible"] = self._structured_live_item_visible(item)
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "structured_item_updated",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "itemType": "fileChange",
                "live": live_state,
            }
        )

    def _record_mcp_progress(
        self,
        thread_id: str,
        turn_id: str,
        item_id: str,
        message: str,
    ) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            item = self._get_or_create_live_item_locked(state, item_id, "mcpToolCall")
            progress_messages = item.setdefault("progressMessages", [])
            progress_messages.append(str(message))
            item["visible"] = self._structured_live_item_visible(item)
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "structured_item_updated",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "itemType": "mcpToolCall",
                "live": live_state,
            }
        )

    def _record_context_compacted(self, thread_id: str, turn_id: str) -> None:
        item_id = f"context-compaction:{turn_id}:{next(self._synthetic_item_counter)}"
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            item = self._get_or_create_live_item_locked(state, item_id, "contextCompaction")
            item["rawItem"] = {"id": item_id, "type": "contextCompaction"}
            item["completed"] = True
            item["visible"] = self._structured_live_item_visible(item)
            live_state = self._serialize_live_state_locked(state)
        self._event_hub.publish(
            {
                "kind": "structured_item_completed",
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
                "itemType": "contextCompaction",
                "live": live_state,
            }
        )

    def _record_turn_completed(self, thread_id: str, turn: dict[str, Any]) -> None:
        with self._live_state_lock:
            self._live_threads.pop(thread_id, None)
        self._event_hub.publish(
            {
                "kind": "turn_completed",
                "threadId": thread_id,
                "turnId": turn.get("id"),
                "status": turn.get("status"),
                "error": turn.get("error"),
            }
        )

    def _record_turn_error(
        self,
        thread_id: str,
        turn_id: str,
        error: dict[str, Any] | None,
        will_retry: bool,
    ) -> None:
        self._event_hub.publish(
            {
                "kind": "turn_error",
                "threadId": thread_id,
                "turnId": turn_id,
                "message": self._format_turn_error(error),
                "error": error,
                "willRetry": will_retry,
            }
        )

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "wodex",
                    "title": "Wodex",
                    "version": "0.1.2",
                }
            },
        )
        self._notify("initialized", {})

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        self._ensure_process_alive()

        request_id = next(self._id_counter)
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = response_queue

        try:
            self._send(
                {
                    "method": method,
                    "id": request_id,
                    "params": params or {},
                }
            )
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"Timed out waiting for {method} response.") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        if "error" in response:
            error = response["error"]
            raise JsonRpcError(error.get("code", -1), error.get("message", "Unknown JSON-RPC error"))

        return response["result"]

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._ensure_process_alive()
        self._send({"method": method, "params": params or {}})

    def _send(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message)
        with self._write_lock:
            self._rpc_logger.write("client", message)
            if self._process.stdin is None:
                raise RuntimeError("codex app-server stdin is unavailable.")
            self._process.stdin.write(encoded + "\n")
            self._process.stdin.flush()

    def _read_stdout(self) -> None:
        if self._process.stdout is None:
            return

        for line in self._process.stdout:
            payload = line.strip()
            if not payload:
                continue

            message = json.loads(payload)
            self._rpc_logger.write("server", message)
            if "id" in message:
                with self._pending_lock:
                    response_queue = self._pending.get(message["id"])
                if response_queue is not None:
                    response_queue.put(message)
                continue

            self._handle_notification(message)

    def _read_stderr(self) -> None:
        if self._process.stderr is None:
            return

        for line in self._process.stderr:
            sys.stderr.write(f"[codex app-server] {line}")

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params", {})

        if method == "thread/started":
            thread = params.get("thread", {})
            thread_id = thread.get("id")
            if thread_id:
                with self._loaded_threads_lock:
                    self._loaded_threads.add(thread_id)
            return

        if method == "thread/closed":
            thread_id = params.get("threadId")
            if thread_id:
                with self._loaded_threads_lock:
                    self._loaded_threads.discard(thread_id)
            return

        if method == "turn/started":
            thread_id = params.get("threadId")
            turn = params.get("turn", {})
            turn_id = turn.get("id")
            if thread_id and turn_id:
                self._mark_turn_started(thread_id, turn_id)
            return

        if method == "item/started":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item = params.get("item", {})
            item_type = item.get("type")
            if thread_id and turn_id and item_type == "agentMessage":
                self._record_agent_message_started(thread_id, turn_id, item)
            elif thread_id and turn_id and item_type == "plan":
                self._record_plan_started(thread_id, turn_id, item)
            elif thread_id and turn_id and item_type == "reasoning":
                self._record_reasoning_started(thread_id, turn_id, item)
            elif thread_id and turn_id and item_type == "commandExecution":
                self._record_command_started(thread_id, turn_id, item)
            elif thread_id and turn_id and item_type:
                self._record_structured_item_started(thread_id, turn_id, item)
            return

        if method == "item/agentMessage/delta":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            delta = params.get("delta", "")
            if thread_id and turn_id and item_id:
                self._record_agent_message_delta(thread_id, turn_id, item_id, delta)
            return

        if method == "turn/plan/updated":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            if thread_id and turn_id:
                self._record_turn_plan_updated(
                    thread_id,
                    turn_id,
                    params.get("explanation"),
                    params.get("plan", []),
                )
            return

        if method == "item/plan/delta":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            delta = params.get("delta", "")
            if thread_id and turn_id and item_id:
                self._record_plan_delta(thread_id, turn_id, item_id, delta)
            return

        if method == "item/reasoning/summaryPartAdded":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            summary_index = params.get("summaryIndex")
            if (
                thread_id
                and turn_id
                and item_id
                and isinstance(summary_index, int)
            ):
                self._record_reasoning_summary_part_added(thread_id, turn_id, item_id, summary_index)
            return

        if method == "item/reasoning/summaryTextDelta":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            summary_index = params.get("summaryIndex")
            delta = params.get("delta", "")
            if (
                thread_id
                and turn_id
                and item_id
                and isinstance(summary_index, int)
            ):
                self._record_reasoning_summary_delta(thread_id, turn_id, item_id, summary_index, delta)
            return

        if method == "item/reasoning/textDelta":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            content_index = params.get("contentIndex")
            delta = params.get("delta", "")
            if (
                thread_id
                and turn_id
                and item_id
                and isinstance(content_index, int)
            ):
                self._record_reasoning_content_delta(thread_id, turn_id, item_id, content_index, delta)
            return

        if method == "item/commandExecution/outputDelta":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            delta = params.get("delta", "")
            if thread_id and turn_id and item_id:
                self._record_command_output_delta(thread_id, turn_id, item_id, delta)
            return

        if method == "item/commandExecution/terminalInteraction":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            process_id = params.get("processId")
            if thread_id and turn_id and item_id:
                self._record_command_terminal_interaction(thread_id, turn_id, item_id, process_id)
            return

        if method == "item/fileChange/outputDelta":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            delta = params.get("delta", "")
            if thread_id and turn_id and item_id:
                self._record_file_change_delta(thread_id, turn_id, item_id, delta)
            return

        if method == "item/mcpToolCall/progress":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            progress_message = params.get("message", "")
            if thread_id and turn_id and item_id:
                self._record_mcp_progress(thread_id, turn_id, item_id, progress_message)
            return

        if method == "thread/compacted":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            if thread_id and turn_id:
                self._record_context_compacted(thread_id, turn_id)
            return

        if method == "item/completed":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item = params.get("item", {})
            item_type = item.get("type")
            if thread_id and turn_id and item_type == "agentMessage":
                self._record_agent_message_completed(thread_id, turn_id, item)
            elif thread_id and turn_id and item_type == "plan":
                self._record_plan_completed(thread_id, turn_id, item)
            elif thread_id and turn_id and item_type == "reasoning":
                self._record_reasoning_completed(thread_id, turn_id, item)
            elif thread_id and turn_id and item_type == "commandExecution":
                self._record_command_completed(thread_id, turn_id, item)
            elif thread_id and turn_id and item_type:
                self._record_structured_item_completed(thread_id, turn_id, item)
            return

        if method == "turn/completed":
            thread_id = params.get("threadId")
            turn = params.get("turn", {})
            if thread_id:
                self._record_turn_completed(thread_id, turn)
            return

        if method == "error":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            if thread_id and turn_id:
                error = params.get("error", {})
                self._record_turn_error(thread_id, turn_id, error, bool(params.get("willRetry")))

    def _format_turn_error(self, error: dict[str, Any] | None) -> str:
        if not error:
            return ""
        message = error.get("message")
        details = error.get("additionalDetails")
        if message and details:
            return f"{message} ({details})"
        if message:
            return message
        return str(error)

    def _ensure_process_alive(self) -> None:
        exit_code = self._process.poll()
        if exit_code is not None:
            raise RuntimeError(f"codex app-server exited unexpectedly with code {exit_code}.")


app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

codex_client: CodexAppServerClient | None = None
window_registry: BrowserWindowRegistry | None = None

_new_thread_lock = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def initialize_runtime() -> dict[str, Any]:
    global APP_CONFIG, FONT_FILE_PATHS, codex_client, window_registry

    if APP_CONFIG is not None and codex_client is not None and window_registry is not None:
        return APP_CONFIG

    config = load_wodex_config()
    font_file_paths = {
        style_name: resolve_config_path(path_value)
        for style_name, path_value in config["ui"]["fonts"]["files"].items()
    }
    next_codex_client = CodexAppServerClient(
        default_cwd=config["codex"]["cwd"],
        thread_list_limit=config["codex"]["threadListLimit"],
        rpc_log_directory=resolve_config_path(config["logging"]["directory"]),
    )
    next_window_registry = BrowserWindowRegistry(
        stale_seconds=config["windows"]["staleSeconds"],
        shutdown_grace_seconds=config["windows"]["shutdownGraceSeconds"],
    )

    APP_CONFIG = config
    FONT_FILE_PATHS = font_file_paths
    codex_client = next_codex_client
    window_registry = next_window_registry
    atexit.register(codex_client.close)
    atexit.register(window_registry.stop)
    return config


def require_runtime() -> tuple[dict[str, Any], CodexAppServerClient, BrowserWindowRegistry]:
    if APP_CONFIG is None or codex_client is None or window_registry is None:
        raise RuntimeError("Wodex runtime is not initialized.")
    return APP_CONFIG, codex_client, window_registry


def get_thread_lock(thread_id: str) -> threading.Lock:
    with _thread_locks_guard:
        lock = _thread_locks.get(thread_id)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[thread_id] = lock
        return lock


def collapse_text(text: str | None) -> str:
    return " ".join((text or "").split())


def thread_title(thread: dict[str, Any]) -> str:
    title = collapse_text(thread.get("name"))
    if title:
        return title
    preview = collapse_text(thread.get("preview"))
    if preview:
        return preview
    return "New Conversation"


def serialize_thread(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": thread.get("id"),
        "title": thread_title(thread),
        "preview": collapse_text(thread.get("preview")),
        "name": thread.get("name"),
        "createdAt": thread.get("createdAt"),
        "updatedAt": thread.get("updatedAt"),
        "cwd": thread.get("cwd"),
        "source": thread.get("source"),
        "status": thread.get("status", {}),
        "modelProvider": thread.get("modelProvider"),
        "ephemeral": thread.get("ephemeral"),
    }


def set_shutdown_callback(callback: Callable[[], None] | None) -> None:
    _, _, runtime_window_registry = require_runtime()
    runtime_window_registry.set_shutdown_callback(callback)


def _read_window_id() -> str | None:
    payload = request.get_json(silent=True) or {}
    window_id = str(payload.get("windowId", "")).strip()
    return window_id or None


@app.get("/")
def index() -> str:
    config, _, _ = require_runtime()
    return render_template(
        "index.html",
        ui_fonts=config["ui"]["fonts"],
        ui_colors=config["ui"]["colorscheme"],
        window_heartbeat_ms=int(config["windows"]["heartbeatSeconds"] * 1000),
    )


@app.get("/api/threads")
def api_threads() -> Any:
    _, runtime_codex_client, _ = require_runtime()
    try:
        threads = [serialize_thread(thread) for thread in runtime_codex_client.list_threads()]
    except JsonRpcError as exc:
        return jsonify({"error": f"JSON-RPC error {exc.code}: {exc.message}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"threads": threads})


@app.get("/api/threads/<thread_id>")
def api_thread_read(thread_id: str) -> Any:
    _, runtime_codex_client, _ = require_runtime()
    try:
        thread = runtime_codex_client.read_thread(thread_id, include_turns=True)
        messages = runtime_codex_client.thread_messages(thread)
        live = runtime_codex_client.get_live_state(thread_id)
    except JsonRpcError as exc:
        return jsonify({"error": f"JSON-RPC error {exc.code}: {exc.message}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "thread": serialize_thread(thread),
            "messages": messages,
            "live": live,
        }
    )


@app.post("/api/chat")
def api_chat() -> Any:
    _, runtime_codex_client, _ = require_runtime()
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("message", "")).strip()
    thread_id = str(payload.get("threadId", "") or "").strip() or None
    client_message_id = str(payload.get("clientMessageId", "") or "").strip() or None
    if not prompt:
        return jsonify({"error": "Message is required."}), 400

    thread_lock = get_thread_lock(thread_id) if thread_id else _new_thread_lock

    with thread_lock:
        try:
            result = runtime_codex_client.send_prompt(thread_id, prompt, client_message_id)
            result["live"] = runtime_codex_client.get_live_state(result["threadId"])
        except JsonRpcError as exc:
            return jsonify({"error": f"JSON-RPC error {exc.code}: {exc.message}"}), 502
        except TimeoutError as exc:
            return jsonify({"error": str(exc), "threadId": thread_id}), 504
        except RuntimeError as exc:
            return jsonify({"error": str(exc), "threadId": thread_id}), 500

    return jsonify(result)


@app.get("/api/events")
def api_events() -> Response:
    _, runtime_codex_client, _ = require_runtime()
    subscriber_id, subscriber_queue = runtime_codex_client.subscribe_events()

    @stream_with_context
    def event_stream() -> Iterator[str]:
        yield "retry: 1000\n\n"
        try:
            while True:
                try:
                    event = subscriber_queue.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            runtime_codex_client.unsubscribe_events(subscriber_id)

    response = Response(event_stream(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.post("/api/window/open")
def api_window_open() -> Any:
    _, _, runtime_window_registry = require_runtime()
    window_id = _read_window_id()
    if window_id is None:
        return jsonify({"error": "windowId is required."}), 400
    active_windows = runtime_window_registry.touch(window_id)
    return jsonify({"ok": True, "activeWindows": active_windows})


@app.post("/api/window/heartbeat")
def api_window_heartbeat() -> Any:
    _, _, runtime_window_registry = require_runtime()
    window_id = _read_window_id()
    if window_id is None:
        return jsonify({"error": "windowId is required."}), 400
    active_windows = runtime_window_registry.touch(window_id)
    return jsonify({"ok": True, "activeWindows": active_windows})


@app.post("/api/window/close")
def api_window_close() -> Any:
    _, _, runtime_window_registry = require_runtime()
    window_id = _read_window_id()
    if window_id is None:
        return jsonify({"error": "windowId is required."}), 400
    active_windows = runtime_window_registry.close(window_id)
    return jsonify({"ok": True, "activeWindows": active_windows})


@app.get("/assets/fonts/<style>")
def font_asset(style: str) -> Any:
    require_runtime()
    font_path = FONT_FILE_PATHS.get(style)
    if font_path is None:
        abort(404)
    return send_file(font_path, conditional=True)


def serve_app(host: str | None = None, port: int | None = None) -> None:
    config = initialize_runtime()
    serve(
        app,
        host=host or config["server"]["host"],
        port=config["server"]["port"] if port is None else port,
    )


if __name__ == "__main__":
    try:
        serve_app()
    except WodexConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
