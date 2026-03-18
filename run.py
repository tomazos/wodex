import argparse
import os
import socket
import threading
import time
import webbrowser

from waitress.server import create_server

from app import app


DEFAULT_HOST = os.environ.get("WODEX_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PORT", "0"))
STARTUP_TIMEOUT_SECONDS = float(os.environ.get("WODEX_STARTUP_TIMEOUT", "10"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the Wodex local web UI.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host for the local web server.")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
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


def main() -> None:
    args = parse_args()
    server = create_server(app, host=args.host, port=args.port)
    actual_port = server.effective_port
    browse_host = browser_host_for(args.host)
    url = f"http://{browse_host}:{actual_port}"

    server_thread = threading.Thread(target=server.run, name="wodex-waitress", daemon=True)
    server_thread.start()

    try:
        wait_for_server(browse_host, actual_port, STARTUP_TIMEOUT_SECONDS)
        print(f"Wodex listening on {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url, new=2)

        while server_thread.is_alive():
            server_thread.join(timeout=1)
    except KeyboardInterrupt:
        print("\nStopping Wodex...", flush=True)
    finally:
        server.close()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
