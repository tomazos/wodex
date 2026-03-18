import argparse
import socket
import sys
import threading
import time
import webbrowser

from waitress.server import create_server

from app import app, initialize_runtime, set_shutdown_callback
from wodex_config import WodexConfigError


def parse_args(config: dict) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the Wodex local web UI.")
    parser.add_argument(
        "--host",
        default=config["server"]["host"],
        help="Bind host for the local web server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config["server"]["port"],
        help="Bind port for the local web server. Defaults to 0 for a random available port.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start the server without opening a browser tab.",
    )
    return parser.parse_args()


def browser_host_for(bind_host: str) -> str:
    if bind_host in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    return bind_host


def wait_for_server(host: str, port: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Wodex did not start listening on {host}:{port} in time.")
            time.sleep(0.05)


def shutdown_server(server) -> None:
    server.close()
    task_dispatcher = getattr(server, "task_dispatcher", None)
    if task_dispatcher is not None:
        task_dispatcher.shutdown()


def main() -> int:
    try:
        config = initialize_runtime()
    except WodexConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    args = parse_args(config)
    server = create_server(app, host=args.host, port=args.port)
    actual_port = server.effective_port
    browse_host = browser_host_for(args.host)
    url = f"http://{browse_host}:{actual_port}"
    shutdown_requested = threading.Event()

    def request_shutdown() -> None:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        print("\nLast Wodex browser window closed. Stopping Wodex...", flush=True)
        server.close()

    set_shutdown_callback(request_shutdown)

    server_thread = threading.Thread(target=server.run, name="wodex-waitress", daemon=True)
    server_thread.start()

    try:
        wait_for_server(browse_host, actual_port, config["server"]["startupTimeoutSeconds"])
        print(f"Wodex listening on {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url, new=2)

        while server_thread.is_alive() and not shutdown_requested.is_set():
            server_thread.join(timeout=1)
    except KeyboardInterrupt:
        print("\nStopping Wodex...", flush=True)
    finally:
        set_shutdown_callback(None)
        shutdown_server(server)
        server_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
