"""Where Codex Local keeps its configuration and runtime state.

macOS keeps both under ``~/Library/Application Support/Codex Local`` so a user has
one place to look. Every other platform follows the XDG base directory spec.

The configuration file carries endpoint credentials and the runtime directory
carries session receipts, so both are created owner-only and Codex Local refuses to
read a configuration file that anyone else can open.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

APP_NAME = "Codex Local"
CONFIG_FILENAME = "config.json"

# Set CODEX_LOCAL_MIGRATE_CONFIG_FROM to an existing configuration file and Codex Local
# copies it into place on first run. There is deliberately no built-in list of
# earlier locations: this configuration holds endpoint credentials, and reading
# a guessed path on somebody else's machine is not a guess worth making.
MIGRATE_FROM_ENV = "CODEX_LOCAL_MIGRATE_CONFIG_FROM"


def _is_darwin() -> bool:
    return sys.platform == "darwin"


def _xdg_dir(variable: str, default: Path) -> Path:
    raw = os.environ.get(variable)
    if raw and Path(raw).is_absolute():
        return Path(raw) / "codex_local"
    return default / "codex_local"


def config_dir() -> Path:
    """Return the directory holding ``config.json``."""
    override = os.environ.get("CODEX_LOCAL_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if _is_darwin():
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return _xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config")


def runtime_dir() -> Path:
    """Return the directory holding session receipts, caches and dashboards."""
    override = os.environ.get("CODEX_LOCAL_RUNTIME_DIR")
    if override:
        return Path(override).expanduser()
    if _is_darwin():
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return _xdg_dir("XDG_STATE_HOME", Path.home() / ".local" / "state")


def config_file() -> Path:
    """Return the path to the model configuration file."""
    return config_dir() / CONFIG_FILENAME


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) owner-only, and return it."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        # A directory on a filesystem without POSIX modes is still usable; the
        # file-level checks below remain the real guard.
        pass
    return path


def legacy_config_paths() -> tuple[Path, ...]:
    """Return a configuration file to migrate from, if one was named."""
    raw = os.environ.get(MIGRATE_FROM_ENV)
    if not raw:
        return ()
    return (Path(raw).expanduser(),)
