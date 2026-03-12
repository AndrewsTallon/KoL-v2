from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Callable, Dict, Any, List, Tuple, Optional

from .dali_transport import DaliHidTransport

# ---------- Helpers ----------

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def pct_to_level(pct: float) -> int:
    pct = clamp(float(pct), 0.0, 100.0)
    return int(round((pct / 100.0) * 254))


# Typical tunable-white lamp range.  Adjust to your lamp's actual datasheet.
# If you don't know the exact range, these are safe defaults for most TW lamps.
MIREK_WARMEST = 370   # ≈ 2703 K  (lowest CCT / warmest)
MIREK_COOLEST = 154   # ≈ 6494 K  (highest CCT / coolest)

# Inter-frame gap in seconds.  The capture showed ~27 ms on the bus.
# 30 ms gives a comfortable margin.
DALI_FRAME_GAP = 0.030


def kelvin_to_mirek(kelvin: int) -> int:
    """Convert Kelvin to Mirek, clamped to the lamp's physical range."""
    kelvin = clamp(int(kelvin), 1000, 20000)
    mirek = int(round(1_000_000 / kelvin))
    return clamp(mirek, MIREK_COOLEST, MIREK_WARMEST)


def mirek_to_dtr(mirek: int) -> Tuple[int, int]:
    """Split a 16-bit Mirek value into (DTR0, DTR1)."""
    mirek = clamp(int(mirek), 0, 65535)
    dtr0 = mirek & 0xFF          # LSB
    dtr1 = (mirek >> 8) & 0xFF   # MSB
    return dtr0, dtr1


def kelvin_to_dtr(kelvin: int) -> Tuple[int, int]:
    """Kelvin → clamped Mirek → (DTR0, DTR1)."""
    return mirek_to_dtr(kelvin_to_mirek(kelvin))


# ---------- DALI Controls ----------

class DaliControls:
    def __init__(self, tx: DaliHidTransport):
        self.tx = tx

    # ── helpers ──────────────────────────────────────────────────────────
    def _gap(self):
        """Enforce inter-frame settling time."""
        time.sleep(DALI_FRAME_GAP)

    # ── Brightness ──────────────────────────────────────────────────────
    def off(self):
        self.tx.send_dali16(0xFF, 0x00)

    def recall_max(self):
        self.tx.send_dali16(0xFF, 0x05)

    def recall_min(self):
        self.tx.send_dali16(0xFF, 0x06)

    def set_arc_level(self, level: int):
        level = clamp(int(level), 0, 254)
        self.tx.send_dali16(0xFE, level)

    def set_arc_pct(self, pct: float):
        self.set_arc_level(pct_to_level(pct))

    # ── DT8 Tunable White ──────────────────────────────────────────────
    def dt8_enable(self):
        """Enable Device Type 8 — must precede every DT8 command."""
        self.tx.send_dali16(0xC1, 0x08)

    def dt8_set_temp_tc(self):
        """Set Temporary Colour Temperature Tc (uses current DTR0/DTR1)."""
        self.tx.send_dali16(0xFF, 0xE7)

    def dt8_activate(self):
        """Activate the temporary colour value."""
        self.tx.send_dali16(0xFF, 0xE2)

    def dt8_set_temp_raw(self, dtr0: int, dtr1: int):
        """
        Full DT8 Tc sequence using raw DTR values.

        Matches confirmed bus sequence from Wireshark capture:
          A3 dtr0 → C3 dtr1 → C1 08 → FF E7 → C1 08 → FF E2
        with ~30 ms gaps between each frame.
        """
        self.tx.send_dali16(0xA3, dtr0)     # DTR0  (Mirek LSB)
        self._gap()
        self.tx.send_dali16(0xC3, dtr1)     # DTR1  (Mirek MSB)
        self._gap()
        self.dt8_enable()                    # Enable DT8
        self._gap()
        self.dt8_set_temp_tc()               # Set Temporary Tc
        self._gap()
        self.dt8_enable()                    # Enable DT8 again
        self._gap()
        self.dt8_activate()                  # Activate

    def dt8_set_mirek(self, mirek: int):
        """Set colour temperature by Mirek value (clamped to lamp range)."""
        mirek = clamp(int(mirek), MIREK_COOLEST, MIREK_WARMEST)
        dtr0, dtr1 = mirek_to_dtr(mirek)
        self.dt8_set_temp_raw(dtr0, dtr1)

    def dt8_set_kelvin(self, kelvin: int):
        """Set colour temperature by Kelvin (converted & clamped)."""
        dtr0, dtr1 = kelvin_to_dtr(kelvin)
        self.dt8_set_temp_raw(dtr0, dtr1)

    def dt8_set_pct(self, pct: float):
        """
        Set colour temperature by percentage.
        0 % = warmest (MIREK_WARMEST), 100 % = coolest (MIREK_COOLEST).
        """
        pct = clamp(float(pct), 0.0, 100.0)
        mirek = int(round(
            MIREK_WARMEST + (MIREK_COOLEST - MIREK_WARMEST) * (pct / 100.0)
        ))
        self.dt8_set_mirek(mirek)


# ---------- AI-friendly command catalog ----------

COMMANDS: Dict[str, Dict[str, Any]] = {
    "off": {
        "description": "Turn luminaire off (broadcast).",
        "params": {},
        "frames": [["FF", "00"]],
    },
    "recall_max": {
        "description": "Recall MAX level (often used as ON).",
        "params": {},
        "frames": [["FF", "05"]],
    },
    "recall_min": {
        "description": "Recall MIN level.",
        "params": {},
        "frames": [["FF", "06"]],
    },
    "set_brightness_level": {
        "description": "Set brightness using DIRECT ARC POWER (0..254).",
        "params": {"level": {"type": "int", "min": 0, "max": 254}},
        "frames": [["FE", "level"]],
    },
    "set_brightness_pct": {
        "description": "Set brightness by percent (0..100).",
        "params": {"pct": {"type": "float", "min": 0.0, "max": 100.0}},
        "frames": [["FE", "pct_to_level(pct)"]],
    },
    "set_temp_raw": {
        "description": "DT8: Set colour temperature using raw DTR0/DTR1 and activate.",
        "params": {
            "dtr0": {"type": "int", "min": 0, "max": 255},
            "dtr1": {"type": "int", "min": 0, "max": 255},
        },
        "frames": [
            ["A3", "dtr0"],
            ["C3", "dtr1"],
            ["C1", "08"],
            ["FF", "E7"],
            ["C1", "08"],
            ["FF", "E2"],
        ],
    },
    "set_temp_mirek": {
        "description": "DT8: Set colour temperature in Mirek (clamped to lamp range).",
        "params": {
            "mirek": {"type": "int", "min": MIREK_COOLEST, "max": MIREK_WARMEST},
        },
    },
    "set_temp_kelvin": {
        "description": "DT8: Set colour temperature in Kelvin (converted & clamped).",
        "params": {
            "kelvin": {"type": "int", "min": 2700, "max": 6500},
        },
    },
    "set_temp_pct": {
        "description": "DT8: Set colour temperature by percent (0=warmest, 100=coolest).",
        "params": {
            "pct": {"type": "float", "min": 0.0, "max": 100.0},
        },
    },
    "set_temp_preset_warm": {
        "description": "DT8: warmest endpoint (2700 K / 370 Mirek).",
        "params": {},
    },
    "set_temp_preset_cool": {
        "description": "DT8: coolest endpoint (6500 K / 154 Mirek).",
        "params": {},
    },
}


# ---------- Executor ----------

def execute_command(ctrl: DaliControls, name: str, **kwargs):
    if name == "off":
        return ctrl.off()
    if name == "recall_max":
        return ctrl.recall_max()
    if name == "recall_min":
        return ctrl.recall_min()
    if name == "set_brightness_level":
        return ctrl.set_arc_level(kwargs["level"])
    if name == "set_brightness_pct":
        return ctrl.set_arc_pct(kwargs["pct"])
    if name == "set_temp_raw":
        return ctrl.dt8_set_temp_raw(kwargs["dtr0"], kwargs["dtr1"])
    if name == "set_temp_mirek":
        return ctrl.dt8_set_mirek(kwargs["mirek"])
    if name == "set_temp_kelvin":
        return ctrl.dt8_set_kelvin(kwargs["kelvin"])
    if name == "set_temp_pct":
        return ctrl.dt8_set_pct(kwargs["pct"])
    if name == "set_temp_preset_warm":
        return ctrl.dt8_set_kelvin(2700)
    if name == "set_temp_preset_cool":
        return ctrl.dt8_set_kelvin(6500)
    raise ValueError(f"Unknown command: {name}")
