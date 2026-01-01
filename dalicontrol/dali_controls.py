from dataclasses import dataclass
from typing import Callable, Dict, Any, List, Tuple, Optional

from dali_transport import DaliHidTransport

# ---------- Helpers ----------
def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def pct_to_level(pct: float) -> int:
    pct = clamp(float(pct), 0.0, 100.0)
    return int(round((pct / 100.0) * 254))

# ---------- DALI Controls ----------
class DaliControls:
    def __init__(self, tx: DaliHidTransport):
        self.tx = tx

    # --- Brightness ---
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

    # --- DT8 Tunable White ---
    def dt8_enable(self):
        self.tx.send_dali16(0xC1, 0x08)

    def dt8_set_temp_tc(self):
        self.tx.send_dali16(0xFF, 0xE7)

    def dt8_activate(self):
        self.tx.send_dali16(0xFF, 0xE2)

    def dt8_set_temp_raw(self, dtr: int, dtr1: int):
        # Based on your confirmed working sequence
        self.tx.send_dali16(0xA3, dtr)   # DTR
        self.tx.send_dali16(0xC3, dtr1)  # DTR1
        self.dt8_enable()
        self.dt8_set_temp_tc()
        self.dt8_enable()
        self.dt8_activate()

# ---------- AI-friendly command catalog ----------
# A compact schema your AI can use to choose and validate commands.
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
        "description": "DT8: Set temporary colour temperature using DTR/DTR1 and activate.",
        "params": {
            "dtr": {"type": "int", "min": 0, "max": 255},
            "dtr1": {"type": "int", "min": 0, "max": 255},
        },
        "frames": [
            ["A3", "dtr"],
            ["C3", "dtr1"],
            ["C1", "08"],
            ["FF", "E7"],
            ["C1", "08"],
            ["FF", "E2"],
        ],
    },
    "set_temp_preset_warm": {
        "description": "DT8: warm endpoint (captured).",
        "params": {},
        "frames": [
            ["A3", "10"], ["C3", "27"], ["C1", "08"], ["FF", "E7"], ["C1", "08"], ["FF", "E2"]
        ],
    },
    "set_temp_preset_cool": {
        "description": "DT8: cool endpoint (captured).",
        "params": {},
        "frames": [
            ["A3", "32"], ["C3", "00"], ["C1", "08"], ["FF", "E7"], ["C1", "08"], ["FF", "E2"]
        ],
    },
}

# ---------- Executor (AI can call this safely) ----------
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
        return ctrl.dt8_set_temp_raw(kwargs["dtr"], kwargs["dtr1"])
    if name == "set_temp_preset_warm":
        return ctrl.dt8_set_temp_raw(0x10, 0x27)
    if name == "set_temp_preset_cool":
        return ctrl.dt8_set_temp_raw(0x32, 0x00)
    raise ValueError(f"Unknown command: {name}")
