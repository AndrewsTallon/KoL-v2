"""
Centralized path resolution for both development and PyInstaller-frozen builds.

- Read-only assets (static/) are resolved from the bundle directory.
- Writable data (telemetry, models, configs) are resolved to a 'data' folder
  next to the executable (frozen) or the package directory (development).
"""

import sys
from pathlib import Path


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _bundle_dir() -> Path:
    """Read-only bundled assets (static files)."""
    if _is_frozen():
        return Path(sys._MEIPASS) / "dalicontrol"
    return Path(__file__).parent


def _data_dir() -> Path:
    """Writable data (configs, telemetry, models)."""
    if _is_frozen():
        return Path(sys.executable).parent / "data"
    return Path(__file__).parent


# Read-only
STATIC_DIR = _bundle_dir() / "static"

# Writable
TELEM_DIR = _data_dir() / "telemetry"
MODELS_DIR = _data_dir() / "models"
STATE_PATH = _data_dir() / "state.json"
SETTINGS_PATH = _data_dir() / "settings.json"
PREFERENCES_PATH = _data_dir() / "preferences.json"
