import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from itertools import count
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request
from waitress import serve


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CWD = os.environ.get("WODEX_CWD", str(Path.home()))
HOST = os.environ.get("WODEX_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))
TURN_TIMEOUT_SECONDS = int(os.environ.get("WODEX_TURN_TIMEOUT", "300"))
THREAD_LIST_LIMIT = int(os.environ.get("WODEX_THREAD_LIST_LIMIT", "200"))


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CodexTurnError(RuntimeError):
    pass


class CodexAppServerClient:
    def __init__(self, default_cwd: str) -> None:
        self.default_cwd = default_cwd
        self._id_counter = count(1)
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._turn_condition = threading.Condition()
        self._completed_turns: dict[tuple[str, str], dict[str, Any]] = {}
        self._turn_errors: dict[tuple[str, str], dict[str, Any]] = {}
        self._loaded_threads: set[str] = set()
        self._loaded_threads_lock = threading.Lock()

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

    def list_threads(self, limit: int = THREAD_LIST_LIMIT) -> list[dict[str, Any]]:
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

    def read_history(self, thread_id: str) -> list[dict[str, str]]:
        return self._thread_to_messages(self.read_thread(thread_id, include_turns=True))

    def run_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        result = self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        turn_id = result["turn"]["id"]
        turn = self._wait_for_turn(thread_id, turn_id, TURN_TIMEOUT_SECONDS)
        final_turn = self._read_turn_from_history(thread_id, turn_id) or turn

        if final_turn.get("status") != "completed":
            error = final_turn.get("error") or self._turn_errors.get((thread_id, turn_id))
            error_message = self._format_turn_error(error) or "Codex did not complete the turn."
            raise CodexTurnError(error_message)

        reply = self._extract_assistant_text(final_turn)
        if not reply:
            raise CodexTurnError("Codex completed the turn without a final assistant message.")

        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "reply": reply,
        }

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "wodex",
                    "title": "Wodex",
                    "version": "0.1.0",
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

        if method == "turn/completed":
            thread_id = params.get("threadId")
            turn = params.get("turn", {})
            turn_id = turn.get("id")
            if thread_id and turn_id:
                with self._turn_condition:
                    self._completed_turns[(thread_id, turn_id)] = turn
                    self._turn_condition.notify_all()
            return

        if method == "error":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            if thread_id and turn_id:
                with self._turn_condition:
                    self._turn_errors[(thread_id, turn_id)] = params.get("error", {})
                    self._turn_condition.notify_all()

    def _wait_for_turn(self, thread_id: str, turn_id: str, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        key = (thread_id, turn_id)

        with self._turn_condition:
            while True:
                completed = self._completed_turns.get(key)
                if completed is not None:
                    return completed

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._turn_condition.wait(timeout=remaining)

        history_turn = self._read_turn_from_history(thread_id, turn_id)
        if history_turn is not None:
            return history_turn

        raise TimeoutError("Timed out waiting for the Codex turn to finish.")

    def _read_turn_from_history(self, thread_id: str, turn_id: str) -> dict[str, Any] | None:
        result = self._request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": True,
            },
        )
        turns = result["thread"].get("turns", [])
        for turn in turns:
            if turn.get("id") == turn_id:
                return turn
        return None

    def _thread_to_messages(self, thread: dict[str, Any]) -> list[dict[str, str]]:
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

    def _extract_assistant_text(self, turn: dict[str, Any]) -> str:
        agent_messages = [
            item.get("text", "").strip()
            for item in turn.get("items", [])
            if item.get("type") == "agentMessage" and item.get("text")
        ]
        if not agent_messages:
            return ""
        return agent_messages[-1]

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

codex_client = CodexAppServerClient(DEFAULT_CWD)
atexit.register(codex_client.close)

_new_thread_lock = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


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


@app.get("/")
def index() -> str:
    return render_template("index.html", default_cwd=codex_client.default_cwd)


@app.get("/api/threads")
def api_threads() -> Any:
    try:
        threads = [serialize_thread(thread) for thread in codex_client.list_threads()]
    except JsonRpcError as exc:
        return jsonify({"error": f"JSON-RPC error {exc.code}: {exc.message}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"cwd": codex_client.default_cwd, "threads": threads})


@app.get("/api/threads/<thread_id>")
def api_thread_read(thread_id: str) -> Any:
    try:
        thread = codex_client.read_thread(thread_id, include_turns=True)
        messages = codex_client._thread_to_messages(thread)
    except JsonRpcError as exc:
        return jsonify({"error": f"JSON-RPC error {exc.code}: {exc.message}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "thread": serialize_thread(thread),
            "messages": messages,
        }
    )


@app.post("/api/chat")
def api_chat() -> Any:
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("message", "")).strip()
    thread_id = str(payload.get("threadId", "") or "").strip() or None
    if not prompt:
        return jsonify({"error": "Message is required."}), 400

    thread_lock = get_thread_lock(thread_id) if thread_id else _new_thread_lock

    with thread_lock:
        try:
            active_thread_id = codex_client.ensure_thread(thread_id)
            result = codex_client.run_turn(active_thread_id, prompt)
            thread = codex_client.read_thread(active_thread_id, include_turns=True)
            messages = codex_client._thread_to_messages(thread)
        except JsonRpcError as exc:
            return jsonify({"error": f"JSON-RPC error {exc.code}: {exc.message}"}), 502
        except CodexTurnError as exc:
            return jsonify({"error": str(exc), "threadId": thread_id}), 500
        except TimeoutError as exc:
            return jsonify({"error": str(exc), "threadId": thread_id}), 504
        except RuntimeError as exc:
            return jsonify({"error": str(exc), "threadId": thread_id}), 500

    return jsonify(
        {
            "threadId": active_thread_id,
            "thread": serialize_thread(thread),
            "reply": result["reply"],
            "messages": messages,
        }
    )


def serve_app() -> None:
    serve(app, host=HOST, port=PORT)


if __name__ == "__main__":
    serve_app()
