from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".wodex"
CONFIG_PATH = CONFIG_DIR / "config.json"
SCHEMA_SOURCE_PATH = PROJECT_ROOT / "config.schema.json"
SCHEMA_PATH = CONFIG_DIR / "config.schema.json"
FONT_DIR = CONFIG_DIR / "fonts"
BUNDLED_FONT_DIR = PROJECT_ROOT / "static" / "fonts"

DEFAULT_FONT_FILES = {
    "regular": FONT_DIR / "UbuntuSansMono[wght].ttf",
    "bold": FONT_DIR / "UbuntuSansMono[wght].ttf",
    "italic": FONT_DIR / "UbuntuSansMono-Italic[wght].ttf",
    "boldItalic": FONT_DIR / "UbuntuSansMono-Italic[wght].ttf",
}

DEFAULT_BUNDLED_FONT_FILES = {
    "regular": BUNDLED_FONT_DIR / "UbuntuSansMono[wght].ttf",
    "bold": BUNDLED_FONT_DIR / "UbuntuSansMono[wght].ttf",
    "italic": BUNDLED_FONT_DIR / "UbuntuSansMono-Italic[wght].ttf",
    "boldItalic": BUNDLED_FONT_DIR / "UbuntuSansMono-Italic[wght].ttf",
}


class WodexConfigError(RuntimeError):
    pass


def default_wodex_config() -> dict[str, Any]:
    return {
        "$schema": "./config.schema.json",
        "server": {
            "host": "127.0.0.1",
            "port": 0,
            "startupTimeoutSeconds": 10,
        },
        "codex": {
            "cwd": str(Path.home()),
            "threadListLimit": 200,
        },
        "windows": {
            "heartbeatSeconds": 15,
            "staleSeconds": 120,
            "shutdownGraceSeconds": 3,
        },
        "logging": {
            "directory": "logs",
        },
        "ui": {
            "fonts": {
                "family": "Ubuntu Sans Mono",
                "sizePx": 16,
                "files": {name: str(path) for name, path in DEFAULT_FONT_FILES.items()},
            },
            "colorscheme": {
                "assistantOddBackground": "#dce9e5",
                "assistantEvenBackground": "#e6e1f4",
                "userBackground": "#f4e0da",
                "chatForeground": "#1f1b16",
            },
        },
    }


def resolve_config_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    return (CONFIG_DIR / path).resolve()


def load_wodex_config() -> dict[str, Any]:
    ensure_config_support_files()
    defaults = default_wodex_config()
    schema = json.loads(SCHEMA_SOURCE_PATH.read_text())

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(defaults, indent=2) + "\n")
        return defaults

    try:
        raw_config = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise _build_error(
            [f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}."],
            defaults,
        ) from exc

    if not isinstance(raw_config, dict):
        raise _build_error(
            ["The configuration root must be a JSON object."],
            defaults,
        )

    validator = Draft202012Validator(schema)
    errors = [format_validation_error(error) for error in validator.iter_errors(raw_config)]
    corrected_config = build_corrected_config(schema, defaults, raw_config)
    errors.extend(validate_runtime_config(corrected_config))
    if errors:
        raise _build_error(errors, corrected_config)

    return corrected_config


def ensure_config_support_files() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SCHEMA_SOURCE_PATH, SCHEMA_PATH)

    for name, source_path in DEFAULT_BUNDLED_FONT_FILES.items():
        if not source_path.exists():
            raise WodexConfigError(f"Bundled font asset is missing: {source_path}")
        target_path = DEFAULT_FONT_FILES[name]
        if not target_path.exists():
            shutil.copyfile(source_path, target_path)


def build_corrected_config(
    schema: dict[str, Any],
    defaults: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    corrected = deepcopy(defaults)
    if not isinstance(value, dict):
        return corrected

    for key, default_value in defaults.items():
        if key not in value:
            continue

        schema_property = schema.get("properties", {}).get(key, {})
        candidate = value[key]
        if isinstance(default_value, dict):
            corrected[key] = build_corrected_config(schema_property, default_value, candidate)
            continue

        if matches_schema_type(candidate, schema_property):
            corrected[key] = candidate

    return corrected


def matches_schema_type(value: Any, schema: dict[str, Any]) -> bool:
    expected_type = schema.get("type")
    if expected_type is None:
        return True
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "boolean":
        return isinstance(value, bool)
    return True


def validate_runtime_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for style_name, font_file in config["ui"]["fonts"]["files"].items():
        resolved_path = resolve_config_path(font_file)
        if not resolved_path.exists():
            errors.append(
                f"Font file for `ui.fonts.files.{style_name}` does not exist: {resolved_path}"
            )
        elif not resolved_path.is_file():
            errors.append(
                f"Font file for `ui.fonts.files.{style_name}` is not a file: {resolved_path}"
            )
    log_dir = resolve_config_path(config["logging"]["directory"])
    if log_dir.exists() and not log_dir.is_dir():
        errors.append(
            f"Logging directory for `logging.directory` is not a directory: {log_dir}"
        )
    return errors


def format_validation_error(error) -> str:
    path = format_error_path(error.absolute_path)
    if error.validator == "required":
        missing_key = str(error.message).split("'")[1]
        return f"Missing option at {path}: `{missing_key}`."
    if error.validator == "additionalProperties":
        unknown_key = str(error.message).split("'")[1]
        return f"Unknown option at {path}: `{unknown_key}`."
    if error.validator == "type":
        actual_type = type(error.instance).__name__
        return f"Wrong type at {path}: expected `{error.validator_value}`, got `{actual_type}`."
    if error.validator == "enum":
        return f"Invalid value at {path}: {error.message}"
    if error.validator in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}:
        return f"Out-of-range value at {path}: {error.message}"
    return f"Invalid value at {path}: {error.message}"


def format_error_path(parts) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _build_error(errors: list[str], corrected_config: dict[str, Any]) -> WodexConfigError:
    error_lines = "\n".join(f"- {error}" for error in errors)
    corrected_json = json.dumps(corrected_config, indent=2)
    message = (
        f"Invalid Wodex configuration at `{CONFIG_PATH}`.\n\n"
        f"{error_lines}\n\n"
        f"Schema: `{SCHEMA_PATH}`\n\n"
        "Corrected configuration you can copy over your current file:\n"
        f"{corrected_json}\n"
    )
    return WodexConfigError(message)
