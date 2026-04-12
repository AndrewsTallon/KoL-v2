import argparse
import csv
import logging
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .ai_operator import AIOperator, load_state, save_state
from .cct_utils import dtr_to_kelvin, level_to_pct
from .dali_controls import DaliControls
from .dali_transport import DaliHidTransport
from .lamp_state import LampController
from .paths import TELEM_DIR
from .profiles import get_active_profile, preference_adapter_for, profile_model_dir
from .settings import Settings
from .usb_occupancy import UsbOccupancyReader, detect_sensor_port


# ---------------- Telemetry ----------------


TELEMETRY_SCHEMA_VERSION = "2026-04-12.1"


class TelemetryLogger:
    """
    Appends rows to a CSV file for later analysis (manual vs AI, comfort vs savings).
    Thread-safe.
    """

    FIELDNAMES = [
        "ts_epoch",
        "ts_iso",
        "mode",
        "telemetry_schema_version",
        "study_phase",
        "profile_id",
        "profile_name",
        "raw_present",
        "filt_occupied",
        "moving",
        "stationary",
        "lux",
        "lux_smooth",
        "lux_ok",
        "moving_age_ms",
        "moving_events",
        "sensor_age_s",
        "move_dist",
        "move_energy",
        "still_dist",
        "still_energy",
        "sensor_seq",
        "sensor_uptime_s",
        "confirm_count",
        "filter_stage",
        "lamp_is_off",
        "lamp_level",
        "lamp_brightness_pct",
        "lamp_brightness_frac",
        "lamp_temp_dtr",
        "lamp_temp_dtr1",
        "cct_kelvin",
        "runtime_s",
        "sample_dt_s",
        "lamp_estimated_power_w",
        "energy_est_wh_cumulative",
        "lighting_during_absence",
        "sample_type",
        "control_source",
        "decision_outcome",
        "action",
        "reason",
        "rationale",
        "user_text",
        "circadian_phase",
        "weather_context",
        "brightness_reasoning",
        "raw_sensor_lux",
        "estimated_ambient_lux",
        "target_lux",
        "circadian_cct_target",
        "brightness_base_pct",
        "rec_brightness_pct",
        "rec_cct_kelvin",
        "brightness_delta_pct",
        "cct_delta_kelvin",
        "brightness_final_pct",
        "brightness_unclamped_pct",
        "ml_brightness_adjust_pct",
        "preference_brightness_adjust_pct",
        "weather_brightness_adjust_pct",
        "model_type",
        "cct_reasoning",
        "behavior_note",
    ]

    def __init__(self, mode: str):
        TELEM_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = TELEM_DIR / f"run_{stamp}_{mode}.csv"
        self.mode = mode
        self._lock = threading.Lock()
        self._fh = None
        self._writer = None
        self._last_ts_epoch = None
        self._open()

    def _open(self):
        is_new = not self.path.exists()
        self._fh = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.FIELDNAMES)
        if is_new:
            self._writer.writeheader()
            self._fh.flush()

    def log_row(self, row: dict):
        with self._lock:
            ts_epoch = row.get("ts_epoch")
            try:
                ts_epoch = float(ts_epoch)
            except (TypeError, ValueError):
                ts_epoch = None
            if row.get("sample_dt_s", "") in ("", None):
                sample_dt = 0.0 if self._last_ts_epoch is None or ts_epoch is None else max(
                    0.0, ts_epoch - self._last_ts_epoch
                )
                row["sample_dt_s"] = round(sample_dt, 3)
            if ts_epoch is not None:
                self._last_ts_epoch = ts_epoch
            self._writer.writerow(row)
            self._fh.flush()

    def close(self):
        with self._lock:
            try:
                if self._fh:
                    self._fh.flush()
                    self._fh.close()
            except Exception:
                pass


def build_row(
    *,
    mode: str,
    snap,
    lamp: LampController,
    runtime_tracker: dict,
    action: str = "",
    reason: str = "",
    rationale: str = "",
    user_text: str = "",
    circadian_phase: str = "",
    weather_context: str = "",
    brightness_reasoning: str = "",
    raw_sensor_lux: str = "",
    estimated_ambient_lux: str = "",
    target_lux: str = "",
    circadian_cct_target: str = "",
    brightness_base_pct: str = "",
    rec_brightness_pct: str = "",
    rec_cct_kelvin: str = "",
    brightness_delta_pct: str = "",
    cct_delta_kelvin: str = "",
    brightness_final_pct: str = "",
    brightness_unclamped_pct: str = "",
    ml_brightness_adjust_pct: str = "",
    preference_brightness_adjust_pct: str = "",
    weather_brightness_adjust_pct: str = "",
    model_type: str = "",
    cct_reasoning: str = "",
    behavior_note: str = "",
    sample_type: str = "heartbeat",
    control_source: str = "",
    decision_outcome: str = "",
    profile_id: str = "",
    profile_name: str = "",
    nominal_power_watts: float = 40.0,
) -> dict:
    now_epoch = time.time()
    now_iso = datetime.fromtimestamp(now_epoch).isoformat(timespec="seconds")

    sensor_age_s = (now_epoch - snap.updated_at) if getattr(snap, "updated_at", 0.0) else -1.0
    temp_dtr, temp_dtr1 = lamp.state.last_temp
    lamp_is_off = bool(lamp.state.is_off)
    lamp_level = int(lamp.state.last_level)
    brightness_pct = 0.0 if lamp_is_off else level_to_pct(lamp_level)
    brightness_frac = round(brightness_pct / 100.0, 4)
    occupied = getattr(snap, "filt_occupied", None)
    lighting_during_absence = (not lamp_is_off) and occupied not in (True, "True", "true", 1, "1")
    estimated_power_w = 0.0 if lamp_is_off else float(nominal_power_watts) * brightness_frac
    study_phase = "ai_driven" if mode == "ai" else "baseline"
    if not control_source:
        control_source = {
            "ai_action": "ai",
            "ai_evaluation": "ai",
            "user_command": "human",
            "heartbeat": "logger",
        }.get(sample_type, "")
    if not decision_outcome and sample_type == "ai_action" and action:
        decision_outcome = "applied"

    return {
        "ts_epoch": round(now_epoch, 3),
        "ts_iso": now_iso,
        "mode": mode,
        "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
        "study_phase": study_phase,
        "profile_id": profile_id,
        "profile_name": profile_name,
        "raw_present": getattr(snap, "raw_present", None),
        "filt_occupied": getattr(snap, "filt_occupied", None),
        "moving": getattr(snap, "moving", None),
        "stationary": getattr(snap, "stationary", None),
        "lux": getattr(snap, "lux", None),
        "lux_smooth": getattr(snap, "lux_smooth", None),
        "lux_ok": getattr(snap, "lux_ok", None),
        "moving_age_ms": getattr(snap, "moving_age_ms", None),
        "moving_events": getattr(snap, "moving_events", None),
        "sensor_age_s": round(sensor_age_s, 3),
        "move_dist": getattr(snap, "move_dist", None),
        "move_energy": getattr(snap, "move_energy", None),
        "still_dist": getattr(snap, "still_dist", None),
        "still_energy": getattr(snap, "still_energy", None),
        "sensor_seq": getattr(snap, "sensor_seq", None),
        "sensor_uptime_s": getattr(snap, "sensor_uptime_s", None),
        "confirm_count": getattr(snap, "confirm_count", None),
        "filter_stage": getattr(snap, "filter_stage", None),
        "lamp_is_off": lamp_is_off,
        "lamp_level": lamp_level,
        "lamp_brightness_pct": round(brightness_pct, 1),
        "lamp_brightness_frac": brightness_frac,
        "lamp_temp_dtr": temp_dtr,
        "lamp_temp_dtr1": temp_dtr1,
        "cct_kelvin": dtr_to_kelvin(temp_dtr, temp_dtr1),
        "runtime_s": round(runtime_tracker.get("total_s", 0), 1),
        "sample_dt_s": "",
        "lamp_estimated_power_w": round(estimated_power_w, 3),
        "energy_est_wh_cumulative": round(runtime_tracker.get("energy_wh", 0), 4),
        "lighting_during_absence": lighting_during_absence,
        "sample_type": sample_type,
        "control_source": control_source,
        "decision_outcome": decision_outcome,
        "action": action,
        "reason": reason,
        "rationale": rationale,
        "user_text": user_text,
        "circadian_phase": circadian_phase,
        "weather_context": weather_context,
        "brightness_reasoning": brightness_reasoning,
        "raw_sensor_lux": raw_sensor_lux,
        "estimated_ambient_lux": estimated_ambient_lux,
        "target_lux": target_lux,
        "circadian_cct_target": circadian_cct_target,
        "brightness_base_pct": brightness_base_pct,
        "rec_brightness_pct": rec_brightness_pct,
        "rec_cct_kelvin": rec_cct_kelvin,
        "brightness_delta_pct": brightness_delta_pct,
        "cct_delta_kelvin": cct_delta_kelvin,
        "brightness_final_pct": brightness_final_pct,
        "brightness_unclamped_pct": brightness_unclamped_pct,
        "ml_brightness_adjust_pct": ml_brightness_adjust_pct,
        "preference_brightness_adjust_pct": preference_brightness_adjust_pct,
        "weather_brightness_adjust_pct": weather_brightness_adjust_pct,
        "model_type": model_type,
        "cct_reasoning": cct_reasoning,
        "behavior_note": behavior_note,
    }


# ---------------- Decision Log ----------------

# In-memory ring buffer for recent decisions (shared with web UI)
_recent_decisions: list = []
_decisions_lock = threading.Lock()


def record_decision(
    action: str, reason: str, rationale: str, snap, mode: str,
    context: Optional[dict] = None,
):
    """Record a system decision for display in the web UI and telemetry."""
    entry = {
        "ts": time.time(),
        "ts_iso": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "reason": reason,
        "rationale": rationale,
        "lux": getattr(snap, "lux", None),
        "occupied": getattr(snap, "filt_occupied", None),
        "mode": mode,
    }
    if context:
        entry["circadian_phase"] = context.get("circadian_phase", "")
        entry["weather"] = context.get("weather", "")
        entry["rec_brightness"] = context.get("rec_brightness")
        entry["rec_cct"] = context.get("rec_cct")
        entry["decision_outcome"] = context.get("decision_outcome", "")
        entry["model_type"] = context.get("model_type", "")
        entry["brightness_reasoning"] = context.get("brightness_reasoning", "")
        entry["target_lux"] = context.get("target_lux")
        entry["brightness_base_pct"] = context.get("brightness_base_pct")
        entry["weather_brightness_adjust_pct"] = context.get(
            "weather_brightness_adjust_pct"
        )
    with _decisions_lock:
        _recent_decisions.append(entry)
        if len(_recent_decisions) > 100:
            del _recent_decisions[:-100]


# ---------------- Main ----------------

def parse_args():
    p = argparse.ArgumentParser(description="KoL DALI Lighting Control")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sensor-port", default=None,
                   help="Serial port for ESP32 sensor (e.g. COM3). Auto-detected if omitted.")
    p.add_argument("--sensor-baud", type=int, default=115200)
    p.add_argument("--auto", action="store_true", help="Auto on/off based on occupancy")
    p.add_argument(
        "--mode",
        choices=["manual", "baseline", "ai"],
        default="manual",
        help="'manual'/'baseline' = pure human control, 'ai' = ML-driven adaptive control.",
    )
    p.add_argument("--web", action="store_true", help="Start the web dashboard server")
    p.add_argument("--web-host", default="127.0.0.1",
                   help="Web server bind address (default: 127.0.0.1, use 0.0.0.0 for network access)")
    p.add_argument("--web-port", type=int, default=8080, help="Web server port (default: 8080)")
    p.add_argument("--no-cli", action="store_true", help="Skip CLI input loop (use with --web)")
    p.add_argument(
        "--nominal-power", type=float, default=40.0,
        help="Nominal luminaire power in watts for energy estimation (default: 40)",
    )
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = Settings.load()

    telem = TelemetryLogger(mode=args.mode)
    logging.info("Telemetry logging to: %s", telem.path)

    # ---- Lamp / DALI init ----
    state = load_state()
    tx: Optional[DaliHidTransport] = None

    try:
        if args.dry_run:
            from .ai_operator import NullControls
            controls = NullControls()
        else:
            tx = DaliHidTransport()
            tx.open()
            controls = DaliControls(tx)

        lamp = LampController(controls, state)
        operator = AIOperator(lamp, dry_run=args.dry_run, settings=settings)

        # A single lock for ALL lamp actions (sensor thread + AI thread + web)
        lamp_lock = threading.Lock()

        # ---- Sensor reader init ----
        # Resolution order for the sensor port:
        #   1. --sensor-port CLI arg (explicit override)
        #   2. settings.sensor_port (last value persisted from web UI / auto-detect)
        #   3. one startup auto-detect pass
        #   4. None -> reader thread keeps probing in the background
        sensor_port: Optional[str] = args.sensor_port or (settings.sensor_port or None)

        if not sensor_port and not args.dry_run:
            sensor_port = detect_sensor_port(baud=args.sensor_baud)
            if sensor_port:
                logging.info("Auto-detected ESP32 sensor on %s", sensor_port)
            else:
                logging.warning(
                    "No ESP32 sensor detected at startup; reader will keep scanning."
                )

        def _persist_sensor_port(port: str) -> None:
            try:
                if settings.sensor_port != port:
                    settings.update({"sensor_port": port})
                    logging.info("Saved sensor_port=%s to settings.", port)
            except Exception as exc:
                logging.warning("Could not persist sensor_port: %s", exc)

        reader = UsbOccupancyReader(
            sensor_port,
            args.sensor_baud,
            on_port_detected=_persist_sensor_port,
        )
        reader.start()

        stop = threading.Event()

        # ---- Runtime & energy tracking (shared mutable dict) ----
        runtime_tracker = {
            "total_s": 0.0,
            "energy_wh": 0.0,
            "_last_tick": time.time(),
        }

        # ---- Shared mutable mode/auto (can be changed from web UI) ----
        app_state = {
            "lamp": lamp,
            "lamp_lock": lamp_lock,
            "reader": reader,
            "telem": telem,
            "operator": operator,
            "adaptive_engine": None,
            "mode": args.mode,
            "auto": args.auto,
            "settings": settings,
            "nominal_power_watts": settings.nominal_power_watts,
            "runtime_tracker": runtime_tracker,
            "recent_decisions": _recent_decisions,
            "decisions_lock": _decisions_lock,
        }

        # ---- Load active participant profile ----
        active_profile = get_active_profile()
        preferences = preference_adapter_for(active_profile["profile_id"]) if active_profile else None
        model_dir = profile_model_dir(active_profile["profile_id"]) if active_profile else None
        if model_dir:
            model_dir.mkdir(parents=True, exist_ok=True)
        app_state["active_profile"] = active_profile
        app_state["preferences"] = preferences
        app_state["profile_model_dir"] = model_dir

        def active_profile_fields():
            profile = app_state.get("active_profile") or {}
            return {
                "profile_id": profile.get("profile_id", ""),
                "profile_name": profile.get("display_name", ""),
            }

        # ---- Adaptive engine (for AI mode) ----
        adaptive_engine = None
        if args.mode == "ai":
            from .adaptive_engine import AdaptiveEngine
            adaptive_engine = AdaptiveEngine(
                lamp, lamp_lock,
                settings=settings,
                preferences=preferences,
                model_dir=model_dir,
                profile_id=active_profile["profile_id"] if active_profile else "",
                profile_name=active_profile["display_name"] if active_profile else "",
            )
            # Try to load existing models, otherwise train
            if active_profile and not adaptive_engine.load_models():
                adaptive_engine.train_from_baseline()

            def on_adaptive_action(action_str, reason_str, rationale_str="", context=None):
                snap = reader.snapshot()
                telem.log_row(build_row(
                    mode=app_state["mode"], snap=snap, lamp=lamp,
                    runtime_tracker=runtime_tracker,
                    action=action_str, reason=reason_str,
                    rationale=rationale_str,
                    circadian_phase=context.get("circadian_phase", "") if context else "",
                    weather_context=context.get("weather", "") if context else "",
                    brightness_reasoning=context.get("brightness_reasoning", "") if context else "",
                    raw_sensor_lux=context.get("raw_sensor_lux", "") if context else "",
                    estimated_ambient_lux=context.get("estimated_ambient_lux", "") if context else "",
                    target_lux=context.get("target_lux", "") if context else "",
                    circadian_cct_target=context.get("circadian_cct_target", "") if context else "",
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
                    behavior_note=context.get("behavior_note", "") if context else "",
                    sample_type=context.get("sample_type", "ai_action") if context else "ai_action",
                    decision_outcome=context.get("decision_outcome", "") if context else "",
                    nominal_power_watts=settings.nominal_power_watts,
                    **active_profile_fields(),
                ))
                record_decision(
                    action=action_str, reason=reason_str,
                    rationale=rationale_str, snap=snap,
                    mode=app_state["mode"],
                    context=context,
                )

            adaptive_engine.on_action = on_adaptive_action
            adaptive_engine.start(reader)
            app_state["adaptive_engine"] = adaptive_engine

        # ---- Sensor loop (telemetry only — no automation in manual mode) ----

        def sensor_loop():
            last_log_at = 0.0
            last_telem_at = 0.0

            while not stop.is_set():
                snap = reader.snapshot()
                filt = snap.filt_occupied

                now = time.time()

                # --- Runtime & energy tracking ---
                dt = now - runtime_tracker["_last_tick"]
                runtime_tracker["_last_tick"] = now
                if not lamp.state.is_off:
                    runtime_tracker["total_s"] += dt
                    dimming_frac = lamp.state.last_level / 254.0
                    runtime_tracker["energy_wh"] += (
                        settings.nominal_power_watts * dimming_frac * dt / 3600.0
                    )

                # --- Telemetry heartbeat: 5-second intervals (thesis spec) ---
                if now - last_telem_at >= 5.0:
                    telem.log_row(build_row(
                        mode=app_state["mode"], snap=snap, lamp=lamp,
                        runtime_tracker=runtime_tracker,
                        sample_type="heartbeat",
                        nominal_power_watts=settings.nominal_power_watts,
                        **active_profile_fields(),
                    ))
                    last_telem_at = now

                # Manual mode: NO automation at all — pure human control.
                # AI mode: occupancy is handled by the adaptive engine.

                # Periodic sensor health log
                if now - last_log_at > 5:
                    last_log_at = now
                    age = (now - snap.updated_at) if snap.updated_at else -1
                    logging.info(
                        "SENSOR: raw=%s filt=%s age=%.1fs line=%s",
                        snap.raw_present,
                        snap.filt_occupied,
                        age,
                        snap.last_line,
                    )

                time.sleep(0.1)

        def input_loop():
            while not stop.is_set():
                try:
                    user_text = input("you> ").strip()
                except (EOFError, KeyboardInterrupt):
                    stop.set()
                    break

                if not user_text:
                    continue

                snap = reader.snapshot()
                sensor_status = {
                    "raw_present": snap.raw_present,
                    "filt_occupied": snap.filt_occupied,
                    "moving": getattr(snap, "moving", None),
                    "stationary": getattr(snap, "stationary", None),
                    "lux": getattr(snap, "lux", None),
                    "moving_age_ms": getattr(snap, "moving_age_ms", None),
                    "moving_events": getattr(snap, "moving_events", None),
                    "last_line": snap.last_line,
                    "age_s": (time.time() - snap.updated_at) if snap.updated_at else None,
                }

                with lamp_lock:
                    operator.handle_user_text(user_text, sensor_status=sensor_status)

                    # Log AFTER the command so lamp state reflects the result
                    telem.log_row(
                        build_row(
                            mode=app_state["mode"],
                            snap=snap,
                            lamp=lamp,
                            runtime_tracker=runtime_tracker,
                            action="user_command",
                            reason="user_text",
                            user_text=user_text,
                            sample_type="user_command",
                            nominal_power_watts=settings.nominal_power_watts,
                            **active_profile_fields(),
                        )
                    )

        # ---- Start threads ----
        t1 = threading.Thread(target=sensor_loop, name="sensor-loop", daemon=True)
        t1.start()

        # Start web server if requested
        if args.web:
            from .web_server import run_server
            run_server(app_state, host=args.web_host, port=args.web_port)
            logging.info("Web dashboard: http://localhost:%d", args.web_port)

        # Start CLI input loop unless --no-cli
        if not args.no_cli:
            t2 = threading.Thread(target=input_loop, name="input-loop", daemon=True)
            t2.start()

        logging.info(
            "Running. Auto=%s. Mode=%s. Web=%s. Ctrl-C to exit.",
            app_state["auto"], app_state["mode"], args.web,
        )

        while not stop.is_set():
            time.sleep(0.5)

    finally:
        if adaptive_engine:
            try:
                adaptive_engine.stop()
            except Exception:
                pass
        try:
            telem.close()
        except Exception:
            pass
        try:
            if tx:
                tx.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
