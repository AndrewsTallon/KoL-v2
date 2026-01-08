import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import serial  # pyserial


RAW_RE = re.compile(r"\braw\s*=\s*(PRESENT|CLEAR)\b", re.IGNORECASE)


@dataclass
class OccupancyEvent:
    present: bool
    ts: float
    line: str


class UsbOccupancyReader:
    """
    Reads lines from a USB serial device (ESP32) and emits OccupancyEvent(present=True/False)
    whenever it sees 'raw=PRESENT' or 'raw=CLEAR' in a line.
    """

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        on_event: Optional[Callable[[OccupancyEvent], None]] = None,
    ):
        self.port = port
        self.baud = baud
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ser: Optional[serial.Serial] = None

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                logging.info("Opening USB serial %s @ %d ...", self.port, self.baud)
                self._ser = serial.Serial(self.port, self.baud, timeout=1)
                # ESP32 often resets on open; give it a moment
                time.sleep(1.0)

                while not self._stop.is_set():
                    raw = self._ser.readline()
                    if not raw:
                        continue
                    try:
                        line = raw.decode("utf-8", errors="ignore").strip()
                    except Exception:
                        continue
                    if not line:
                        continue

                    m = RAW_RE.search(line)
                    if not m:
                        continue

                    present = m.group(1).upper() == "PRESENT"
                    evt = OccupancyEvent(present=present, ts=time.monotonic(), line=line)
                    if self.on_event:
                        self.on_event(evt)

            except Exception as exc:
                logging.warning("USB serial error (%s). Reconnecting in 2s...", exc)
                try:
                    if self._ser:
                        self._ser.close()
                except Exception:
                    pass
                self._ser = None
                time.sleep(2.0)
