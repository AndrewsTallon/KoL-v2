import argparse
import logging
import threading
import time
from typing import Optional

from .ai_operator import AIOperator, load_state, save_state
from .dali_controls import DaliControls
from .dali_transport import DaliHidTransport
from .lamp_state import LampController
from .usb_occupancy import UsbOccupancyReader


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sensor-port", required=True)
    p.add_argument("--sensor-baud", type=int, default=115200)
    p.add_argument("--auto", action="store_true", help="Auto on/off based on occupancy")
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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

                    # Settings
                    DIM_DELAY = 10.0  # Seconds to wait at "dim" level before turning off
                    DIM_LEVEL = 10    # Percent brightness for the warning dim

                    while not stop.is_set():
                        snap = reader.snapshot()
                        filt = snap.filt_occupied

                        # Only run if --auto flag was used and sensor is sending data
                        if args.auto and (filt is not None):
                            
                            # --- Case 1: Someone is PRESENT ---
                            if filt:
                                # If we just detected a person (transition from False -> True)
                                # OR if we are currently counting down a vacancy timer
                                if (not last_filt) or (vacant_start is not None):
                                    logging.info("AUTO: OCCUPIED -> Restoring light")
                                    with lamp_lock:
                                        # Force brightness to 75% (or whatever you prefer)
                                        lamp.set_brightness_pct(75) 
                                        save_state(lamp.state)
                                
                                # Reset vacancy logic
                                vacant_start = None
                                last_filt = True

                            # --- Case 2: Area is VACANT ---
                            else:
                                # Edge detection: We *just* lost occupancy
                                if last_filt:
                                    logging.info(f"AUTO: VACANT -> Dimming to {DIM_LEVEL}% for {DIM_DELAY}s")
                                    vacant_start = time.time()
                                    last_filt = False
                                    with lamp_lock:
                                        lamp.set_brightness_pct(DIM_LEVEL)
                                
                                # Timer check: Have we been vacant long enough?
                                if vacant_start and (time.time() - vacant_start > DIM_DELAY):
                                    logging.info("AUTO: VACANT Timer expired -> Turning OFF")
                                    with lamp_lock:
                                        lamp.off()
                                        save_state(lamp.state)
                                    vacant_start = None  # Stop checking until someone returns

                        # Periodic sensor health log
                        now = time.time()
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

                        time.sleep(0.1)  # Check 10 times a second for fast response

        def input_loop():
            # Interactive typed commands/questions
            while not stop.is_set():
                try:
                    user_text = input("you> ").strip()
                except (EOFError, KeyboardInterrupt):
                    stop.set()
                    break

                if not user_text:
                    continue

                # Provide sensor status to the operator (for "status?" questions)
                snap = reader.snapshot()
                sensor_status = {
                    "raw_present": snap.raw_present,
                    "filt_occupied": snap.filt_occupied,
                    "last_line": snap.last_line,
                    "age_s": (time.time() - snap.updated_at) if snap.updated_at else None,
                }

                # Ensure AI actions don't collide with auto actions
                with lamp_lock:
                    operator.handle_user_text(user_text, sensor_status=sensor_status)

        t1 = threading.Thread(target=sensor_loop, name="sensor-loop", daemon=True)
        t2 = threading.Thread(target=input_loop, name="input-loop", daemon=True)
        t1.start()
        t2.start()

        logging.info(
            "Running. Auto=%s. Type commands anytime. Ctrl-C to exit.",
            args.auto,
        )

        # Keep main alive until stop
        while not stop.is_set():
            time.sleep(0.5)

    finally:
        try:
            if tx:
                tx.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()