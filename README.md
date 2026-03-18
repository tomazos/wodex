# Wodex

Wodex is a minimal Flask web app that talks to a local `codex app-server` process over stdio and exposes a basic browser chat UI.
It is served locally with Waitress rather than Flask's development server.

## What it does

- Starts one local `codex app-server` subprocess inside the Flask backend
- Lists stored Codex threads through `thread/list`
- Lets each browser window keep its own currently selected thread in browser `sessionStorage`
- Sends each prompt with `turn/start`
- Reads the finished turn back from Codex history and shows the assistant reply in the page

## Run it

```bash
wodex
```

Or directly:

```bash
/home/zos/wodex/bin/wodex
```

The launcher picks a random available localhost port, prints the final URL, and opens it in your browser automatically.

## Optional environment variables

```bash
WODEX_HOST=127.0.0.1
PORT=0
WODEX_CWD=/home/zos
WODEX_TURN_TIMEOUT=300
WODEX_STARTUP_TIMEOUT=10
```

`PORT=0` means "pick a random available port", which is the default when launched through `run.py` or `bin/wodex`.

`WODEX_CWD` controls the working directory passed to new Codex threads. If you do not set it, Wodex defaults to your home directory.

## Important note

Wodex uses your local Codex installation and inherits its auth/config behavior unless you change the backend startup code. With your current Codex config, that means it is not sandboxed.
