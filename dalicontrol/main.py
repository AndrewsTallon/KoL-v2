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
from .cct_utils import dtr_to_kelvin
from .dali_controls import DaliControls
from .dali_transport import DaliHidTransport
from .lamp_state import LampController
from .usb_occupancy import UsbOccupancyReader


# ---------------- Telemetry ----------------

TELEM_DIR = Path(__file__).with_name("telemetry")


class TelemetryLogger:
    """
    Appends rows to a CSV file for later analysis (baseline vs AI, comfort vs savings).
    Thread-safe.
    """

    FIELDNAMES = [
        "ts_epoch",
        "ts_iso",
        "mode",
        "raw_present",
        "filt_occupied",
        "moving",
        "stationary",
        "lux",
        "moving_age_ms",
        "moving_events",
        "sensor_age_s",
        "lamp_is_off",
        "lamp_level",
        "lamp_temp_dtr",
        "lamp_temp_dtr1",
        "cct_kelvin",
        "runtime_s",
        "action",
        "reason",
        "user_text",
    ]

    def __init__(self, mode: str):
        TELEM_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = TELEM_DIR / f"run_{stamp}_{mode}.csv"
        self.mode = mode
        self._lock = threading.Lock()
        self._fh = None
        self._writer = None
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
    user_text: str = "",
) -> dict:
    now_epoch = time.time()
    now_iso = datetime.fromtimestamp(now_epoch).isoformat(timespec="seconds")

    sensor_age_s = (now_epoch - snap.updated_at) if getattr(snap, "updated_at", 0.0) else -1.0
    temp_dtr, temp_dtr1 = lamp.state.last_temp

    return {
        "ts_epoch": round(now_epoch, 3),
        "ts_iso": now_iso,
        "mode": mode,
        "raw_present": getattr(snap, "raw_present", None),
        "filt_occupied": getattr(snap, "filt_occupied", None),
        "moving": getattr(snap, "moving", None),
        "stationary": getattr(snap, "stationary", None),
        "lux": getattr(snap, "lux", None),
        "moving_age_ms": getattr(snap, "moving_age_ms", None),
        "moving_events": getattr(snap, "moving_events", None),
        "sensor_age_s": round(sensor_age_s, 3),
        "lamp_is_off": lamp.state.is_off,
        "lamp_level": lamp.state.last_level,
        "lamp_temp_dtr": temp_dtr,
        "lamp_temp_dtr1": temp_dtr1,
        "cct_kelvin": dtr_to_kelvin(temp_dtr, temp_dtr1),
        "runtime_s": round(runtime_tracker.get("total_s", 0), 1),
        "action": action,
        "reason": reason,
        "user_text": user_text,
    }


# ---------------- Main ----------------

def parse_args():
    p = argparse.ArgumentParser(description="KoL DALI Lighting Control")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sensor-port", required=True)
    p.add_argument("--sensor-baud", type=int, default=115200)
    p.add_argument("--auto", action="store_true", help="Auto on/off based on occupancy")
    p.add_argument(
        "--mode",
        choices=["baseline", "ai"],
        default="baseline",
        help="Labels telemetry so you can compare baseline vs AI runs later.",
    )
    p.add_argument("--web", action="store_true", help="Start the web dashboard server")
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
        operator = AIOperator(lamp, dry_run=args.dry_run)

        # A single lock for ALL lamp actions (sensor thread + AI thread + web)
        lamp_lock = threading.Lock()

        # ---- Sensor reader init ----
        reader = UsbOccupancyReader(args.sensor_port, args.sensor_baud)
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
            "nominal_power_watts": args.nominal_power,
            "runtime_tracker": runtime_tracker,
        }

        # ---- Adaptive engine (for AI mode) ----
        adaptive_engine = None
        if args.mode == "ai":
            from .adaptive_engine import AdaptiveEngine
            adaptive_engine = AdaptiveEngine(
                lamp, lamp_lock,
                nominal_power_watts=args.nominal_power,
            )
            # Try to load existing models, otherwise train
            if not adaptive_engine.load_models():
                adaptive_engine.train_from_baseline()

            def on_adaptive_action(action_str, reason_str):
                snap = reader.snapshot()
                telem.log_row(build_row(
                    mode=app_state["mode"], snap=snap, lamp=lamp,
                    runtime_tracker=runtime_tracker,
                    action=action_str, reason=reason_str,
                ))

            adaptive_engine.on_action = on_adaptive_action
            adaptive_engine.start(reader)
            app_state["adaptive_engine"] = adaptive_engine

        # ---- Sensor loop (telemetry + auto-occupancy for baseline) ----

        def sensor_loop():
            # State tracking
            last_filt = None
            vacant_start = None
            last_log_at = 0.0
            last_telem_at = 0.0

            # Settings (thesis-aligned: 60s absence timeout)
            DIM_DELAY = 60.0   # Seconds to wait at "dim" level before turning off
            DIM_LEVEL = 10     # Percent brightness for the warning dim

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
                        app_state["nominal_power_watts"] * dimming_frac * dt / 3600.0
                    )

                # --- Telemetry heartbeat: 5-second intervals (thesis spec) ---
                if now - last_telem_at >= 5.0:
                    telem.log_row(build_row(
                        mode=app_state["mode"], snap=snap, lamp=lamp,
                        runtime_tracker=runtime_tracker,
                    ))
                    last_telem_at = now

                # Only run auto-occupancy if enabled and sensor is sending data
                # In AI mode, occupancy is handled by the adaptive engine
                if app_state["auto"] and app_state["mode"] == "baseline" and (filt is not None):

                    # --- Case 1: Someone is PRESENT ---
                    if filt:
                        if (not last_filt) or (vacant_start is not None):
                            logging.info("AUTO: OCCUPIED -> Restoring light")
                            with lamp_lock:
                                lamp.set_brightness_pct(75)
                                save_state(lamp.state)

                                telem.log_row(
                                    build_row(
                                        mode=app_state["mode"],
                                        snap=snap,
                                        lamp=lamp,
                                        runtime_tracker=runtime_tracker,
                                        action="set_brightness_pct(75)",
                                        reason="auto_occupied_restore",
                                    )
                                )

                        vacant_start = None
                        last_filt = True

                    # --- Case 2: Area is VACANT ---
                    else:
                        if last_filt:
                            logging.info(f"AUTO: VACANT -> Dimming to {DIM_LEVEL}% for {DIM_DELAY}s")
                            vacant_start = time.time()
                            last_filt = False
                            with lamp_lock:
                                lamp.set_brightness_pct(DIM_LEVEL)

                                telem.log_row(
                                    build_row(
                                        mode=app_state["mode"],
                                        snap=snap,
                                        lamp=lamp,
                                        runtime_tracker=runtime_tracker,
                                        action=f"set_brightness_pct({DIM_LEVEL})",
                                        reason="auto_vacant_dim",
                                    )
                                )

                        if vacant_start and (time.time() - vacant_start > DIM_DELAY):
                            logging.info("AUTO: VACANT Timer expired -> Turning OFF")
                            with lamp_lock:
                                lamp.off()
                                save_state(lamp.state)

                                telem.log_row(
                                    build_row(
                                        mode=app_state["mode"],
                                        snap=snap,
                                        lamp=lamp,
                                        runtime_tracker=runtime_tracker,
                                        action="off()",
                                        reason="auto_vacant_off",
                                    )
                                )

                            vacant_start = None

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
                        )
                    )

        # ---- Start threads ----
        t1 = threading.Thread(target=sensor_loop, name="sensor-loop", daemon=True)
        t1.start()

        # Start web server if requested
        if args.web:
            from .web_server import run_server
            run_server(app_state, host="0.0.0.0", port=args.web_port)
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
