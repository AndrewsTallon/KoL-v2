"""
FastAPI web server for the DALI lighting control system.

Provides:
- REST API for lamp control, mode switching, telemetry access
- WebSocket for real-time sensor/lamp status streaming
- Static file serving for the dashboard frontend
"""

import asyncio
import csv
import json
import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .cct_utils import dtr_to_kelvin, kelvin_to_dtr, level_to_pct
from .energy_estimator import estimate_energy
from .paths import STATIC_DIR, TELEM_DIR

logger = logging.getLogger(__name__)


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
    nominal_power_watts: Optional[float] = None
    weather_api_key: Optional[str] = None
    weather_location: Optional[str] = None


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
        }

    # ---- Lamp Control ----

    @app.post("/api/lamp/brightness")
    async def set_brightness(req: BrightnessRequest):
        with app_state["lamp_lock"]:
            app_state["lamp"].set_brightness_pct(req.pct)
            _save_state(app_state)
        return {"ok": True, "brightness_pct": req.pct}

    @app.post("/api/lamp/cct")
    async def set_cct(req: CCTRequest):
        dtr, dtr1 = kelvin_to_dtr(req.kelvin)
        with app_state["lamp_lock"]:
            app_state["lamp"].set_temp_raw(dtr, dtr1)
            _save_state(app_state)
        return {"ok": True, "cct_kelvin": req.kelvin}

    @app.post("/api/lamp/on")
    async def lamp_on():
        with app_state["lamp_lock"]:
            app_state["lamp"].on_last()
            _save_state(app_state)
        return {"ok": True}

    @app.post("/api/lamp/off")
    async def lamp_off():
        with app_state["lamp_lock"]:
            app_state["lamp"].off()
            _save_state(app_state)
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
                    from .preferences import UserPreferences
                    prefs = app_state.get("preferences")
                    if not prefs:
                        prefs = UserPreferences.load()
                        app_state["preferences"] = prefs
                    engine = AdaptiveEngine(
                        app_state["lamp"],
                        app_state["lamp_lock"],
                        settings=app_state.get("settings"),
                        preferences=prefs,
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
                if not engine._models_loaded:
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
        partial = {k: v for k, v in req_data.items() if v is not None}
        if not partial:
            return settings.to_dict()
        try:
            new_state = settings.update(partial)
            # Keep nominal_power_watts in sync with app_state
            app_state["nominal_power_watts"] = settings.nominal_power_watts
            return {"ok": True, "settings": new_state}
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # ---- User Preferences ----

    @app.get("/api/preferences")
    async def get_preferences():
        prefs = app_state.get("preferences")
        if not prefs:
            from .preferences import UserPreferences
            prefs = UserPreferences.load()
            app_state["preferences"] = prefs
        return prefs.to_dict()

    @app.post("/api/preferences")
    async def update_preferences(req: dict):
        prefs = app_state.get("preferences")
        if not prefs:
            from .preferences import UserPreferences
            prefs = UserPreferences.load()
            app_state["preferences"] = prefs
        new_state = prefs.update(req)
        # Push updated preferences to adaptive engine if running
        engine = app_state.get("adaptive_engine")
        if engine:
            engine.preferences = prefs
        return {"ok": True, "preferences": new_state}

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
            return JSONResponse({"error": str(exc)}, status_code=500)

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
        success = engine.train_from_baseline()
        return {"ok": success}

    # ---- WebSocket for live updates ----

    @app.websocket("/ws/live")
    async def websocket_live(ws: WebSocket):
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


def run_server(app_state: dict, host: str = "0.0.0.0", port: int = 8080):
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
