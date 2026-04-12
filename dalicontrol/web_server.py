"""
FastAPI web server for the DALI lighting control system.

Provides:
- REST API for lamp control, mode switching, telemetry access
- WebSocket for real-time sensor/lamp status streaming
- Static file serving for the dashboard frontend
- API key authentication (set KOL_API_KEY env var to enable)
- Security headers middleware
"""

import asyncio
import csv
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .cct_utils import dtr_to_kelvin, kelvin_to_dtr, level_to_pct
from .energy_estimator import estimate_energy
from .paths import STATIC_DIR, TELEM_DIR
from .usb_occupancy import list_available_ports
from .profiles import (
    create_profile,
    get_active_profile,
    get_participant_info,
    list_final_evaluations,
    list_profiles,
    preference_adapter_for,
    profile_model_dir,
    save_final_evaluation,
    save_participant_info,
    select_profile,
)
from .weather import (
    WeatherApiError,
    fetch_current_weather,
    fetch_forecast,
    geocode_location,
)

logger = logging.getLogger(__name__)

# API key for authentication.  Set KOL_API_KEY environment variable to
# enable.  When unset, all endpoints are open (development mode).
_API_KEY: Optional[str] = os.environ.get("KOL_API_KEY")


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject standard security headers into every HTTP response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:"
        )
        return response


class _ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require a valid API key for non-static, non-dashboard requests."""

    async def dispatch(self, request: Request, call_next):
        # Skip auth for static assets, dashboard, and docs
        path = request.url.path
        if (
            not _API_KEY
            or path == "/"
            or path.startswith("/static")
            or path.startswith("/api/docs")
            or path.startswith("/openapi")
        ):
            return await call_next(request)

        # Check X-API-Key header
        provided = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(provided, _API_KEY):
            return JSONResponse(
                {"error": "Invalid or missing API key"},
                status_code=401,
            )
        return await call_next(request)


# --- Pydantic request models ---

class BrightnessRequest(BaseModel):
    pct: float

class CCTRequest(BaseModel):
    kelvin: int

class ModeRequest(BaseModel):
    mode: Optional[str] = None   # "manual" or "ai"
    auto: Optional[bool] = None

class PowerRequest(BaseModel):
    nominal_power_watts: float

class SettingsRequest(BaseModel):
    dim_delay: Optional[float] = None
    dim_level: Optional[int] = None
    absence_timeout: Optional[float] = None
    eval_interval: Optional[int] = None
    brightness_threshold: Optional[int] = None
    cct_threshold: Optional[int] = None
    dali_command_gap_s: Optional[float] = None
    brightness_feedback_window_s: Optional[float] = None
    brightness_feedback_min_lux_delta: Optional[float] = None
    nominal_power_watts: Optional[float] = None
    weather_api_key: Optional[str] = None
    weather_location: Optional[str] = None
    weather_lat: Optional[float] = None
    weather_lon: Optional[float] = None
    weather_location_label: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None


class WeatherLocationRequest(BaseModel):
    query: str
    api_key: Optional[str] = None


class SensorPortRequest(BaseModel):
    # Empty string or null both mean "clear & resume auto-detection".
    port: Optional[str] = None


class CreateProfileRequest(BaseModel):
    display_name: str
    participant_info: dict


class SelectProfileRequest(BaseModel):
    profile_id: str


def _profile_fields(app_state: dict) -> dict:
    profile = app_state.get("active_profile") or {}
    return {
        "profile_id": profile.get("profile_id", ""),
        "profile_name": profile.get("display_name", ""),
    }


def _activate_profile(app_state: dict, profile: dict) -> dict:
    """Apply a selected profile to app state and the running adaptive engine."""
    profile_id = profile["profile_id"]
    prefs = preference_adapter_for(profile_id)
    model_dir = profile_model_dir(profile_id)
    model_dir.mkdir(parents=True, exist_ok=True)

    app_state["active_profile"] = profile
    app_state["preferences"] = prefs
    app_state["profile_model_dir"] = model_dir

    engine = app_state.get("adaptive_engine")
    if engine:
        engine.set_profile_context(
            preferences=prefs,
            model_dir=model_dir,
            profile_id=profile_id,
            profile_name=profile["display_name"],
        )
        engine.load_models()

    return {
        "profile": profile,
        "participant_info": get_participant_info(profile_id),
    }


def create_app(app_state: dict) -> FastAPI:
    """Create the FastAPI application with references to shared state.

    app_state must contain:
        lamp: LampController
        lamp_lock: threading.Lock
        reader: UsbOccupancyReader
        telem: TelemetryLogger
        operator: AIOperator
        adaptive_engine: AdaptiveEngine (or None)
        mode: str  ("manual" or "ai")
        auto: bool
        nominal_power_watts: float
        runtime_tracker: dict  (shared mutable for runtime tracking)
    """
    app = FastAPI(title="KoL Lighting Control", docs_url="/api/docs")

    def log_user_lamp_action(action: str, user_text: str = "") -> None:
        telem = app_state.get("telem")
        if not telem:
            return
        from .main import build_row

        snap = app_state["reader"].snapshot()
        telem.log_row(build_row(
            mode=app_state.get("mode", ""),
            snap=snap,
            lamp=app_state["lamp"],
            runtime_tracker=app_state["runtime_tracker"],
            action=action,
            reason="web_user_command",
            user_text=user_text,
            sample_type="user_command",
            nominal_power_watts=app_state.get("nominal_power_watts", 40.0),
            **_profile_fields(app_state),
        ))

    # Security middleware (order matters — headers first, then auth)
    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_middleware(_ApiKeyMiddleware)

    if _API_KEY:
        logger.info("API key authentication enabled (KOL_API_KEY is set).")
    else:
        logger.warning(
            "API key authentication DISABLED. Set KOL_API_KEY env var "
            "to secure API endpoints before production deployment."
        )

    # Serve static files (dashboard)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---- Dashboard page ----

    @app.get("/")
    async def dashboard():
        return FileResponse(str(STATIC_DIR / "index.html"))

    # ---- Status ----

    @app.get("/api/status")
    async def get_status():
        snap = app_state["reader"].snapshot()
        lamp = app_state["lamp"]
        dtr, dtr1 = lamp.state.last_temp

        return {
            "sensor": {
                "raw_present": snap.raw_present,
                "occupied": snap.filt_occupied,
                "moving": snap.moving,
                "stationary": snap.stationary,
                "lux": snap.lux,
                "moving_age_ms": snap.moving_age_ms,
                "updated_at": snap.updated_at,
                "age_s": round(time.time() - snap.updated_at, 1) if snap.updated_at else None,
            },
            "lamp": {
                "brightness_pct": level_to_pct(lamp.state.last_level),
                "brightness_level": lamp.state.last_level,
                "cct_kelvin": dtr_to_kelvin(dtr, dtr1),
                "temp_dtr": dtr,
                "temp_dtr1": dtr1,
                "is_off": lamp.state.is_off,
            },
            "mode": app_state["mode"],
            "auto": app_state["auto"],
            "nominal_power_watts": app_state["nominal_power_watts"],
            "runtime_s": app_state["runtime_tracker"].get("total_s", 0),
            "profile": app_state.get("active_profile"),
        }

    # ---- Lamp Control ----

    @app.post("/api/lamp/brightness")
    async def set_brightness(req: BrightnessRequest):
        with app_state["lamp_lock"]:
            app_state["lamp"].set_brightness_pct(req.pct)
            _save_state(app_state)
        log_user_lamp_action(f"set_brightness_pct({req.pct:g})", f"web brightness {req.pct:g}%")
        return {"ok": True, "brightness_pct": req.pct}

    @app.post("/api/lamp/cct")
    async def set_cct(req: CCTRequest):
        dtr, dtr1 = kelvin_to_dtr(req.kelvin)
        with app_state["lamp_lock"]:
            app_state["lamp"].set_temp_raw(dtr, dtr1)
            _save_state(app_state)
        log_user_lamp_action(f"set_cct({req.kelvin}K)", f"web cct {req.kelvin}K")
        return {"ok": True, "cct_kelvin": req.kelvin}

    @app.post("/api/lamp/on")
    async def lamp_on():
        with app_state["lamp_lock"]:
            app_state["lamp"].on_last()
            _save_state(app_state)
        log_user_lamp_action("on_last()", "web on")
        return {"ok": True}

    @app.post("/api/lamp/off")
    async def lamp_off():
        with app_state["lamp_lock"]:
            app_state["lamp"].off()
            _save_state(app_state)
        log_user_lamp_action("off()", "web off")
        return {"ok": True}

    # ---- Mode ----

    @app.post("/api/mode")
    async def set_mode(req: ModeRequest):
        if req.mode is not None and req.mode in ("manual", "ai"):
            old_mode = app_state["mode"]
            app_state["mode"] = req.mode
            logger.info("Mode changed: %s → %s", old_mode, req.mode)

            engine = app_state.get("adaptive_engine")

            if req.mode == "ai":
                # Lazy-create engine if it doesn't exist yet
                if engine is None:
                    from .adaptive_engine import AdaptiveEngine
                    active_profile = app_state.get("active_profile")
                    prefs = app_state.get("preferences")
                    model_dir = app_state.get("profile_model_dir")
                    engine = AdaptiveEngine(
                        app_state["lamp"],
                        app_state["lamp_lock"],
                        settings=app_state.get("settings"),
                        preferences=prefs,
                        model_dir=model_dir,
                        profile_id=active_profile["profile_id"] if active_profile else "",
                        profile_name=active_profile["display_name"] if active_profile else "",
                    )

                    # Wire up the telemetry callback
                    from .main import build_row, record_decision
                    telem = app_state.get("telem")
                    reader = app_state["reader"]
                    runtime_tracker = app_state["runtime_tracker"]
                    lamp = app_state["lamp"]

                    def on_adaptive_action(action_str, reason_str, rationale_str="", context=None):
                        snap = reader.snapshot()
                        if telem:
                            telem.log_row(build_row(
                                mode=app_state["mode"], snap=snap, lamp=lamp,
                                runtime_tracker=runtime_tracker,
                                action=action_str, reason=reason_str,
                                rationale=rationale_str,
                                circadian_phase=context.get("circadian_phase", "") if context else "",
                                weather_context=context.get("weather", "") if context else "",
                                brightness_reasoning=context.get("brightness_reasoning", "") if context else "",
                                target_lux=context.get("target_lux", "") if context else "",
                                brightness_base_pct=context.get("brightness_base_pct", "") if context else "",
                                rec_brightness_pct=context.get("rec_brightness", "") if context else "",
                                rec_cct_kelvin=context.get("rec_cct", "") if context else "",
                                brightness_delta_pct=context.get("brightness_delta", "") if context else "",
                                cct_delta_kelvin=context.get("cct_delta", "") if context else "",
                                brightness_final_pct=context.get("brightness_final_pct", "") if context else "",
                                brightness_unclamped_pct=context.get("brightness_unclamped_pct", "") if context else "",
                                ml_brightness_adjust_pct=context.get("ml_brightness_adjust_pct", "") if context else "",
                                preference_brightness_adjust_pct=context.get("preference_brightness_adjust_pct", "") if context else "",
                                weather_brightness_adjust_pct=context.get("weather_brightness_adjust_pct", "") if context else "",
                                model_type=context.get("model_type", "") if context else "",
                                cct_reasoning=context.get("cct_reasoning", "") if context else "",
                                sample_type="ai_action",
                                nominal_power_watts=app_state.get("nominal_power_watts", 40.0),
                                **_profile_fields(app_state),
                            ))
                        record_decision(
                            action=action_str, reason=reason_str,
                            rationale=rationale_str, snap=snap,
                            mode=app_state["mode"],
                            context=context,
                        )

                    engine.on_action = on_adaptive_action
                    app_state["adaptive_engine"] = engine

                reader = app_state["reader"]
                if app_state.get("active_profile") and not engine._models_loaded:
                    engine.load_models() or engine.train_from_baseline()
                engine.start(reader)
            elif engine:
                engine.stop()

        if req.auto is not None:
            app_state["auto"] = req.auto
            logger.info("Auto occupancy: %s", req.auto)

        return {"mode": app_state["mode"], "auto": app_state["auto"]}

    # ---- Config / Settings ----

    @app.post("/api/config/power")
    async def set_power(req: PowerRequest):
        settings = app_state.get("settings")
        if settings:
            settings.update({"nominal_power_watts": req.nominal_power_watts})
            app_state["nominal_power_watts"] = req.nominal_power_watts
        else:
            app_state["nominal_power_watts"] = req.nominal_power_watts
        return {"ok": True, "nominal_power_watts": req.nominal_power_watts}

    @app.get("/api/settings")
    async def get_settings():
        settings = app_state.get("settings")
        if not settings:
            return JSONResponse({"error": "Settings not available"}, status_code=500)
        return settings.to_dict()

    @app.post("/api/settings")
    async def update_settings(req: SettingsRequest):
        settings = app_state.get("settings")
        if not settings:
            return JSONResponse({"error": "Settings not available"}, status_code=500)
        req_data = req.model_dump() if hasattr(req, 'model_dump') else req.dict()
        fields_set = getattr(req, "model_fields_set", getattr(req, "__fields_set__", set()))
        partial = {k: v for k, v in req_data.items() if v is not None or k in fields_set}
        if not partial:
            return settings.to_dict()
        try:
            new_state = settings.update(partial)
            # Keep nominal_power_watts in sync with app_state
            app_state["nominal_power_watts"] = settings.nominal_power_watts
            return {"ok": True, "settings": new_state}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # ---- Sensor port selection ----

    @app.get("/api/sensor/ports")
    async def get_sensor_ports():
        """List available serial ports and report the one the reader is using."""
        reader = app_state.get("reader")
        settings = app_state.get("settings")
        return {
            "current": getattr(reader, "port", None),
            "saved": getattr(settings, "sensor_port", "") if settings else "",
            "available": list_available_ports(),
        }

    @app.post("/api/sensor/port")
    async def set_sensor_port(req: SensorPortRequest):
        """Set (or clear, via null/empty) the sensor serial port.

        Persists to settings.json and reconnects the reader without restart.
        """
        reader = app_state.get("reader")
        settings = app_state.get("settings")
        if reader is None:
            return JSONResponse({"error": "Reader not available"}, status_code=500)

        raw_value = req.port or ""
        normalized = reader.set_port(raw_value)

        if settings is not None:
            try:
                settings.update({"sensor_port": normalized or ""})
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        return {"ok": True, "current": normalized}

    @app.post("/api/weather/locations")
    async def search_weather_locations(req: WeatherLocationRequest):
        settings = app_state.get("settings")
        if not settings:
            return JSONResponse({"error": "Settings not available"}, status_code=500)

        api_key = (req.api_key or settings.weather_api_key or "").strip()
        if not api_key:
            return JSONResponse({"error": "Weather API key is required"}, status_code=400)

        try:
            return {"ok": True, "locations": geocode_location(req.query, api_key, limit=5)}
        except WeatherApiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    @app.get("/api/weather/status")
    async def get_weather_status():
        settings = app_state.get("settings")
        if not settings:
            return JSONResponse({"error": "Settings not available"}, status_code=500)

        api_key = (settings.weather_api_key or "").strip()
        if not api_key:
            return {
                "configured": False,
                "ok": False,
                "source": "",
                "location": None,
                "current": None,
                "forecast": [],
                "fetched_at": None,
                "error": "Weather API key is not configured",
            }

        if settings.weather_lat is None or settings.weather_lon is None:
            label = settings.weather_location_label or settings.weather_location or ""
            return {
                "configured": True,
                "ok": False,
                "source": "",
                "location": {"label": label} if label else None,
                "current": None,
                "forecast": [],
                "fetched_at": None,
                "error": "Choose a verified weather location",
            }

        try:
            current = fetch_current_weather(settings.weather_lat, settings.weather_lon, api_key)
            forecast_data = fetch_forecast(settings.weather_lat, settings.weather_lon, api_key, cnt=8)
        except WeatherApiError as exc:
            return {
                "configured": True,
                "ok": False,
                "source": "openweather",
                "location": {
                    "label": settings.weather_location_label or settings.weather_location,
                    "lat": settings.weather_lat,
                    "lon": settings.weather_lon,
                },
                "current": None,
                "forecast": [],
                "fetched_at": None,
                "error": str(exc),
            }

        forecast_location = forecast_data.get("location") or {}
        label = (
            settings.weather_location_label
            or ", ".join(
                part for part in (
                    forecast_location.get("name"),
                    forecast_location.get("country"),
                )
                if part
            )
            or settings.weather_location
        )
        return {
            "configured": True,
            "ok": True,
            "source": "openweather",
            "location": {
                "label": label,
                "lat": settings.weather_lat,
                "lon": settings.weather_lon,
            },
            "current": current,
            "forecast": forecast_data.get("forecast", []),
            "fetched_at": current.get("fetched_at") or forecast_data.get("fetched_at"),
            "error": "",
        }

    # ---- Participant Profiles ----

    @app.get("/api/profiles")
    async def get_profiles():
        return list_profiles()

    @app.post("/api/profiles")
    async def create_profile_endpoint(req: CreateProfileRequest):
        try:
            profile = create_profile(req.display_name, req.participant_info)
            return {"ok": True, **_activate_profile(app_state, profile)}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/profiles/select")
    async def select_profile_endpoint(req: SelectProfileRequest):
        try:
            profile = select_profile(req.profile_id)
            return {"ok": True, **_activate_profile(app_state, profile)}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @app.get("/api/profile/current")
    async def get_current_profile():
        profile = app_state.get("active_profile") or get_active_profile()
        if profile:
            if not app_state.get("active_profile"):
                _activate_profile(app_state, profile)
            return {
                "profile": profile,
                "participant_info": get_participant_info(profile["profile_id"]),
                "final_evaluations": list_final_evaluations(profile["profile_id"]),
            }
        return JSONResponse({"error": "No active profile"}, status_code=409)

    @app.get("/api/profile/participant-info")
    async def get_profile_participant_info():
        profile = app_state.get("active_profile")
        if not profile:
            return JSONResponse({"error": "No active profile"}, status_code=409)
        return get_participant_info(profile["profile_id"]) or {}

    @app.post("/api/profile/participant-info")
    async def update_profile_participant_info(req: dict):
        profile = app_state.get("active_profile")
        if not profile:
            return JSONResponse({"error": "No active profile"}, status_code=409)
        try:
            participant_info = save_participant_info(profile["profile_id"], req)
            app_state["preferences"] = preference_adapter_for(profile["profile_id"])
            engine = app_state.get("adaptive_engine")
            if engine:
                engine.preferences = app_state["preferences"]
            return {"ok": True, "participant_info": participant_info}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/profile/final-evaluations")
    async def get_profile_final_evaluations():
        profile = app_state.get("active_profile")
        if not profile:
            return JSONResponse({"error": "No active profile"}, status_code=409)
        return {"evaluations": list_final_evaluations(profile["profile_id"])}

    @app.post("/api/profile/final-evaluations")
    async def create_profile_final_evaluation(req: dict):
        profile = app_state.get("active_profile")
        if not profile:
            return JSONResponse({"error": "No active profile"}, status_code=409)
        telem = app_state.get("telem")
        telemetry_run = getattr(getattr(telem, "path", None), "name", "")
        try:
            evaluation = save_final_evaluation(
                profile["profile_id"],
                req,
                mode=app_state.get("mode", ""),
                telemetry_run=telemetry_run,
            )
            return {"ok": True, "evaluation": evaluation}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # ---- User Preferences (legacy compatibility) ----

    @app.get("/api/preferences")
    async def get_preferences():
        profile = app_state.get("active_profile")
        if not profile:
            return JSONResponse({"error": "No active profile"}, status_code=409)
        info = get_participant_info(profile["profile_id"]) or {}
        return {
            "completed": bool(info),
            "participant_info": info,
        }

    @app.post("/api/preferences")
    async def update_preferences(req: dict):
        profile = app_state.get("active_profile")
        if not profile:
            return JSONResponse({"error": "No active profile"}, status_code=409)
        try:
            participant_info = save_participant_info(profile["profile_id"], req)
            app_state["preferences"] = preference_adapter_for(profile["profile_id"])
            engine = app_state.get("adaptive_engine")
            if engine:
                engine.preferences = app_state["preferences"]
            return {"ok": True, "preferences": participant_info}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # ---- Telemetry ----

    @app.get("/api/telemetry/runs")
    async def list_runs():
        TELEM_DIR.mkdir(parents=True, exist_ok=True)
        runs = sorted(TELEM_DIR.glob("run_*.csv"), reverse=True)
        return [{"name": r.name, "size_kb": round(r.stat().st_size / 1024, 1)} for r in runs]

    @app.get("/api/telemetry/data")
    async def get_telemetry_data(run: str, last: Optional[int] = None):
        """Get telemetry data as JSON. Optional 'last' param = last N minutes."""
        safe_name = Path(run).name
        csv_path = TELEM_DIR / safe_name
        if not csv_path.exists():
            return JSONResponse({"error": "Run not found"}, status_code=404)

        rows = []
        cutoff = None
        if last:
            cutoff = time.time() - (last * 60)

        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if cutoff:
                        ts = float(row.get("ts_epoch", 0))
                        if ts < cutoff:
                            continue
                    rows.append(row)
        except Exception as exc:
            logger.error("Error reading telemetry data: %s", exc)
            return JSONResponse({"error": "Failed to read telemetry data"}, status_code=500)

        return rows

    @app.get("/api/telemetry/download/{filename}")
    async def download_telemetry(filename: str):
        safe_name = Path(filename).name
        csv_path = TELEM_DIR / safe_name
        if not csv_path.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)
        return FileResponse(
            str(csv_path),
            media_type="text/csv",
            filename=safe_name,
        )

    # ---- Energy ----

    @app.get("/api/energy")
    async def get_energy(run: str):
        safe_name = Path(run).name
        csv_path = TELEM_DIR / safe_name
        if not csv_path.exists():
            return JSONResponse({"error": "Run not found"}, status_code=404)

        report = estimate_energy(csv_path, app_state["nominal_power_watts"])
        if report is None:
            return JSONResponse({"error": "Estimation failed"}, status_code=500)

        return asdict(report)

    # ---- Decision Log ----

    @app.get("/api/decisions")
    async def get_decisions():
        decisions = app_state.get("recent_decisions", [])
        lock = app_state.get("decisions_lock")
        if lock:
            with lock:
                return list(decisions)
        return list(decisions)

    # ---- Train models ----

    @app.post("/api/ai/train")
    async def train_models():
        engine = app_state.get("adaptive_engine")
        if not engine:
            return JSONResponse({"error": "No adaptive engine"}, status_code=400)
        if not app_state.get("active_profile"):
            return JSONResponse({"error": "Select a profile before training"}, status_code=409)
        success = engine.train_from_baseline()
        return {"ok": success}

    # ---- WebSocket for live updates ----

    @app.websocket("/ws/live")
    async def websocket_live(ws: WebSocket):
        # Validate API key for WebSocket connections if auth is enabled
        if _API_KEY:
            token = ws.query_params.get("token", "")
            if not secrets.compare_digest(token, _API_KEY):
                await ws.close(code=4001, reason="Invalid or missing API key")
                return
        await ws.accept()
        logger.info("WebSocket client connected.")
        try:
            while True:
                snap = app_state["reader"].snapshot()
                lamp = app_state["lamp"]
                dtr, dtr1 = lamp.state.last_temp

                # Get latest decision if available
                last_decision = None
                decisions = app_state.get("recent_decisions", [])
                lock = app_state.get("decisions_lock")
                if decisions:
                    if lock:
                        with lock:
                            last_decision = decisions[-1] if decisions else None
                    else:
                        last_decision = decisions[-1] if decisions else None

                msg = {
                    "sensor": {
                        "occupied": snap.filt_occupied,
                        "raw_present": snap.raw_present,
                        "moving": snap.moving,
                        "stationary": snap.stationary,
                        "lux": snap.lux,
                        "moving_age_ms": snap.moving_age_ms,
                        "age_s": round(time.time() - snap.updated_at, 1) if snap.updated_at else None,
                    },
                    "lamp": {
                        "brightness_pct": level_to_pct(lamp.state.last_level),
                        "cct_kelvin": dtr_to_kelvin(dtr, dtr1),
                        "is_off": lamp.state.is_off,
                    },
                    "mode": app_state["mode"],
                    "auto": app_state["auto"],
                    "runtime_s": app_state["runtime_tracker"].get("total_s", 0),
                    "energy_est_wh": app_state["runtime_tracker"].get("energy_wh", 0),
                    "last_decision": last_decision,
                    "profile": app_state.get("active_profile"),
                    "ts": time.time(),
                }

                await ws.send_json(msg)
                await asyncio.sleep(5)

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected.")
        except Exception as exc:
            logger.warning("WebSocket error: %s", exc)

    return app


def _save_state(app_state):
    """Persist lamp state after manual control changes."""
    from .ai_operator import save_state
    save_state(app_state["lamp"].state)


def run_server(app_state: dict, host: str = "127.0.0.1", port: int = 8080):
    """Run the web server in a background thread."""
    import uvicorn

    app = create_app(app_state)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, name="web-server", daemon=True)
    thread.start()
    logger.info("Web server started on http://%s:%d", host, port)
    return thread
