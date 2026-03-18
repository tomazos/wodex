# Wodex

Version: `0.1.1`

Wodex is a local web UI for Codex, written by Codex. It starts a local `codex app-server` process, lists available Codex threads, and lets you browse and continue them in a browser.

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

## Configuration

On first launch, Wodex creates:

- `~/.wodex/config.json`
- `~/.wodex/config.schema.json`
- `~/.wodex/fonts/`

The default UI font is the bundled Ubuntu Sans Mono font. The bundled font files are copied into `~/.wodex/fonts/` so the config can refer to them directly. Relative file paths inside the config are resolved from `~/.wodex`.
The bundled font assets are shipped under the Ubuntu Font Licence 1.0, with the license text included in `static/fonts/`.

Wodex validates `~/.wodex/config.json` against `~/.wodex/config.schema.json` on startup. If validation fails, Wodex exits with a helpful error message and prints a corrected config you can copy over your current file.

Default config:

```json
{
  "$schema": "./config.schema.json",
  "server": {
    "host": "127.0.0.1",
    "port": 0,
    "startupTimeoutSeconds": 10
  },
  "codex": {
    "cwd": "/path/to/project",
    "threadListLimit": 200
  },
  "windows": {
    "heartbeatSeconds": 15,
    "staleSeconds": 120,
    "shutdownGraceSeconds": 3
  },
  "ui": {
    "fonts": {
      "family": "Ubuntu Sans Mono",
      "sizePx": 16,
      "files": {
        "regular": "fonts/UbuntuSansMono[wght].ttf",
        "bold": "fonts/UbuntuSansMono[wght].ttf",
        "italic": "fonts/UbuntuSansMono-Italic[wght].ttf",
        "boldItalic": "fonts/UbuntuSansMono-Italic[wght].ttf"
      }
    },
    "colorscheme": {
      "assistantOddBackground": "#dce9e5",
      "assistantEvenBackground": "#e6e1f4",
      "userBackground": "#f4e0da",
      "chatForeground": "#1f1b16"
    }
  }
}
```

Configuration keys:

- `server.host`: bind host for the local web server
- `server.port`: bind port, with `0` meaning "pick a random available port"
- `server.startupTimeoutSeconds`: how long the launcher waits for the server to come up
- `codex.cwd`: working directory passed to new Codex threads
- `codex.threadListLimit`: maximum number of threads loaded into the session list
- `windows.heartbeatSeconds`: browser heartbeat interval used to detect open windows
- `windows.staleSeconds`: how long a quiet window can go before Wodex treats it as gone
- `windows.shutdownGraceSeconds`: grace period before Wodex exits after the last window closes
- `ui.fonts.family`: CSS font family name exposed to the page
- `ui.fonts.sizePx`: base UI font size
- `ui.fonts.files.*`: font files to serve for regular, bold, italic, and bold-italic faces
- `ui.colorscheme.*`: chat colors for assistant odd/even rows, user rows, and chat text

## What it does

- Starts one local `codex app-server` subprocess inside the Flask backend
- Lists stored Codex threads through `thread/list`
- Lets each browser window keep its own currently selected thread in browser `sessionStorage`
- Sends prompts with `turn/start` when idle and `turn/steer` when a turn is already active
- Streams live turn and assistant-message updates to the browser over server-sent events
- Reads persisted thread history through `thread/read` when loading or refreshing a thread
- Shuts down automatically after the last Wodex browser window closes

## Run it

```bash
wodex
```

Or directly:

```bash
./bin/wodex
```

The launcher picks a random available localhost port, prints the final URL, opens it in your browser automatically, and exits after the last Wodex browser window closes.

Launcher flags:

- `--host`: override `server.host`
- `--port`: override `server.port`
- `--no-browser`: start without opening a browser tab

## Important note

Wodex uses your local Codex installation and inherits its auth/config behavior unless you change the backend startup code. If your local Codex config is unsandboxed, Wodex will be too.

## Change Log

### 0.1.1

- Prompt input now sends on Enter, inserts a newline on Shift+Enter, and no longer shows a Send button
- Session list keeps its own scroll area and is ordered by most recently updated thread first
- Chat transcript keeps its own scroll area, separate from the session header and prompt composer
- Session header remains visible at the top of the window while the chat transcript scrolls
- Prompt composer remains visible at the bottom of the window while the chat transcript scrolls
- Chat and prompt panes now use a monospaced font, and the session list matches that font
- Chat entries are now borderless, left-aligned, and distinguished by user color plus alternating assistant colors
- The prompt composer is simplified to just the textarea, without the extra prompt label or reply hint text
- The sidebar intro text and thread browser tag are removed, and the Wodex title now links to the GitHub repository
- Session list entries now show created and updated timestamps only, and source labels like `cli` and `vscode` are hidden from the UI
- The left Wodex header and the current-session header now use the same fixed height, and the session list can be collapsed with a triangle toggle
- Status messages no longer leave a persistent `Ready` label in the chat pane when Wodex is idle
- Session timestamps now use `YYYY-MM-DD HH:MM` formatting throughout the UI
- Chat entries are now rectangular and flush-stacked, and short transcripts anchor to the bottom above the prompt pane
- The prompt field no longer exposes a manual resize handle and now auto-expands to fit its content up to half the available conversation height
- The current session header now shows a slightly smaller title with the session id beneath it instead of repeating the title preview and updated timestamp
- Wodex now auto-creates and validates `~/.wodex/config.json`, copies `config.schema.json` into `~/.wodex`, and exits with a corrected config suggestion when validation fails
- The UI font is now configured from `~/.wodex/config.json`, with bundled Ubuntu Sans Mono font files copied into `~/.wodex/fonts` by default
- Chat colors for user rows, assistant odd/even rows, and chat foreground text are now configurable through `ui.colorscheme`
- The `New threads start in ...` note is removed from the sidebar, and the `New Chat` and `Refresh` controls are now compact `+` and `↻` buttons
- The Sessions toggle now sits beside the new-chat and refresh buttons at the bottom of the left header, and the sidebar is widened so thread timestamps stay on one line
- The chat log scroll behavior is restored, and short transcripts still anchor to the bottom without blocking manual scrolling
- The active session title in the top header is now capped at 60 characters and gains an ellipsis when truncated
- The session list is tighter again, with less horizontal space around the timestamp arrow and a narrower left pane
- The Sessions, new-chat, and refresh controls now sit inside one shared rectangular control group instead of a bordered standalone Sessions button
- When the session list is collapsed, the header split stays fixed while the chat pane expands left to fill the freed space
- Codex turns no longer time out in Wodex, and `codex.turnTimeoutSeconds` has been removed from the config schema
- Wodex now fills the full browser window instead of rendering inside a rounded inset document-style shell
- The left header controls now use a flat full-width strip with top and bottom rules instead of boxed individual buttons
- The session list is narrower again, freeing more horizontal space for the chat pane
- Prompt submission now clears the textbox immediately and inserts the user message into the chat log before Codex finishes responding
- Wodex now streams live turn events from app-server, updates in-flight assistant messages from `item/agentMessage/delta`, and shows a running `Thinking...` timer while a turn is active
- Submitting a prompt during an active turn now uses `turn/steer`; submitting when idle still starts a new turn with `turn/start`

### 0.1.0

- Initial public release
- Local web UI for Codex backed by `codex app-server`
- Thread browser with per-window active thread state in browser `sessionStorage`
- Local launcher that picks a random localhost port and opens the browser automatically
