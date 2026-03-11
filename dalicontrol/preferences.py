"""
User lighting preferences with JSON persistence.

Stores questionnaire responses so the AI adaptive engine can use user
preferences from day one (before ML models are trained from telemetry).
Thread-safe for concurrent access.
"""

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict

from .paths import PREFERENCES_PATH

logger = logging.getLogger(__name__)


@dataclass
class UserPreferences:
    # Schedule
    wake_time: str = "07:00"
    sleep_time: str = "23:00"
    work_start: str = "09:00"
    work_end: str = "17:00"

    # Brightness preferences (0-100%)
    morning_brightness: int = 70
    midday_brightness: int = 60
    evening_brightness: int = 50
    night_brightness: int = 30

    # Color temperature preferences (Kelvin)
    warm_cool_preference: str = "neutral"  # "warm", "neutral", "cool"
    morning_cct: int = 4000
    midday_cct: int = 5500
    evening_cct: int = 3000
    night_cct: int = 2700

    # Sensitivity: how aggressively the AI adjusts
    change_sensitivity: str = "medium"  # "low", "medium", "high"

    # Whether the user has completed the questionnaire
    completed: bool = False

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # --- Persistence ---

    @classmethod
    def load(cls) -> "UserPreferences":
        prefs = cls()
        if PREFERENCES_PATH.exists():
            try:
                with open(PREFERENCES_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key, val in data.items():
                    if hasattr(prefs, key) and not key.startswith("_"):
                        setattr(prefs, key, val)
                logger.info("User preferences loaded from %s", PREFERENCES_PATH)
            except Exception as exc:
                logger.warning("Failed to load preferences, using defaults: %s", exc)
        else:
            logger.info("No preferences file found, using defaults.")
        return prefs

    def save(self) -> None:
        try:
            data = self.to_dict()
            with open(PREFERENCES_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info("Preferences saved to %s", PREFERENCES_PATH)
        except Exception as exc:
            logger.error("Failed to save preferences: %s", exc)

    def update(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        """Apply partial updates with validation. Returns the new state."""
        with self._lock:
            for key, val in partial.items():
                if key.startswith("_") or not hasattr(self, key):
                    continue

                # Type coercion
                if key in ("morning_brightness", "midday_brightness",
                           "evening_brightness", "night_brightness",
                           "morning_cct", "midday_cct", "evening_cct", "night_cct"):
                    val = int(val)
                elif key == "completed":
                    val = bool(val)
                elif key in ("wake_time", "sleep_time", "work_start", "work_end",
                             "warm_cool_preference", "change_sensitivity"):
                    val = str(val)

                setattr(self, key, val)

            self.save()
            return self.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}

    # --- Helpers for adaptive engine ---

    def get_period(self, hour: float) -> str:
        """Map a decimal hour to a user-defined period based on their schedule."""
        wake_h = self._parse_time(self.wake_time)
        work_start_h = self._parse_time(self.work_start)
        work_end_h = self._parse_time(self.work_end)
        sleep_h = self._parse_time(self.sleep_time)

        if hour < wake_h or hour >= sleep_h:
            return "night"
        elif hour < work_start_h:
            return "morning"
        elif hour < work_end_h:
            return "midday"
        else:
            return "evening"

    def get_preferred_brightness(self, hour: float) -> float:
        period = self.get_period(hour)
        return float(getattr(self, f"{period}_brightness"))

    def get_preferred_cct(self, hour: float) -> int:
        period = self.get_period(hour)
        return getattr(self, f"{period}_cct")

    def get_sensitivity_thresholds(self) -> tuple:
        """Returns (brightness_threshold_pct, cct_threshold_kelvin)."""
        mapping = {
            "low": (10, 200),
            "medium": (5, 100),
            "high": (2, 50),
        }
        return mapping.get(self.change_sensitivity, (5, 100))

    @staticmethod
    def _parse_time(time_str: str) -> float:
        """Parse 'HH:MM' to decimal hours."""
        try:
            parts = time_str.split(":")
            return int(parts[0]) + int(parts[1]) / 60.0
        except (ValueError, IndexError):
            return 0.0
