# Wodex

Wodex is a local web UI for Codex. It starts a local `codex app-server` process, lists available Codex threads, and lets you browse and continue them in a browser.

The backend is a small Flask app served locally with Waitress rather than Flask's development server.

## Installation

Requirements:

- Python 3
- `codex` installed and available on `PATH`
- a working local Codex login/config

Set up a local virtual environment and install dependencies:

```bash
git clone https://github.com/tomazos/wodex.git
cd wodex
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional: add the launcher to your shell `PATH`:

```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/bin/wodex" ~/.local/bin/wodex
```

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
./bin/wodex
```

The launcher picks a random available localhost port, prints the final URL, and opens it in your browser automatically.

## Optional environment variables

```bash
WODEX_HOST=127.0.0.1
PORT=0
WODEX_CWD=/path/to/project
WODEX_TURN_TIMEOUT=300
WODEX_STARTUP_TIMEOUT=10
```

`PORT=0` means "pick a random available port", which is the default when launched through `run.py` or `bin/wodex`.

`WODEX_CWD` controls the working directory passed to new Codex threads. If you do not set it, Wodex defaults to your home directory.

## Important note

Wodex uses your local Codex installation and inherits its auth/config behavior unless you change the backend startup code. If your local Codex config is unsandboxed, Wodex will be too.
