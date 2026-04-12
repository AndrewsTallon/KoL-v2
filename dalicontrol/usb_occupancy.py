import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import serial


# Tokens that mean "no port configured" (legacy/default placeholder).
_EMPTY_PORT_TOKENS = {"", "none", "null", "auto"}


def _normalize_port(port: Optional[str]) -> Optional[str]:
    """Return a real port string, or None if the value is a blank/placeholder."""
    if port is None:
        return None
    stripped = port.strip()
    if not stripped or stripped.lower() in _EMPTY_PORT_TOKENS:
        return None
    return stripped


def list_available_ports() -> List[dict]:
    """Enumerate currently visible serial ports for UI/API consumption."""
    import serial.tools.list_ports

    results: List[dict] = []
    for p in serial.tools.list_ports.comports():
        results.append({
            "device": p.device,
            "description": p.description or "",
            "hwid": p.hwid or "",
        })
    return results


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
    sensor_uptime_s: Optional[int] = None

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


# Signature key sets we accept as "this is our ESP32 sensor".
# Legacy firmware always sent raw+occupied+lux; newer firmware may omit one of
# those in the very first line but always includes moving/stationary.
_SIGNATURE_KEY_SETS = (
    {"raw", "occupied", "lux"},
    {"moving", "stationary"},
    {"occupied", "lux"},
)


def _looks_like_sensor(data: dict) -> bool:
    keys = set(data.keys())
    return any(sig.issubset(keys) for sig in _SIGNATURE_KEY_SETS)


def detect_sensor_port(
    baud: int = 115200,
    per_port_seconds: float = 3.0,
    post_open_settle: float = 1.2,
) -> Optional[str]:
    """
    Auto-detect the ESP32 sensor serial port.

    Enumerates all COM ports, opens each briefly, waits for the ESP32 to finish
    its DTR/RTS boot reset, then reads lines for up to ``per_port_seconds``
    looking for JSON output with our sensor signature keys.

    Returns the port device string (e.g. "COM3") or None if not found.
    """
    import serial.tools.list_ports

    candidates = serial.tools.list_ports.comports()
    if not candidates:
        logging.warning("Auto-detect: no serial ports enumerated by the OS.")
        return None

    logging.info(
        "Auto-detecting ESP32 sensor among %d port(s): %s",
        len(candidates),
        ", ".join(f"{p.device} ({p.description or 'n/a'})" for p in candidates),
    )

    for port_info in candidates:
        port = port_info.device
        logging.info("  Probing %s [%s] hwid=%s ...",
                     port, port_info.description or "n/a", port_info.hwid or "n/a")
        try:
            ser = serial.Serial(port, baud, timeout=0.5)
        except (serial.SerialException, PermissionError, OSError) as exc:
            logging.info("  -> skip %s: %s", port, exc)
            continue

        try:
            # Opening a CP210x/CH340 bridge asserts DTR/RTS which resets the
            # ESP32; give it a moment to boot before reading.
            time.sleep(post_open_settle)
            try:
                ser.reset_input_buffer()
            except Exception:
                pass

            deadline = time.time() + per_port_seconds
            while time.time() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="ignore").strip()
                if not text or not text.startswith("{"):
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and _looks_like_sensor(data):
                    logging.info("  -> ESP32 sensor detected on %s", port)
                    return port
        finally:
            try:
                ser.close()
            except Exception:
                pass

    logging.warning("Auto-detection found no ESP32 sensor on any port.")
    return None


class UsbOccupancyReader:
    """
    Reads JSON lines from the ESP32 over USB serial.

    Expected format includes:
      {"raw":true, "occupied":true, "moving":false, "stationary":true, "lux":375.83}
    """

    def __init__(
        self,
        port: Optional[str],
        baud: int = 115200,
        on_port_detected: Optional[Callable[[str], None]] = None,
    ):
        # An empty/placeholder port means "no port yet — auto-detect".
        self.port: Optional[str] = _normalize_port(port)
        self.baud = baud
        self.on_port_detected = on_port_detected
        self.status = OccupancyStatus()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()

        # Triggers the _run loop to drop the current serial and reopen.
        self._reconnect = threading.Event()

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

    def set_port(self, new_port: Optional[str]) -> Optional[str]:
        """Switch to a new port at runtime.

        Pass ``None`` / ``""`` to clear the port and resume auto-detection.
        Returns the normalized port value now in effect.
        """
        normalized = _normalize_port(new_port)
        with self._lock:
            self.port = normalized
        # Drop the current serial so the _run loop reconnects on the new port.
        self._reconnect.set()
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass
        logging.info("Sensor port set to %s", normalized or "(auto-detect)")
        return normalized

    def _open(self) -> serial.Serial:
        assert self.port, "UsbOccupancyReader._open called with no port"
        return serial.Serial(self.port, self.baud, timeout=1, exclusive=True)

    def _wait_for_port(self) -> Optional[str]:
        """When no port is configured, periodically retry auto-detection.

        Returns the detected port, or None if stopped before one was found.
        """
        # Announce once per entry so the log doesn't spam.
        logging.info("No sensor port configured; scanning for ESP32 every 5s...")
        while not self._stop.is_set():
            # Allow a manual set_port() to short-circuit the scan.
            if self.port:
                return self.port
            detected = None
            try:
                detected = detect_sensor_port(baud=self.baud)
            except Exception as exc:
                logging.debug("Auto-detect probe raised: %s", exc)
            if detected:
                with self._lock:
                    self.port = detected
                if self.on_port_detected:
                    try:
                        self.on_port_detected(detected)
                    except Exception as exc:
                        logging.warning("on_port_detected callback failed: %s", exc)
                return detected
            # Wait before the next scan, but exit quickly on stop / set_port.
            self._reconnect.wait(timeout=5.0)
            self._reconnect.clear()
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.port:
                    if not self._wait_for_port():
                        return  # stopped
                self._reconnect.clear()
                logging.info("Opening USB serial %s @ %s ...", self.port, self.baud)
                self._ser = self._open()
                logging.info("USB occupancy reader running on %s.", self.port)

                # Flush partial junk
                try:
                    self._ser.reset_input_buffer()
                except Exception:
                    pass

                while not self._stop.is_set():
                    # A runtime set_port() (or clear) asks us to drop and reopen.
                    if self._reconnect.is_set():
                        break
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
                                        "still_dist", "still_energy", "seq", "uptime_s",
                                        "confirm_count", "filter_stage"):
                                val = data.get(key)
                                if val is not None:
                                    attr = {
                                        "seq": "sensor_seq",
                                        "uptime_s": "sensor_uptime_s",
                                    }.get(key, key)
                                    setattr(self.status, attr, val)

                            self.status.last_line = text
                            self.status.updated_at = now

                    except json.JSONDecodeError:
                        continue
                    except Exception:
                        continue

            except PermissionError:
                logging.warning("USB Access Denied on %s. Retrying in 2s...", self.port)
                time.sleep(2)
            except Exception as exc:
                # If the user just asked for a reconnect/clear, the close()
                # they triggered is expected — don't spam a warning.
                if self._reconnect.is_set():
                    logging.info("Sensor port changed; reopening...")
                else:
                    logging.warning("USB error on %s (%s). Reconnecting in 2s...",
                                    self.port, exc)
                    time.sleep(2)
            finally:
                try:
                    if self._ser:
                        self._ser.close()
                except Exception:
                    pass
                self._ser = None
