"""
AI-based adaptive lighting control engine.

Implements the thesis Section 3.3.2 control strategy:
- Learns user preferences from baseline telemetry CSV data
- Evaluates lighting adjustments every 5 minutes (configurable)
- Applies brightness/CCT thresholds to prevent micro-adjustments
- Handles occupancy-based switching with dim-then-off pattern
- Provides rich decision context (circadian, weather, behavior)
"""

import csv
import logging
import math
import threading
import time
import urllib.request
import json as _json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from .cct_utils import dtr_to_kelvin, kelvin_to_dtr, level_to_pct
from .lamp_state import LampController
from .paths import MODELS_DIR, TELEM_DIR

logger = logging.getLogger(__name__)

# Maximum behavior history entries
_MAX_BEHAVIOR_HISTORY = 500


class AdaptiveEngine:
    """AI adaptive control that learns from baseline data and adjusts lighting."""

    # Fallback class-level defaults (used if no settings object provided)
    EVAL_INTERVAL = 300
    ABSENCE_TIMEOUT = 60
    BRIGHTNESS_THRESHOLD = 5
    CCT_THRESHOLD = 100

    def __init__(
        self,
        lamp: LampController,
        lamp_lock: threading.Lock,
        settings=None,
        nominal_power_watts: float = 40.0,
        preferences=None,
    ):
        self.lamp = lamp
        self.lamp_lock = lamp_lock
        self.settings = settings
        self.nominal_power_watts = nominal_power_watts
        self.preferences = preferences  # UserPreferences instance (optional)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # ML models
        self._brightness_model = None
        self._cct_model = None
        self._models_loaded = False

        # Runtime state -- 3-state vacancy machine
        self._last_eval_time = 0.0
        self._last_occupied_time = 0.0
        self._was_occupied = False
        self._vacancy_state: str = "occupied"  # "occupied", "dimming", "off"
        self._vacancy_start: float = 0.0
        self._pre_dim_brightness: Optional[float] = None

        # Last recommended values (for threshold comparison)
        self._current_brightness_pct: Optional[float] = None
        self._current_cct_kelvin: Optional[int] = None

        # Behavior history: ring buffer of (hour, brightness, cct)
        self._behavior_history: list = []

        # Weather cache
        self._weather_cache: Optional[dict] = None
        self._weather_cache_time: float = 0.0
        self._weather_cache_ttl: float = 1800.0  # 30 minutes

        # Prediction source tracking for decision logging (split by channel)
        self._brightness_source: str = "fallback"
        self._cct_source: str = "circadian"
        self._prediction_source: str = "fallback / circadian"  # backward compat

        # Callback for telemetry logging
        self.on_action = None  # callable(action_str, reason_str, rationale_str, context)

    # ---- Settings property accessors ----

    @property
    def _eval_interval(self) -> int:
        return self.settings.eval_interval if self.settings else self.EVAL_INTERVAL

    @property
    def _absence_timeout(self) -> float:
        return self.settings.absence_timeout if self.settings else self.ABSENCE_TIMEOUT

    @property
    def _brightness_threshold(self) -> int:
        return self.settings.brightness_threshold if self.settings else self.BRIGHTNESS_THRESHOLD

    @property
    def _cct_threshold(self) -> int:
        return self.settings.cct_threshold if self.settings else self.CCT_THRESHOLD

    @property
    def _dim_level(self) -> int:
        return self.settings.dim_level if self.settings else 10

    @property
    def _dim_delay(self) -> float:
        return self.settings.dim_delay if self.settings else 60.0

    # ---- Training ----

    def train_from_baseline(self, csv_paths: Optional[list] = None) -> bool:
        """Train ML models from baseline telemetry CSV files."""
        if csv_paths is None:
            # Accept both legacy "baseline" and new "manual" CSV files
            csv_paths = sorted(
                list(TELEM_DIR.glob("run_*_baseline.csv"))
                + list(TELEM_DIR.glob("run_*_manual.csv"))
            )

        if not csv_paths:
            logger.warning("No baseline CSV files found for training.")
            return False

        try:
            from sklearn.ensemble import RandomForestRegressor
            import joblib
        except ImportError:
            logger.warning("scikit-learn not available; using fallback control.")
            return False

        features = []
        brightness_targets = []
        cct_targets = []

        for csv_path in csv_paths:
            try:
                self._load_csv_data(csv_path, features, brightness_targets, cct_targets)
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
                    if row.get("lamp_is_off", "").lower() in ("true", "1"):
                        continue
                    if row.get("filt_occupied", "").lower() not in ("true", "1"):
                        continue

                    ts_iso = row.get("ts_iso", "")
                    if not ts_iso:
                        continue
                    dt = datetime.fromisoformat(ts_iso)
                    hour_frac = dt.hour + dt.minute / 60.0

                    lux_str = row.get("lux", "")
                    if not lux_str or lux_str == "None":
                        continue
                    lux = float(lux_str)

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

                    hour_sin = math.sin(2 * math.pi * hour_frac / 24.0)
                    hour_cos = math.cos(2 * math.pi * hour_frac / 24.0)

                    features.append([hour_sin, hour_cos, lux])
                    brightness_targets.append(brightness_pct)
                    cct_targets.append(cct_k)

                except (ValueError, KeyError):
                    continue

    # ---- Prediction ----

    def predict(self, lux: float, hour: Optional[float] = None) -> Tuple[float, int]:
        """Predict recommended brightness (%) and CCT (Kelvin).

        Brightness: ML models (if trained) blended with user preferences,
        then user preferences alone, then generic fallback heuristics.

        CCT: Circadian rhythm is always the primary driver.  User
        preferences (questionnaire) act as a mild nudge on top of the
        circadian base — they sharpen the curve, not replace it.
        ML CCT predictions are intentionally ignored because the
        training data was collected without user interaction, so the
        model learned a static value rather than true preference.
        """
        if hour is None:
            now = datetime.now()
            hour = now.hour + now.minute / 60.0

        hour_sin = math.sin(2 * math.pi * hour / 24.0)
        hour_cos = math.cos(2 * math.pi * hour / 24.0)

        prefs = self.preferences

        # === BRIGHTNESS: ML-first (unchanged behaviour) ===
        if self._models_loaded and self._brightness_model:
            X = [[hour_sin, hour_cos, lux]]
            ml_brightness = float(self._brightness_model.predict(X)[0])

            if prefs and prefs.completed:
                pref_brightness = prefs.get_preferred_brightness(hour)
                brightness_pct = 0.7 * ml_brightness + 0.3 * pref_brightness
                self._brightness_source = "ML + preferences"
            else:
                brightness_pct = ml_brightness
                self._brightness_source = "ML"
        elif prefs and prefs.completed:
            brightness_pct = prefs.get_preferred_brightness(hour)
            lux_factor = self._fallback_brightness(lux) / 100.0
            brightness_pct = brightness_pct * lux_factor + brightness_pct * (1 - lux_factor) * 0.5
            brightness_pct = max(brightness_pct, 10.0)
            self._brightness_source = "preferences"
        else:
            brightness_pct = self._fallback_brightness(lux)
            self._brightness_source = "fallback"

        # === CCT: Circadian-first ===
        # The circadian curve is always the foundation for CCT.
        # User preferences nudge the base (80% circadian, 20% preference)
        # so the questionnaire sharpens the rhythm without overriding it.
        circadian_cct = self._fallback_cct(hour)

        if prefs and prefs.completed:
            pref_cct = prefs.get_preferred_cct(hour)
            cct_kelvin = int(round(0.8 * circadian_cct + 0.2 * pref_cct))
            self._cct_source = "circadian + preferences"
        else:
            cct_kelvin = circadian_cct
            self._cct_source = "circadian"

        # Combined source string for backward compatibility
        self._prediction_source = f"{self._brightness_source} / {self._cct_source}"

        brightness_pct = max(5.0, min(100.0, brightness_pct))
        cct_kelvin = max(2700, min(6500, cct_kelvin))

        return brightness_pct, cct_kelvin

    def _fallback_brightness(self, lux: float) -> float:
        """Inverse relationship: more daylight -> less artificial light."""
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
            return 2700
        elif 10 <= hour <= 14:
            return 6500
        elif hour < 10:
            t = (hour - 7) / 3.0
            return int(2700 + t * (6500 - 2700))
        else:
            t = (hour - 14) / 6.0
            return int(6500 - t * (6500 - 2700))

    # ---- Rich context helpers ----

    def _circadian_phase(self, hour: float) -> str:
        """Human-readable circadian phase name."""
        if hour < 7:
            return "pre-dawn wind-down"
        elif hour < 10:
            return "morning warm-up"
        elif hour < 14:
            return "midday peak alertness"
        elif hour < 18:
            return "afternoon transition"
        elif hour <= 20:
            return "evening wind-down"
        else:
            return "night wind-down"

    def _build_cct_reasoning(
        self, rec_cct: int, circadian_target: int, phase: str,
        hour: float, source: str,
    ) -> str:
        """Build explicit reasoning for CCT changes based on circadian rhythm."""
        cct_diff = abs(rec_cct - circadian_target)
        temp_desc = "warm" if rec_cct < 3500 else "neutral" if rec_cct < 5000 else "cool"

        # Circadian benefit explanation per phase
        phase_benefits = {
            "pre-dawn wind-down": "warm tones support melatonin production for rest",
            "morning warm-up": "transitioning to cooler tones to boost morning alertness",
            "midday peak alertness": "cool white light supports focus and productivity",
            "afternoon transition": "gradually warming as the day winds down",
            "evening wind-down": "warm tones ease the transition toward sleep",
            "night wind-down": "warm tones support melatonin production for rest",
        }
        benefit = phase_benefits.get(phase, "")

        if cct_diff <= 200:
            # Aligns with circadian
            reason = f"CCT {rec_cct}K aligns with circadian {phase} target ({circadian_target}K) - {benefit}"
        else:
            # Diverges slightly from pure circadian due to preference nudge
            if "preferences" in source:
                reason = (
                    f"CCT {rec_cct}K follows circadian rhythm, "
                    f"nudged by user preference "
                    f"(circadian target: {circadian_target}K for {phase}) - {benefit}"
                )
            else:
                reason = f"CCT {rec_cct}K ({temp_desc}) for {phase} - {benefit}"

        return reason

    def _infer_weather_lux(self, lux: float, hour: float) -> str:
        """Infer weather conditions from ambient lux as a proxy."""
        if hour < 7 or hour > 19:
            return "night"
        if lux < 100:
            return "overcast"
        elif lux < 300:
            return "cloudy"
        else:
            return "clear"

    def _fetch_weather(self) -> Optional[dict]:
        """Fetch weather from OpenWeatherMap API (cached)."""
        if not self.settings:
            return None
        api_key = self.settings.weather_api_key
        location = self.settings.weather_location
        if not api_key or not location:
            return None

        now = time.time()
        if self._weather_cache and (now - self._weather_cache_time < self._weather_cache_ttl):
            return self._weather_cache

        try:
            url = (
                f"https://api.openweathermap.org/data/2.5/weather"
                f"?q={urllib.request.quote(location)}"
                f"&appid={urllib.request.quote(api_key)}"
                f"&units=metric"
            )
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = _json.loads(resp.read().decode())
                weather = data.get("weather", [{}])[0]
                main = data.get("main", {})
                result = {
                    "condition": weather.get("main", "Unknown"),
                    "description": weather.get("description", ""),
                    "temp_c": main.get("temp"),
                    "humidity": main.get("humidity"),
                }
                self._weather_cache = result
                self._weather_cache_time = now
                return result
        except Exception as exc:
            logger.debug("Weather API fetch failed: %s", exc)
            return None

    def _get_weather_context(self, lux: float, hour: float) -> str:
        """Combined weather context from API + lux proxy."""
        api_weather = self._fetch_weather()
        lux_weather = self._infer_weather_lux(lux, hour)

        if api_weather:
            condition = api_weather["condition"]
            temp = api_weather.get("temp_c")
            parts = [f"{condition}"]
            if temp is not None:
                parts.append(f"{temp:.0f}C")
            return f"{', '.join(parts)} (lux: {lux_weather})"
        else:
            if lux_weather == "night":
                return "night"
            return f"{lux_weather} (lux proxy, {lux:.0f} lx)"

    def _behavior_summary(self, hour: float) -> str:
        """Summarize historical behavior near this hour."""
        if len(self._behavior_history) < 5:
            return ""

        nearby = [
            (b, c) for h, b, c in self._behavior_history
            if abs(h - hour) <= 1.0 or abs(h - hour - 24) <= 1.0 or abs(h - hour + 24) <= 1.0
        ]

        if len(nearby) < 3:
            return ""

        avg_b = sum(b for b, _ in nearby) / len(nearby)
        avg_c = sum(c for _, c in nearby) / len(nearby)
        return f"typical at this hour: {avg_b:.0f}% brightness, {avg_c:.0f}K"

    def _record_behavior(self, hour: float, brightness: float, cct: int) -> None:
        """Record a brightness/CCT setting for behavior tracking."""
        self._behavior_history.append((hour, brightness, cct))
        if len(self._behavior_history) > _MAX_BEHAVIOR_HISTORY:
            self._behavior_history = self._behavior_history[-_MAX_BEHAVIOR_HISTORY:]

    # ---- Engine lifecycle ----

    def start(self, reader) -> None:
        """Start the adaptive control loop in a background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._reader = reader
        self._stop.clear()
        self._last_eval_time = 0.0
        self._vacancy_state = "occupied"
        self._thread = threading.Thread(
            target=self._run_loop, name="adaptive-engine", daemon=True
        )
        self._thread.start()
        logger.info("Adaptive engine started (eval every %ds).", self._eval_interval)

    def stop(self) -> None:
        self._stop.set()

    # ---- Main control loop ----

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._reader.snapshot()
                now = time.time()
                occupied = snap.filt_occupied

                # --- 3-state vacancy machine ---
                if self._vacancy_state == "occupied":
                    if occupied:
                        self._last_occupied_time = now
                    elif occupied is not None and not occupied:
                        # Transition: occupied -> dimming
                        cur_brightness = level_to_pct(self.lamp.state.last_level)
                        self._pre_dim_brightness = cur_brightness
                        self._vacancy_start = now
                        self._vacancy_state = "dimming"
                        dim_level = self._dim_level

                        logger.info(
                            "ADAPTIVE: VACANT -> Dimming to %d%% as warning", dim_level
                        )
                        with self.lamp_lock:
                            self.lamp.set_brightness_pct(dim_level)

                        if self.on_action:
                            rationale = (
                                f"Desk vacant -> dimming to {dim_level}% "
                                f"as warning before shutdown"
                            )
                            self.on_action(
                                f"set_brightness_pct({dim_level})",
                                "adaptive_vacant_dim",
                                rationale,
                                {"circadian_phase": "", "weather": ""},
                            )

                elif self._vacancy_state == "dimming":
                    if occupied:
                        # Person returned during dim warning -> restore
                        restore_pct = self._pre_dim_brightness or 75.0
                        self._vacancy_state = "occupied"
                        self._last_occupied_time = now

                        logger.info(
                            "ADAPTIVE: Person returned during dim -> restoring to %.0f%%",
                            restore_pct,
                        )
                        with self.lamp_lock:
                            self.lamp.set_brightness_pct(restore_pct)

                        if self.on_action:
                            rationale = (
                                f"Person returned during dim warning "
                                f"-> restoring brightness to {restore_pct:.0f}%"
                            )
                            self.on_action(
                                f"set_brightness_pct({restore_pct:.0f})",
                                "adaptive_dim_restore",
                                rationale,
                                {"circadian_phase": "", "weather": ""},
                            )
                    else:
                        # Check if dim delay has elapsed
                        dim_delay = self._dim_delay
                        if now - self._vacancy_start >= dim_delay:
                            self._vacancy_state = "off"
                            logger.info(
                                "ADAPTIVE: Dim timer expired (%.0fs) -> turning OFF",
                                dim_delay,
                            )
                            with self.lamp_lock:
                                self.lamp.off()

                            if self.on_action:
                                rationale = (
                                    f"Vacant for {dim_delay:.0f}s after dimming "
                                    f"-> turning off to save energy"
                                )
                                self.on_action(
                                    "off()", "adaptive_vacant_off", rationale,
                                    {"circadian_phase": "", "weather": ""},
                                )

                elif self._vacancy_state == "off":
                    if occupied:
                        # Person returned after full shutdown -> restore adaptive
                        self._vacancy_state = "occupied"
                        self._last_occupied_time = now
                        self._apply_adaptive(snap, reason="presence_restore")

                # --- Periodic AI evaluation (every eval_interval) ---
                if (
                    self._vacancy_state == "occupied"
                    and occupied
                    and (now - self._last_eval_time >= self._eval_interval)
                ):
                    self._last_eval_time = now
                    self._apply_adaptive(snap, reason="adaptive_eval")

                self._was_occupied = bool(occupied)

            except Exception as exc:
                logger.error("Adaptive engine error: %s", exc)

            self._stop.wait(1.0)

    # ---- Apply adaptive lighting with rich context ----

    def _apply_adaptive(self, snap, reason: str = "adaptive_eval") -> None:
        """Evaluate and apply lighting adjustments with rich decision context."""
        lux = snap.lux if snap.lux is not None else 300.0

        now = datetime.now()
        hour = now.hour + now.minute / 60.0
        time_exact = now.strftime("%H:%M")

        rec_brightness, rec_cct = self.predict(lux, hour)

        # Current state
        cur_brightness = level_to_pct(self.lamp.state.last_level)
        cur_dtr, cur_dtr1 = self.lamp.state.last_temp
        cur_cct = dtr_to_kelvin(cur_dtr, cur_dtr1)

        brightness_delta = abs(rec_brightness - cur_brightness)
        cct_delta = abs(rec_cct - cur_cct)

        # Build rich context
        circadian_phase = self._circadian_phase(hour)
        circadian_cct_target = self._fallback_cct(hour)
        weather_context = self._get_weather_context(lux, hour)
        cct_source = self._cct_source
        brightness_source = self._brightness_source
        behavior_note = self._behavior_summary(hour)

        lux_desc = "bright" if lux > 300 else "moderate" if lux > 100 else "dim"
        hour_desc = (
            "morning" if 6 <= hour < 10
            else "midday" if 10 <= hour < 14
            else "afternoon" if 14 <= hour < 18
            else "evening"
        )
        temp_desc = "cool white" if rec_cct >= 5000 else "neutral" if rec_cct >= 3500 else "warm"

        # CCT reasoning: explain why this temperature was chosen
        cct_reasoning = self._build_cct_reasoning(
            rec_cct, circadian_cct_target, circadian_phase, hour, cct_source
        )

        context = {
            "time_exact": time_exact,
            "circadian_phase": circadian_phase,
            "circadian_cct_target": circadian_cct_target,
            "weather": weather_context,
            "lux": round(lux, 1),
            "lux_desc": lux_desc,
            "model_type": f"brightness: {brightness_source}, cct: {cct_source}",
            "rec_brightness": round(rec_brightness, 1),
            "rec_cct": rec_cct,
            "brightness_delta": round(brightness_delta, 1),
            "cct_delta": cct_delta,
            "cct_reasoning": cct_reasoning,
            "behavior_note": behavior_note,
        }

        actions = []

        if brightness_delta >= self._brightness_threshold or self.lamp.state.is_off:
            with self.lamp_lock:
                self.lamp.set_brightness_pct(rec_brightness)
            self._current_brightness_pct = rec_brightness
            actions.append(f"set_brightness_pct({rec_brightness:.0f})")
            logger.info(
                "ADAPTIVE: Brightness %.0f%% -> %.0f%% (delta=%.1f%%)",
                cur_brightness, rec_brightness, brightness_delta,
            )

        if cct_delta >= self._cct_threshold:
            dtr, dtr1 = kelvin_to_dtr(rec_cct)
            with self.lamp_lock:
                self.lamp.set_temp_raw(dtr, dtr1)
            self._current_cct_kelvin = rec_cct
            actions.append(f"set_cct({rec_cct}K)")
            logger.info(
                "ADAPTIVE: CCT %dK -> %dK (delta=%dK)", cur_cct, rec_cct, cct_delta
            )

        # Record behavior
        self._record_behavior(hour, rec_brightness, rec_cct)

        # Build rich rationale with explicit circadian CCT reasoning
        if reason == "presence_restore":
            rationale = (
                f"Person returned after absence -> restoring adaptive lighting. "
                f"{circadian_phase} ({time_exact}), "
                f"{weather_context}. "
                f"Brightness {rec_brightness:.0f}%, {temp_desc} {rec_cct}K. "
                f"{cct_reasoning}"
            )
        elif actions:
            rationale = (
                f"{circadian_phase.capitalize()} ({time_exact}), "
                f"{weather_context}, "
                f"{lux_desc} ambient ({lux:.0f} lux). "
                f"Brightness {rec_brightness:.0f}%, {temp_desc} {rec_cct}K. "
                f"{cct_reasoning}"
            )
            if behavior_note:
                rationale += f" [{behavior_note}]"
        else:
            rationale = (
                f"No adjustment needed at {time_exact}. "
                f"{circadian_phase}, {weather_context}. "
                f"Brightness delta {brightness_delta:.0f}% < {self._brightness_threshold}%, "
                f"CCT delta {cct_delta}K < {self._cct_threshold}K"
            )

        if actions and self.on_action:
            self.on_action("; ".join(actions), reason, rationale, context)
