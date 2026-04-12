"""
Runtime-configurable settings with JSON persistence.

Settings are loaded from settings.json at startup and can be updated
at runtime via the web API. All changes are immediately persisted to disk.
Thread-safe for concurrent access from sensor loop, adaptive engine, and web server.
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import SETTINGS_PATH

logger = logging.getLogger(__name__)

# Validation ranges: (min, max) for numeric fields
_RANGES: Dict[str, tuple] = {
    "dim_delay": (5.0, 600.0),
    "dim_level": (1, 50),
    "absence_timeout": (5.0, 600.0),
    "eval_interval": (30, 3600),
    "brightness_threshold": (1, 50),
    "cct_threshold": (10, 1000),
    "dali_command_gap_s": (0.0, 10.0),
    "brightness_feedback_window_s": (0.0, 30.0),
    "brightness_feedback_min_lux_delta": (0.0, 100.0),
    "max_brightness_step_pct": (1.0, 100.0),
    "lamp_lux_at_100pct": (0.0, 2000.0),
    "nominal_power_watts": (1.0, 500.0),
    "weather_lat": (-90.0, 90.0),
    "weather_lon": (-180.0, 180.0),
}


@dataclass
class Settings:
    # Baseline mode: vacancy dim-then-off
    dim_delay: float = 60.0            # seconds at dim level before turning off
    dim_level: int = 10                # percent brightness for warning dim

    # AI adaptive engine
    absence_timeout: float = 60.0      # seconds before turning off on vacancy
    eval_interval: int = 300           # seconds between AI evaluations
    brightness_threshold: int = 5      # minimum % change to trigger adjustment
    cct_threshold: int = 100           # minimum Kelvin change to trigger adjustment
    dali_command_gap_s: float = 0.75   # settle time between CCT and brightness sends
    brightness_feedback_window_s: float = 2.5  # seconds to observe lux after brightness
    brightness_feedback_min_lux_delta: float = 3.0  # minimum lux_smooth movement to confirm
    max_brightness_step_pct: float = 15.0  # max % brightness change per adaptive eval cycle
    lamp_lux_at_100pct: float = 400.0  # estimated lamp lux contribution at sensor at 100%

    # Energy estimation
    nominal_power_watts: float = 40.0

    # Weather API (optional)
    weather_api_key: str = ""
    weather_location: str = ""         # city name or "lat,lon"
    weather_lat: Optional[float] = None
    weather_lon: Optional[float] = None
    weather_location_label: str = ""

    # OpenAI API (optional)
    openai_api_key: str = ""
    openai_model: str = ""             # blank = use env/default model

    # Sensor (ESP32) USB serial port. Blank = auto-detect.
    sensor_port: str = ""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from disk, falling back to defaults."""
        settings = cls()
        should_save = False
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key, val in data.items():
                    if hasattr(settings, key) and not key.startswith("_"):
                        setattr(settings, key, val)
                logger.info("Settings loaded from %s", SETTINGS_PATH)
            except Exception as exc:
                logger.warning("Failed to load settings, using defaults: %s", exc)
        else:
            logger.info("No settings file found, using defaults.")

        env_openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not settings.openai_api_key and env_openai_key:
            settings.openai_api_key = env_openai_key
            should_save = True
            logger.info("Imported OPENAI_API_KEY into %s", SETTINGS_PATH)

        if not SETTINGS_PATH.exists() or should_save:
            settings.save()
        return settings

    def save(self) -> None:
        """Persist current settings to disk."""
        try:
            data = self.to_dict()
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save settings: %s", exc)

    def update(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        """Apply partial updates with validation. Returns the new state.

        Raises ValueError if any value is out of range.
        """
        with self._lock:
            errors = []
            for key, val in partial.items():
                if key.startswith("_") or not hasattr(self, key):
                    continue

                # Type coercion
                if key in (
                    "dim_delay",
                    "absence_timeout",
                    "dali_command_gap_s",
                    "brightness_feedback_window_s",
                    "brightness_feedback_min_lux_delta",
                    "max_brightness_step_pct",
                    "lamp_lux_at_100pct",
                    "nominal_power_watts",
                    "weather_lat",
                    "weather_lon",
                ):
                    val = None if val in (None, "") else float(val)
                elif key in ("dim_level", "eval_interval", "brightness_threshold", "cct_threshold"):
                    val = int(val)
                elif key in (
                    "weather_api_key",
                    "weather_location",
                    "weather_location_label",
                    "openai_api_key",
                    "openai_model",
                    "sensor_port",
                ):
                    val = "" if val is None else str(val)

                # Range validation for numeric fields
                if key in _RANGES and val is not None:
                    lo, hi = _RANGES[key]
                    if not (lo <= val <= hi):
                        errors.append(f"{key}: {val} out of range [{lo}, {hi}]")
                        continue

                setattr(self, key, val)

            if errors:
                raise ValueError("; ".join(errors))

            self.save()
            return self.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (excluding internal fields)."""
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if not item.name.startswith("_")
        }
