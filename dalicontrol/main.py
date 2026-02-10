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
        "action": action,
        "reason": reason,
        "user_text": user_text,
    }


# ---------------- Main ----------------

def parse_args():
    p = argparse.ArgumentParser()
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

        # A single lock for ALL lamp actions (sensor thread + AI thread)
        lamp_lock = threading.Lock()

        # ---- Sensor reader init ----
        reader = UsbOccupancyReader(args.sensor_port, args.sensor_baud)
        reader.start()

        stop = threading.Event()

        def sensor_loop():
            # State tracking
            last_filt = None
            vacant_start = None
            last_log_at = 0.0
            last_telem_at = 0.0

            # Settings
            DIM_DELAY = 10.0  # Seconds to wait at "dim" level before turning off
            DIM_LEVEL = 10    # Percent brightness for the warning dim

            while not stop.is_set():
                snap = reader.snapshot()
                filt = snap.filt_occupied

                # --- Telemetry heartbeat: 1 row/sec even if nothing changes ---
                now = time.time()
                if now - last_telem_at >= 1.0:
                    telem.log_row(build_row(mode=args.mode, snap=snap, lamp=lamp))
                    last_telem_at = now

                # Only run if --auto flag was used and sensor is sending data
                if args.auto and (filt is not None):

                    # --- Case 1: Someone is PRESENT ---
                    if filt:
                        if (not last_filt) or (vacant_start is not None):
                            logging.info("AUTO: OCCUPIED -> Restoring light")
                            with lamp_lock:
                                lamp.set_brightness_pct(75)
                                save_state(lamp.state)

                                telem.log_row(
                                    build_row(
                                        mode=args.mode,
                                        snap=snap,
                                        lamp=lamp,
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
                                        mode=args.mode,
                                        snap=snap,
                                        lamp=lamp,
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
                                        mode=args.mode,
                                        snap=snap,
                                        lamp=lamp,
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
                            mode=args.mode,
                            snap=snap,
                            lamp=lamp,
                            action="user_command",
                            reason="user_text",
                            user_text=user_text,
                        )
                    )

        t1 = threading.Thread(target=sensor_loop, name="sensor-loop", daemon=True)
        t2 = threading.Thread(target=input_loop, name="input-loop", daemon=True)
        t1.start()
        t2.start()

        logging.info("Running. Auto=%s. Mode=%s. Ctrl-C to exit.", args.auto, args.mode)

        while not stop.is_set():
            time.sleep(0.5)

    finally:
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
