import atexit
import json
import queue
import subprocess
import sys
import threading
import time
from itertools import count
from pathlib import Path
from typing import Callable
from typing import Any
from typing import Iterator

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, stream_with_context
from waitress import serve

from wodex_config import WodexConfigError, load_wodex_config, resolve_config_path

PROJECT_ROOT = Path(__file__).resolve().parent
APP_CONFIG: dict[str, Any] | None = None
FONT_FILE_PATHS: dict[str, Path] = {}


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()

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

    def thread_messages(self, thread: dict[str, Any]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
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
                    "agentMessages": [],
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
                "agentOrder": [],
                "agentItems": {},
            }
            self._live_threads[thread_id] = state
        return state

    def _serialize_live_state_locked(self, state: dict[str, Any]) -> dict[str, Any]:
        agent_messages = []
        for item_id in state["agentOrder"]:
            item = state["agentItems"].get(item_id)
            if item is None or not item.get("visible"):
                continue
            agent_messages.append(
                {
                    "itemId": item_id,
                    "text": item.get("text", ""),
                    "phase": item.get("phase"),
                    "completed": item.get("completed", False),
                }
            )
        return {
            "activeTurnId": state.get("activeTurnId"),
            "thinkingSince": state.get("thinkingSince"),
            "agentMessages": agent_messages,
        }

    def _mark_turn_started(self, thread_id: str, turn_id: str, publish_event: bool = True) -> None:
        with self._live_state_lock:
            state = self._get_or_create_live_state_locked(thread_id)
            state["activeTurnId"] = turn_id
            state["thinkingSince"] = time.time()
            state["agentOrder"] = []
            state["agentItems"] = {}
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
            existing = state["agentItems"].get(item_id)
            if existing is None:
                existing = {
                    "text": "",
                    "phase": phase,
                    "completed": False,
                    "visible": False,
                }
                state["agentItems"][item_id] = existing
                state["agentOrder"].append(item_id)
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
            item = state["agentItems"].get(item_id)
            if item is None:
                item = {
                    "text": "",
                    "phase": None,
                    "completed": False,
                    "visible": False,
                }
                state["agentItems"][item_id] = item
                state["agentOrder"].append(item_id)
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
            existing = state["agentItems"].get(item_id)
            if existing is None:
                existing = {
                    "text": "",
                    "phase": phase,
                    "completed": False,
                    "visible": False,
                }
                state["agentItems"][item_id] = existing
                state["agentOrder"].append(item_id)
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
                    "version": "0.1.1",
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
            if thread_id and turn_id and item.get("type") == "agentMessage":
                self._record_agent_message_started(thread_id, turn_id, item)
            return

        if method == "item/agentMessage/delta":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item_id = params.get("itemId")
            delta = params.get("delta", "")
            if thread_id and turn_id and item_id:
                self._record_agent_message_delta(thread_id, turn_id, item_id, delta)
            return

        if method == "item/completed":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            item = params.get("item", {})
            if thread_id and turn_id and item.get("type") == "agentMessage":
                self._record_agent_message_completed(thread_id, turn_id, item)
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
