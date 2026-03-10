import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import serial


@dataclass
class OccupancyStatus:
    raw_present: Optional[bool] = None
    filt_occupied: Optional[bool] = None

    # richer signals from ESP32 JSON
    moving: Optional[bool] = None
    stationary: Optional[bool] = None

    # BH1750 lux is a float (your ESP32 sends e.g. 375.83)
    lux: Optional[float] = None

    # BH1750 smoothed lux (EMA from ESP32)
    lux_smooth: Optional[float] = None
    lux_ok: Optional[bool] = None

    # Radar signal detail
    move_dist: Optional[int] = None
    move_energy: Optional[int] = None
    still_dist: Optional[int] = None
    still_energy: Optional[int] = None

    # ESP32 heartbeat sequence counter
    sensor_seq: Optional[int] = None

    # Occupancy filter diagnostics
    confirm_count: Optional[int] = None
    filter_stage: Optional[str] = None

    # timing/diagnostics
    updated_at: float = 0.0
    last_line: str = ""

    last_moving_at: float = 0.0
    last_occupied_at: float = 0.0
    moving_events: int = 0        # counts rising edges of moving
    moving_age_ms: int = -1       # ms since last moving=true (computed at snapshot time)


class UsbOccupancyReader:
    """
    Reads JSON lines from the ESP32 over USB serial.

    Expected format includes:
      {"raw":true, "occupied":true, "moving":false, "stationary":true, "lux":375.83}
    """

    def __init__(self, port: str, baud: int = 115200):
        self.port = port
        self.baud = baud
        self.status = OccupancyStatus()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()

        # internal edge tracking
        self._moving_prev: Optional[bool] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="usb-occupancy", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass

    def snapshot(self) -> OccupancyStatus:
        with self._lock:
            snap = OccupancyStatus(**self.status.__dict__)

        # compute moving age at snapshot time (so it’s always current)
        if snap.last_moving_at and snap.last_moving_at > 0:
            snap.moving_age_ms = int((time.time() - snap.last_moving_at) * 1000)
        else:
            snap.moving_age_ms = -1

        return snap

    def _open(self) -> serial.Serial:
        return serial.Serial(self.port, self.baud, timeout=1, exclusive=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                logging.info("Opening USB serial %s @ %s ...", self.port, self.baud)
                self._ser = self._open()
                logging.info("USB occupancy reader running.")

                # Flush partial junk
                try:
                    self._ser.reset_input_buffer()
                except Exception:
                    pass

                while not self._stop.is_set():
                    line = self._ser.readline()
                    if not line:
                        continue

                    try:
                        text = line.decode("utf-8", errors="ignore").strip()
                        if not text:
                            continue

                        data = json.loads(text)

                        # Extract (backward compatible)
                        raw_val = data.get("raw")
                        occ_val = data.get("occupied")

                        # NEW fields
                        mov_val = data.get("moving")
                        sta_val = data.get("stationary")
                        lux_val = data.get("lux")

                        now = time.time()

                        with self._lock:
                            if raw_val is not None:
                                self.status.raw_present = bool(raw_val)

                            if occ_val is not None:
                                self.status.filt_occupied = bool(occ_val)
                                if self.status.filt_occupied:
                                    self.status.last_occupied_at = now

                            if mov_val is not None:
                                m = bool(mov_val)
                                self.status.moving = m

                                # rising edge count
                                if self._moving_prev is False and m is True:
                                    self.status.moving_events += 1
                                self._moving_prev = m

                                if m:
                                    self.status.last_moving_at = now

                            if sta_val is not None:
                                self.status.stationary = bool(sta_val)

                            if lux_val is not None:
                                # BH1750 lux is float; accept int/float/str
                                try:
                                    self.status.lux = float(lux_val)
                                except Exception:
                                    pass

                            # Parse extended sensor fields (additive, never break old firmware)
                            for key in ("lux_smooth", "lux_ok", "move_dist", "move_energy",
                                        "still_dist", "still_energy", "seq",
                                        "confirm_count", "filter_stage"):
                                val = data.get(key)
                                if val is not None:
                                    attr = "sensor_seq" if key == "seq" else key
                                    setattr(self.status, attr, val)

                            self.status.last_line = text
                            self.status.updated_at = now

                    except json.JSONDecodeError:
                        continue
                    except Exception:
                        continue

            except PermissionError:
                logging.warning("USB Access Denied. Retrying in 2s...")
                time.sleep(2)
            except Exception as exc:
                logging.warning("USB error (%s). Reconnecting in 2s...", exc)
                time.sleep(2)
