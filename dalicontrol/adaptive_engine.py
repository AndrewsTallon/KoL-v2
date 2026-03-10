"""
AI-based adaptive lighting control engine.

Implements the thesis Section 3.3.2 control strategy:
- Learns user preferences from baseline telemetry CSV data
- Evaluates lighting adjustments every 5 minutes
- Applies brightness/CCT thresholds to prevent micro-adjustments
- Handles occupancy-based switching with 60-second absence timeout
"""

import csv
import logging
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from .cct_utils import dtr_to_kelvin, kelvin_to_dtr, level_to_pct
from .lamp_state import LampController

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).with_name("models")
TELEM_DIR = Path(__file__).with_name("telemetry")


class AdaptiveEngine:
    """AI adaptive control that learns from baseline data and adjusts lighting."""

    # Timing (thesis Table XX)
    EVAL_INTERVAL = 300       # 5 minutes between AI evaluations
    ABSENCE_TIMEOUT = 60      # 1 minute before turning off on vacancy

    # Thresholds to prevent micro-adjustments
    BRIGHTNESS_THRESHOLD = 5  # minimum % change to trigger adjustment
    CCT_THRESHOLD = 100       # minimum Kelvin change to trigger adjustment

    def __init__(
        self,
        lamp: LampController,
        lamp_lock: threading.Lock,
        nominal_power_watts: float = 40.0,
    ):
        self.lamp = lamp
        self.lamp_lock = lamp_lock
        self.nominal_power_watts = nominal_power_watts

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # ML models
        self._brightness_model = None
        self._cct_model = None
        self._models_loaded = False

        # Runtime state
        self._last_eval_time = 0.0
        self._last_occupied_time = 0.0
        self._was_occupied = False
        self._turned_off_for_absence = False

        # Last recommended values (for threshold comparison)
        self._current_brightness_pct: Optional[float] = None
        self._current_cct_kelvin: Optional[int] = None

        # Callback for telemetry logging
        self.on_action = None  # callable(action_str, reason_str, rationale_str)

    def train_from_baseline(self, csv_paths: Optional[list] = None) -> bool:
        """Train ML models from baseline telemetry CSV files.

        Returns True if models were trained successfully.
        """
        if csv_paths is None:
            csv_paths = sorted(TELEM_DIR.glob("run_*_baseline.csv"))

        if not csv_paths:
            logger.warning("No baseline CSV files found for training.")
            return False

        try:
            from sklearn.ensemble import RandomForestRegressor
            import joblib
        except ImportError:
            logger.warning("scikit-learn not available; using fallback control.")
            return False

        # Collect training data
        features = []  # [hour_sin, hour_cos, lux]
        brightness_targets = []
        cct_targets = []

        for csv_path in csv_paths:
            try:
                self._load_csv_data(
                    csv_path, features, brightness_targets, cct_targets
                )
            except Exception as exc:
                logger.warning("Error reading %s: %s", csv_path, exc)

        if len(features) < 10:
            logger.warning(
                "Insufficient training data (%d samples). Need at least 10.",
                len(features),
            )
            return False

        logger.info("Training adaptive models on %d samples...", len(features))

        self._brightness_model = RandomForestRegressor(
            n_estimators=50, max_depth=8, random_state=42
        )
        self._brightness_model.fit(features, brightness_targets)

        self._cct_model = RandomForestRegressor(
            n_estimators=50, max_depth=8, random_state=42
        )
        self._cct_model.fit(features, cct_targets)

        # Persist models
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump(self._brightness_model, MODELS_DIR / "brightness_model.joblib")
        joblib.dump(self._cct_model, MODELS_DIR / "cct_model.joblib")

        self._models_loaded = True
        logger.info("Adaptive models trained and saved.")
        return True

    def load_models(self) -> bool:
        """Load previously trained models from disk."""
        try:
            import joblib
        except ImportError:
            return False

        brightness_path = MODELS_DIR / "brightness_model.joblib"
        cct_path = MODELS_DIR / "cct_model.joblib"

        if not brightness_path.exists() or not cct_path.exists():
            return False

        try:
            self._brightness_model = joblib.load(brightness_path)
            self._cct_model = joblib.load(cct_path)
            self._models_loaded = True
            logger.info("Loaded pre-trained adaptive models.")
            return True
        except Exception as exc:
            logger.warning("Failed to load models: %s", exc)
            return False

    def _load_csv_data(self, csv_path, features, brightness_targets, cct_targets):
        """Extract training samples from a single baseline CSV."""
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    # Skip rows where lamp is off (no user preference to learn)
                    if row.get("lamp_is_off", "").lower() in ("true", "1"):
                        continue

                    # Skip rows without occupancy (no user present)
                    if row.get("filt_occupied", "").lower() not in ("true", "1"):
                        continue

                    # Extract features
                    ts_iso = row.get("ts_iso", "")
                    if not ts_iso:
                        continue
                    dt = datetime.fromisoformat(ts_iso)
                    hour_frac = dt.hour + dt.minute / 60.0

                    lux_str = row.get("lux", "")
                    if not lux_str or lux_str == "None":
                        continue
                    lux = float(lux_str)

                    # Extract targets
                    level_str = row.get("lamp_level", "")
                    if not level_str or level_str == "None":
                        continue
                    level = int(level_str)
                    brightness_pct = level_to_pct(level)

                    dtr_str = row.get("lamp_temp_dtr", "")
                    dtr1_str = row.get("lamp_temp_dtr1", "")
                    if not dtr_str or not dtr1_str or dtr_str == "None":
                        continue
                    cct_k = dtr_to_kelvin(int(dtr_str), int(dtr1_str))

                    # Cyclical hour encoding
                    hour_sin = math.sin(2 * math.pi * hour_frac / 24.0)
                    hour_cos = math.cos(2 * math.pi * hour_frac / 24.0)

                    features.append([hour_sin, hour_cos, lux])
                    brightness_targets.append(brightness_pct)
                    cct_targets.append(cct_k)

                except (ValueError, KeyError):
                    continue

    def predict(self, lux: float, hour: Optional[float] = None) -> Tuple[float, int]:
        """Predict recommended brightness (%) and CCT (Kelvin).

        Falls back to a sensible default curve if models aren't available.
        """
        if hour is None:
            now = datetime.now()
            hour = now.hour + now.minute / 60.0

        hour_sin = math.sin(2 * math.pi * hour / 24.0)
        hour_cos = math.cos(2 * math.pi * hour / 24.0)

        if self._models_loaded and self._brightness_model and self._cct_model:
            X = [[hour_sin, hour_cos, lux]]
            brightness_pct = float(self._brightness_model.predict(X)[0])
            cct_kelvin = int(round(self._cct_model.predict(X)[0]))
        else:
            # Fallback: heuristic control
            brightness_pct = self._fallback_brightness(lux)
            cct_kelvin = self._fallback_cct(hour)

        # Clamp to valid ranges
        brightness_pct = max(5.0, min(100.0, brightness_pct))
        cct_kelvin = max(2700, min(6500, cct_kelvin))

        return brightness_pct, cct_kelvin

    def _fallback_brightness(self, lux: float) -> float:
        """Inverse relationship: more daylight → less artificial light."""
        if lux >= 500:
            return 20.0
        elif lux >= 300:
            return 40.0
        elif lux >= 150:
            return 60.0
        elif lux >= 50:
            return 80.0
        else:
            return 100.0

    def _fallback_cct(self, hour: float) -> int:
        """Circadian-aligned CCT: warm morning/evening, cool midday."""
        if hour < 7 or hour > 20:
            return 2700  # warm
        elif 10 <= hour <= 14:
            return 6500  # cool/energizing
        elif hour < 10:
            # 7-10: ramp from warm to cool
            t = (hour - 7) / 3.0
            return int(2700 + t * (6500 - 2700))
        else:
            # 14-20: ramp from cool to warm
            t = (hour - 14) / 6.0
            return int(6500 - t * (6500 - 2700))

    def start(self, reader) -> None:
        """Start the adaptive control loop in a background thread.

        Args:
            reader: UsbOccupancyReader instance providing sensor snapshots.
        """
        if self._thread and self._thread.is_alive():
            return
        self._reader = reader
        self._stop.clear()
        self._last_eval_time = 0.0
        self._thread = threading.Thread(
            target=self._run_loop, name="adaptive-engine", daemon=True
        )
        self._thread.start()
        logger.info("Adaptive engine started (eval every %ds).", self.EVAL_INTERVAL)

    def stop(self) -> None:
        self._stop.set()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._reader.snapshot()
                now = time.time()
                occupied = snap.filt_occupied

                # --- Occupancy-based switching (runs every loop iteration) ---
                if occupied:
                    self._last_occupied_time = now
                    if self._turned_off_for_absence:
                        # Person returned → restore adaptive lighting
                        self._turned_off_for_absence = False
                        self._apply_adaptive(snap, reason="presence_restore")

                elif self._last_occupied_time > 0:
                    absence_duration = now - self._last_occupied_time
                    if (
                        absence_duration >= self.ABSENCE_TIMEOUT
                        and not self._turned_off_for_absence
                        and not self.lamp.state.is_off
                    ):
                        logger.info(
                            "ADAPTIVE: Absent for %.0fs → turning off", absence_duration
                        )
                        with self.lamp_lock:
                            self.lamp.off()
                        self._turned_off_for_absence = True
                        if self.on_action:
                            rationale = f"Vacant for {absence_duration:.0f}s -> turning off to save energy"
                            self.on_action("off()", "adaptive_absence_timeout", rationale)

                # --- Periodic AI evaluation (every EVAL_INTERVAL) ---
                if occupied and (now - self._last_eval_time >= self.EVAL_INTERVAL):
                    self._last_eval_time = now
                    self._apply_adaptive(snap, reason="adaptive_eval")

                self._was_occupied = bool(occupied)

            except Exception as exc:
                logger.error("Adaptive engine error: %s", exc)

            self._stop.wait(1.0)  # Check every second

    def _apply_adaptive(self, snap, reason: str = "adaptive_eval") -> None:
        """Evaluate and apply lighting adjustments if thresholds are exceeded."""
        lux = snap.lux if snap.lux is not None else 300.0

        now = datetime.now()
        hour = now.hour + now.minute / 60.0

        rec_brightness, rec_cct = self.predict(lux, hour)

        # Current state
        cur_brightness = level_to_pct(self.lamp.state.last_level)
        cur_dtr, cur_dtr1 = self.lamp.state.last_temp
        cur_cct = dtr_to_kelvin(cur_dtr, cur_dtr1)

        brightness_delta = abs(rec_brightness - cur_brightness)
        cct_delta = abs(rec_cct - cur_cct)

        actions = []
        rationale_parts = []

        if brightness_delta >= self.BRIGHTNESS_THRESHOLD or self.lamp.state.is_off:
            with self.lamp_lock:
                self.lamp.set_brightness_pct(rec_brightness)
            self._current_brightness_pct = rec_brightness
            actions.append(f"set_brightness_pct({rec_brightness:.0f})")

            lux_desc = "bright" if lux > 300 else "moderate" if lux > 100 else "dim"
            rationale_parts.append(
                f"{lux_desc} ambient light ({lux:.0f} lux) -> brightness {rec_brightness:.0f}%"
            )
            logger.info(
                "ADAPTIVE: Brightness %.0f%% → %.0f%% (Δ=%.1f%%)",
                cur_brightness, rec_brightness, brightness_delta,
            )

        if cct_delta >= self.CCT_THRESHOLD:
            dtr, dtr1 = kelvin_to_dtr(rec_cct)
            with self.lamp_lock:
                self.lamp.set_temp_raw(dtr, dtr1)
            self._current_cct_kelvin = rec_cct
            actions.append(f"set_cct({rec_cct}K)")

            hour_desc = (
                "morning" if 6 <= hour < 10
                else "midday" if 10 <= hour < 14
                else "afternoon" if 14 <= hour < 18
                else "evening"
            )
            temp_desc = "cool white" if rec_cct >= 5000 else "neutral" if rec_cct >= 3500 else "warm"
            rationale_parts.append(f"{hour_desc} ({hour:.1f}h) -> {temp_desc} {rec_cct}K")
            logger.info(
                "ADAPTIVE: CCT %dK → %dK (Δ=%dK)", cur_cct, rec_cct, cct_delta
            )

        if reason == "presence_restore":
            rationale = "Person returned after absence -> restoring adaptive lighting"
        else:
            rationale = "; ".join(rationale_parts) if rationale_parts else "No adjustment needed"

        if actions and self.on_action:
            self.on_action("; ".join(actions), reason, rationale)
