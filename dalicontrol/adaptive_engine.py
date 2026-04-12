"""
AI-based adaptive lighting control engine.

Implements the thesis Section 3.3.2 control strategy:
- Uses circadian rhythm and ambient lux as the primary brightness driver
  (target desk illuminance minus ambient contribution)
- ML models learn time-of-day usage *patterns* (not user preferences)
  and act as a secondary refinement layer (20% nudge)
- User preferences from the questionnaire provide a mild nudge (20%)
- CCT follows circadian rhythm with optional preference nudge
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
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from .cct_utils import dtr_to_kelvin, kelvin_to_dtr, level_to_pct
from .lamp_state import LampController
from .paths import MODELS_DIR, TELEM_DIR
from .weather import WeatherApiError, fetch_current_weather

logger = logging.getLogger(__name__)

# Maximum behavior history entries
_MAX_BEHAVIOR_HISTORY = 500


class AdaptiveEngine:
    """AI adaptive control using circadian rhythm + lux as the primary driver.

    Brightness is computed from a circadian-aware target desk illuminance
    (e.g. 500 lux midday, 200 lux evening) minus ambient contribution,
    then optionally nudged by ML patterns and user preferences.
    ML learns time-of-day *patterns*, not user preferences.
    """

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
        model_dir: Optional[Path] = None,
        profile_id: str = "",
        profile_name: str = "",
    ):
        self.lamp = lamp
        self.lamp_lock = lamp_lock
        self.settings = settings
        self.nominal_power_watts = nominal_power_watts
        self.preferences = preferences  # UserPreferences instance (optional)
        self._model_dir = Path(model_dir) if model_dir else MODELS_DIR
        self.profile_id = profile_id
        self.profile_name = profile_name

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
        self._last_brightness_context: dict = {}

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
    def _max_brightness_step(self) -> float:
        return self.settings.max_brightness_step_pct if self.settings else 15.0

    @property
    def _lamp_lux_at_100pct(self) -> float:
        return self.settings.lamp_lux_at_100pct if self.settings else 400.0

    @property
    def _dim_level(self) -> int:
        return self.settings.dim_level if self.settings else 10

    @property
    def _dim_delay(self) -> float:
        return self.settings.dim_delay if self.settings else 60.0

    @property
    def _dali_command_gap_s(self) -> float:
        return self.settings.dali_command_gap_s if self.settings else 0.75

    @property
    def _brightness_feedback_window_s(self) -> float:
        return self.settings.brightness_feedback_window_s if self.settings else 2.5

    @property
    def _brightness_feedback_min_lux_delta(self) -> float:
        return self.settings.brightness_feedback_min_lux_delta if self.settings else 3.0

    def set_profile_context(
        self,
        *,
        preferences=None,
        model_dir: Optional[Path] = None,
        profile_id: str = "",
        profile_name: str = "",
    ) -> None:
        """Switch the adaptive engine to a different participant profile."""
        self.preferences = preferences
        self._model_dir = Path(model_dir) if model_dir else MODELS_DIR
        self.profile_id = profile_id
        self.profile_name = profile_name
        self._brightness_model = None
        self._cct_model = None
        self._models_loaded = False

    # ---- Training ----

    def train_from_baseline(self, csv_paths: Optional[list] = None) -> bool:
        """Train ML models from baseline telemetry CSV files.

        The models learn time-of-day usage *patterns* (correlations
        between hour, ambient lux, and lamp settings) — NOT user
        preferences.  Brightness predictions from these models are
        used as a secondary refinement (20% nudge) on top of the
        circadian + lux base, not as the primary driver.
        """
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

        self._model_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(self._brightness_model, self._model_dir / "brightness_model.joblib")
        joblib.dump(self._cct_model, self._model_dir / "cct_model.joblib")

        self._models_loaded = True
        logger.info("Adaptive models trained and saved.")
        return True

    def load_models(self) -> bool:
        """Load previously trained models from disk.

        Validates that loaded objects are RandomForestRegressor instances
        to mitigate insecure deserialization risks with joblib/pickle.
        """
        try:
            import joblib
            from sklearn.ensemble import RandomForestRegressor
        except ImportError:
            return False

        brightness_path = self._model_dir / "brightness_model.joblib"
        cct_path = self._model_dir / "cct_model.joblib"

        if not brightness_path.exists() or not cct_path.exists():
            return False

        try:
            brightness_model = joblib.load(brightness_path)
            cct_model = joblib.load(cct_path)

            # Validate deserialized types to prevent arbitrary code execution
            if not isinstance(brightness_model, RandomForestRegressor):
                logger.warning("Brightness model is not a RandomForestRegressor, rejecting.")
                return False
            if not isinstance(cct_model, RandomForestRegressor):
                logger.warning("CCT model is not a RandomForestRegressor, rejecting.")
                return False

            self._brightness_model = brightness_model
            self._cct_model = cct_model
            self._models_loaded = True
            logger.info("Loaded pre-trained adaptive models.")
            return True
        except Exception as exc:
            logger.warning("Failed to load models: %s", exc)
            return False

    def _load_csv_data(self, csv_path, features, brightness_targets, cct_targets):
        """Extract pattern samples from a single baseline CSV.

        Each sample captures the correlation between time-of-day,
        ambient lux, and the lamp setting at that moment.  These are
        *usage patterns*, not explicit user preferences.
        """
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    if self.profile_id:
                        row_profile = (row.get("profile_id") or "").strip()
                        if row_profile != self.profile_id:
                            continue

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

        Brightness: Circadian rhythm + ambient lux is always the primary
        driver.  A comfortable target desk illuminance varies by time of
        day (higher during work hours, lower at night).  The system
        calculates how much artificial light is needed to reach the
        target given the measured ambient lux.

        ML patterns and user preferences each contribute a 20% nudge on
        top of the circadian+lux base — they refine the curve without
        overriding it.

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

        # === BRIGHTNESS: Circadian + lux first ===
        # The circadian brightness curve is always the foundation.
        # It computes a comfortable brightness given time-of-day and
        # ambient lux (more daylight -> less artificial light needed).
        target_lux = self._target_desk_lux(hour)
        base_brightness = self._circadian_brightness(lux, hour)
        self._brightness_source = "circadian + lux"

        # ML pattern nudge (20%): ML learns time-of-day usage patterns
        # from baseline telemetry, NOT user preferences.
        ml_nudge = 0.0
        if self._models_loaded and self._brightness_model:
            X = [[hour_sin, hour_cos, lux]]
            ml_brightness = float(self._brightness_model.predict(X)[0])
            ml_nudge = 0.2 * (ml_brightness - base_brightness)
            self._brightness_source = "circadian + lux + patterns"

        # User preference nudge (20%): questionnaire values refine
        # the circadian base without overriding it.
        pref_nudge = 0.0
        if prefs and getattr(prefs, "completed", False):
            pref_brightness = prefs.get_preferred_brightness(hour)
            pref_nudge = 0.2 * (pref_brightness - base_brightness)
            self._brightness_source += " + preferences"

        api_weather = self._fetch_weather()
        weather_nudge, weather_condition = self._weather_brightness_adjustment(
            api_weather, lux, hour
        )
        if weather_nudge:
            self._brightness_source += " + weather"

        brightness_pct_unclamped = base_brightness + ml_nudge + pref_nudge + weather_nudge
        brightness_pct = max(5.0, min(100.0, brightness_pct_unclamped))
        self._last_brightness_context = {
            "target_lux": round(target_lux, 1),
            "ambient_lux": round(float(lux), 1),
            "brightness_base_pct": round(base_brightness, 1),
            "ml_brightness_adjust_pct": round(ml_nudge, 1),
            "preference_brightness_adjust_pct": round(pref_nudge, 1),
            "weather_brightness_adjust_pct": round(weather_nudge, 1),
            "weather_condition": weather_condition,
            "brightness_unclamped_pct": round(brightness_pct_unclamped, 1),
            "brightness_final_pct": round(brightness_pct, 1),
        }

        # === CCT: Circadian-first ===
        # The circadian curve is always the foundation for CCT.
        # User preferences nudge the base (80% circadian, 20% preference)
        # so the questionnaire sharpens the rhythm without overriding it.
        circadian_cct = self._fallback_cct(hour)

        if (
            prefs
            and getattr(prefs, "completed", False)
            and getattr(prefs, "supports_cct_preference", True)
        ):
            pref_cct = prefs.get_preferred_cct(hour)
            cct_kelvin = int(round(0.8 * circadian_cct + 0.2 * pref_cct))
            self._cct_source = "circadian + preferences"
        else:
            cct_kelvin = circadian_cct
            self._cct_source = "circadian"

        # Combined source string for backward compatibility
        self._prediction_source = f"{self._brightness_source} / {self._cct_source}"

        cct_kelvin = max(2700, min(6500, cct_kelvin))

        return brightness_pct, cct_kelvin

    # ---- Circadian brightness (primary driver) ----

    # Target desk illuminance by time of day (lux).  The system
    # calculates what percentage of artificial light is needed to
    # bridge the gap between ambient lux and this target.
    _TARGET_LUX_SCHEDULE = {
        # (start_hour, end_hour): target_desk_lux
        (0, 7): 150,       # Night / pre-dawn: low, melatonin-friendly
        (7, 9): 350,       # Morning warm-up: moderate
        (9, 12): 500,      # Late morning focus: full task lighting
        (12, 14): 500,     # Midday: full task lighting
        (14, 17): 450,     # Afternoon: still productive
        (17, 19): 300,     # Early evening: winding down
        (19, 21): 200,     # Evening: relaxed
        (21, 24): 150,     # Night: low
    }

    def _target_desk_lux(self, hour: float) -> float:
        """Comfortable target desk illuminance for the given hour.

        Uses smooth linear interpolation between schedule breakpoints
        to avoid abrupt jumps.
        """
        # Build sorted breakpoints from schedule midpoints
        breakpoints = []  # (hour, lux)
        for (start, end), target in sorted(self._TARGET_LUX_SCHEDULE.items()):
            mid = (start + end) / 2.0
            breakpoints.append((mid, target))

        # Wrap-around: duplicate first point at +24 and last at -24
        breakpoints_ext = (
            [(h - 24, lx) for h, lx in breakpoints]
            + breakpoints
            + [(h + 24, lx) for h, lx in breakpoints]
        )

        # Find surrounding breakpoints and interpolate
        for i in range(len(breakpoints_ext) - 1):
            h0, l0 = breakpoints_ext[i]
            h1, l1 = breakpoints_ext[i + 1]
            if h0 <= hour < h1:
                t = (hour - h0) / (h1 - h0)
                return l0 + t * (l1 - l0)

        # Fallback (shouldn't happen)
        return 350.0

    def _circadian_brightness(self, lux: float, hour: float) -> float:
        """Compute brightness % from circadian target lux and ambient lux.

        The idea: the lamp should supply enough light so that
        ambient + artificial ≈ target desk illuminance.
        If ambient already exceeds the target, dim to a minimum.
        """
        target = self._target_desk_lux(hour)
        deficit = target - lux

        if deficit <= 0:
            # Ambient light already exceeds target — minimal artificial
            return max(10.0, 20.0 * (target / max(lux, 1.0)))

        # Map deficit to brightness %.  At full deficit (lux=0)
        # the lamp runs at 100%.  The relationship is linear
        # with respect to the target.
        brightness = (deficit / target) * 100.0
        return max(10.0, min(100.0, brightness))

    def _fallback_brightness(self, lux: float) -> float:
        """Simple inverse relationship (legacy, used as sanity reference)."""
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

    @staticmethod
    def _adjustment_phrase(label: str, value: float) -> str:
        if value > 0:
            return f"{label} added {value:.0f}%"
        if value < 0:
            return f"{label} reduced {abs(value):.0f}%"
        return f"{label} added 0%"

    def _build_brightness_reasoning(
        self, phase: str, rec_brightness: Optional[float] = None
    ) -> str:
        """Explain the brightness recommendation from its calculation parts."""
        ctx = self._last_brightness_context or {}
        final_pct = ctx.get("brightness_final_pct", rec_brightness)
        target_lux = ctx.get("target_lux")
        ambient_lux = ctx.get("ambient_lux")
        base_pct = ctx.get("brightness_base_pct")
        if target_lux is None or ambient_lux is None or base_pct is None:
            return (
                f"Brightness {float(final_pct or 0.0):.0f}% selected "
                f"from {self._brightness_source}."
            )
        ml_nudge = float(ctx.get("ml_brightness_adjust_pct") or 0.0)
        pref_nudge = float(ctx.get("preference_brightness_adjust_pct") or 0.0)
        weather_nudge = float(ctx.get("weather_brightness_adjust_pct") or 0.0)
        weather_condition = str(ctx.get("weather_condition") or "").strip()

        parts = [
            f"{phase} target is {target_lux:.0f} lux",
            f"ambient is {ambient_lux:.0f} lux",
            f"circadian base is {base_pct:.0f}%",
        ]
        if ml_nudge:
            parts.append(self._adjustment_phrase("patterns", ml_nudge))
        if pref_nudge:
            parts.append(self._adjustment_phrase("preferences", pref_nudge))
        weather_label = (
            f"{weather_condition.lower()} weather"
            if weather_condition and weather_condition != "night"
            else "weather"
        )
        parts.append(self._adjustment_phrase(weather_label, weather_nudge))

        return f"Brightness {final_pct:.0f}% because " + ", ".join(parts) + "."

    def _weather_brightness_adjustment(
        self, api_weather: Optional[dict], lux: float, hour: float
    ) -> Tuple[float, str]:
        """Return a conservative brightness nudge from live weather conditions."""
        condition = str((api_weather or {}).get("condition") or "").strip()
        if hour < 7 or hour > 19:
            return 0.0, "night"
        if not api_weather:
            return 0.0, ""
        if lux >= 500:
            return 0.0, condition

        normalized = condition.lower()
        severe = {
            "thunderstorm",
            "snow",
            "mist",
            "fog",
            "haze",
            "smoke",
            "dust",
            "sand",
            "ash",
            "squall",
            "tornado",
        }
        if normalized in severe:
            return (8.0 if lux < 300 else 4.0), condition
        if normalized in {"rain", "drizzle"}:
            return (6.0 if lux < 300 else 3.0), condition
        if normalized == "clouds":
            return (4.0 if lux < 300 else 2.0), condition
        return 0.0, condition

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
        lat = getattr(self.settings, "weather_lat", None)
        lon = getattr(self.settings, "weather_lon", None)
        if not api_key or lat is None or lon is None:
            return None

        now = time.time()
        if self._weather_cache and (now - self._weather_cache_time < self._weather_cache_ttl):
            return self._weather_cache

        try:
            result = fetch_current_weather(lat, lon, api_key)
            self._weather_cache = result
            self._weather_cache_time = now
            return result
        except WeatherApiError as exc:
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

    # ---- Ambient lux estimation ----

    def _estimate_ambient_lux(self, sensor_lux: float) -> float:
        """Subtract estimated lamp contribution from sensor reading.

        The desk sensor reads total_lux = ambient + lamp_contribution.
        Lamp contribution is approximately proportional to brightness %.
        Without this correction, the adaptive engine treats its own light
        output as ambient, causing brightness oscillation.
        """
        if self.lamp.state.is_off:
            return max(0.0, sensor_lux)
        current_pct = self._current_brightness_pct
        if current_pct is None:
            current_pct = level_to_pct(self.lamp.state.last_level)
        lamp_contribution = (current_pct / 100.0) * self._lamp_lux_at_100pct
        return max(0.0, sensor_lux - lamp_contribution)

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

    def _read_feedback_lux_smooth(self) -> Tuple[Optional[float], str]:
        """Return a fresh lux_smooth sample for coarse brightness feedback."""
        reader = getattr(self, "_reader", None)
        if reader is None:
            return None, "sensor reader unavailable"

        snap = reader.snapshot()
        lux_smooth = getattr(snap, "lux_smooth", None)
        if lux_smooth is None:
            return None, "lux_smooth unavailable"

        updated_at = getattr(snap, "updated_at", 0.0) or 0.0
        if updated_at <= 0:
            return None, "sensor timestamp unavailable"

        age_s = time.time() - updated_at
        max_age_s = max(2.0, self._brightness_feedback_window_s + 1.0)
        if age_s > max_age_s:
            return None, f"sensor stale ({age_s:.1f}s old)"

        try:
            return float(lux_smooth), ""
        except (TypeError, ValueError):
            return None, "lux_smooth invalid"

    def _observe_brightness_feedback(
        self,
        before_sample: Tuple[Optional[float], str],
        start_brightness: float,
        target_brightness: float,
        intended_level: int,
        intended_is_off: bool,
    ) -> Tuple[str, str]:
        """Observe lux_smooth after a brightness command and retry once if needed."""
        before_lux, before_reason = before_sample
        if before_lux is None:
            return f"unverified ({before_reason})", "unverified"

        window_s = self._brightness_feedback_window_s
        if window_s > 0:
            time.sleep(window_s)

        after_lux, after_reason = self._read_feedback_lux_smooth()
        if after_lux is None:
            return f"unverified ({after_reason})", "unverified"

        min_delta = self._brightness_feedback_min_lux_delta
        lux_delta = after_lux - before_lux
        expected_up = target_brightness >= start_brightness
        observed_delta = lux_delta if expected_up else -lux_delta

        if observed_delta >= min_delta:
            return (
                f"confirmed lux_smooth {before_lux:.1f} -> {after_lux:.1f}",
                "confirmed",
            )

        gap_s = self._dali_command_gap_s
        if gap_s > 0:
            time.sleep(gap_s)

        with self.lamp_lock:
            if (
                self.lamp.state.last_level != intended_level
                or self.lamp.state.is_off != intended_is_off
            ):
                return (
                    "unverified (brightness changed before retry)",
                    "superseded",
                )
            self.lamp.set_brightness_pct(target_brightness)

        direction = "increase" if expected_up else "decrease"
        return (
            f"no {direction} seen ({before_lux:.1f} -> {after_lux:.1f}); retried once",
            "retried",
        )

    def _apply_ordered_adjustments(
        self,
        *,
        needs_brightness: bool,
        needs_cct: bool,
        rec_brightness: float,
        rec_cct: int,
        cur_brightness: float,
        was_off: bool,
    ) -> Tuple[list, str, str]:
        """Send CCT before brightness and verify brightness via coarse sensor feedback."""
        actions = []
        feedback_note = ""
        feedback_status = ""

        before_sample: Tuple[Optional[float], str] = (None, "not sampled")
        intended_level: Optional[int] = None
        intended_is_off: Optional[bool] = None

        with self.lamp_lock:
            if needs_cct:
                dtr, dtr1 = kelvin_to_dtr(rec_cct)
                self.lamp.set_temp_raw(dtr, dtr1)
                self._current_cct_kelvin = rec_cct
                actions.append(f"set_cct({rec_cct}K)")

            if needs_cct and needs_brightness:
                gap_s = self._dali_command_gap_s
                if gap_s > 0:
                    time.sleep(gap_s)

            if needs_brightness:
                before_sample = self._read_feedback_lux_smooth()
                self.lamp.set_brightness_pct(rec_brightness)
                self._current_brightness_pct = rec_brightness
                intended_level = self.lamp.state.last_level
                intended_is_off = self.lamp.state.is_off
                actions.append(f"set_brightness_pct({rec_brightness:.0f})")

        if needs_brightness and intended_level is not None and intended_is_off is not None:
            start_brightness = 0.0 if was_off else cur_brightness
            feedback_note, feedback_status = self._observe_brightness_feedback(
                before_sample,
                start_brightness,
                rec_brightness,
                intended_level,
                intended_is_off,
            )

        return actions, feedback_note, feedback_status

    def _apply_adaptive(self, snap, reason: str = "adaptive_eval") -> None:
        """Evaluate and apply lighting adjustments with rich decision context."""
        raw_lux = snap.lux if snap.lux is not None else 300.0
        lux = self._estimate_ambient_lux(raw_lux)

        now = datetime.now()
        hour = now.hour + now.minute / 60.0
        time_exact = now.strftime("%H:%M")

        rec_brightness, rec_cct = self.predict(lux, hour)

        # Current state
        cur_brightness = level_to_pct(self.lamp.state.last_level)
        cur_dtr, cur_dtr1 = self.lamp.state.last_temp
        cur_cct = dtr_to_kelvin(cur_dtr, cur_dtr1)

        # Max-step clamping: prevent large sudden jumps
        was_off = self.lamp.state.is_off
        if not was_off and self._current_brightness_pct is not None:
            max_step = self._max_brightness_step
            delta = rec_brightness - cur_brightness
            if abs(delta) > max_step:
                rec_brightness = cur_brightness + max_step * (1.0 if delta > 0 else -1.0)
                rec_brightness = max(5.0, min(100.0, rec_brightness))

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
        brightness_reasoning = self._build_brightness_reasoning(
            circadian_phase, rec_brightness
        )
        brightness_context = dict(self._last_brightness_context or {})

        context = {
            "time_exact": time_exact,
            "circadian_phase": circadian_phase,
            "circadian_cct_target": circadian_cct_target,
            "weather": weather_context,
            "lux": round(lux, 1),
            "raw_sensor_lux": round(raw_lux, 1),
            "estimated_ambient_lux": round(lux, 1),
            "lux_desc": lux_desc,
            "model_type": f"brightness: {brightness_source}, cct: {cct_source}",
            "rec_brightness": round(rec_brightness, 1),
            "rec_cct": rec_cct,
            "brightness_delta": round(brightness_delta, 1),
            "cct_delta": cct_delta,
            "brightness_reasoning": brightness_reasoning,
            "cct_reasoning": cct_reasoning,
            "behavior_note": behavior_note,
            **brightness_context,
        }

        needs_brightness = brightness_delta >= self._brightness_threshold or was_off
        needs_cct = cct_delta >= self._cct_threshold

        actions, feedback_note, feedback_status = self._apply_ordered_adjustments(
            needs_brightness=needs_brightness,
            needs_cct=needs_cct,
            rec_brightness=rec_brightness,
            rec_cct=rec_cct,
            cur_brightness=cur_brightness,
            was_off=was_off,
        )

        if needs_brightness:
            logger.info(
                "ADAPTIVE: Brightness %.0f%% -> %.0f%% (delta=%.1f%%)",
                cur_brightness, rec_brightness, brightness_delta,
            )

        if needs_cct:
            logger.info(
                "ADAPTIVE: CCT %dK -> %dK (delta=%dK)", cur_cct, rec_cct, cct_delta
            )

        if feedback_status:
            actions.append(f"brightness_feedback({feedback_status})")

        # Record behavior
        self._record_behavior(hour, rec_brightness, rec_cct)

        # Build rich rationale with explicit circadian CCT reasoning
        if reason == "presence_restore":
            rationale = (
                f"Person returned after absence -> restoring adaptive lighting. "
                f"{circadian_phase} ({time_exact}), "
                f"{weather_context}. "
                f"{brightness_reasoning} {temp_desc.capitalize()} {rec_cct}K. "
                f"{cct_reasoning}"
            )
            if feedback_note:
                rationale += f" Brightness feedback: {feedback_note}."
        elif actions:
            rationale = (
                f"{circadian_phase.capitalize()} ({time_exact}), "
                f"{weather_context}, "
                f"{lux_desc} ambient ({lux:.0f} lux). "
                f"{brightness_reasoning} {temp_desc.capitalize()} {rec_cct}K. "
                f"{cct_reasoning}"
            )
            if behavior_note:
                rationale += f" [{behavior_note}]"
            if feedback_note:
                rationale += f" Brightness feedback: {feedback_note}."
        else:
            rationale = (
                f"No adjustment needed at {time_exact}. "
                f"{circadian_phase}, {weather_context}. "
                f"{brightness_reasoning} "
                f"Brightness delta {brightness_delta:.0f}% < {self._brightness_threshold}%, "
                f"CCT delta {cct_delta}K < {self._cct_threshold}K"
            )

        if actions and self.on_action:
            context["sample_type"] = "ai_action"
            context["decision_outcome"] = "applied"
            self.on_action("; ".join(actions), reason, rationale, context)
        elif self.on_action:
            context["sample_type"] = "ai_evaluation"
            context["decision_outcome"] = "no_change"
            self.on_action("no_change", reason, rationale, context)
