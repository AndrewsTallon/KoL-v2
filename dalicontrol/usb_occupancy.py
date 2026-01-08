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
    last_line: str = ""
    updated_at: float = 0.0

class UsbOccupancyReader:
    """
    Reads JSON lines from the ESP32 over USB serial.
    Expected format: {"raw":true, "occupied":true, ...}
    """

    def __init__(self, port: str, baud: int = 115200):
        self.port = port
        self.baud = baud
        self.status = OccupancyStatus()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()

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
            # Return a copy of the current status
            return OccupancyStatus(**self.status.__dict__)

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
                        
                        # [FIX] Parse JSON instead of Regex
                        data = json.loads(text)
                        
                        # Extract the booleans from the JSON structure
                        # {"raw": true, "occupied": true, ...}
                        raw_val = data.get("raw")
                        filt_val = data.get("occupied")

                        with self._lock:
                            if raw_val is not None:
                                self.status.raw_present = bool(raw_val)
                            if filt_val is not None:
                                self.status.filt_occupied = bool(filt_val)
                            
                            self.status.last_line = text
                            self.status.updated_at = time.time()

                    except json.JSONDecodeError:
                        # Ignore lines that aren't valid JSON (like startup messages)
                        continue
                    except Exception:
                        continue

            except PermissionError:
                logging.warning("USB Access Denied. Retrying in 2s...")
                time.sleep(2)
            except Exception as exc:
                logging.warning("USB error (%s). Reconnecting in 2s...", exc)
                time.sleep(2)