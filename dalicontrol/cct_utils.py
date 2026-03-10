"""
CCT Kelvin ↔ DALI DTR/DTR1 mapping utility.

The luminaire uses raw DTR/DTR1 bytes for DT8 tunable-white control.
This module provides conversion between human-readable Kelvin values
and the hardware register values.

Known calibration points (from lamp_state.py):
  WARM_PRESET = (0x10, 0x27)  →  ~2700 K
  COOL_PRESET = (0x32, 0x00)  →  ~6500 K
"""

from typing import Tuple

# Calibration endpoints
WARM_K = 2700
COOL_K = 6500

WARM_DTR = 0x10   # 16
WARM_DTR1 = 0x27  # 39
COOL_DTR = 0x32   # 50
COOL_DTR1 = 0x00  # 0


def kelvin_to_dtr(kelvin: int) -> Tuple[int, int]:
    """Convert a CCT value in Kelvin to (DTR, DTR1) register values.

    Linearly interpolates between warm (2700K) and cool (6500K) endpoints.
    Values outside range are clamped.
    """
    kelvin = max(WARM_K, min(COOL_K, int(kelvin)))
    t = (kelvin - WARM_K) / (COOL_K - WARM_K)  # 0.0=warm, 1.0=cool

    dtr = int(round(WARM_DTR + t * (COOL_DTR - WARM_DTR)))
    dtr1 = int(round(WARM_DTR1 + t * (COOL_DTR1 - WARM_DTR1)))

    dtr = max(0, min(255, dtr))
    dtr1 = max(0, min(255, dtr1))
    return (dtr, dtr1)


def dtr_to_kelvin(dtr: int, dtr1: int) -> int:
    """Convert (DTR, DTR1) register values back to approximate CCT in Kelvin.

    Uses DTR as the primary interpolation axis since it has the wider range.
    """
    if COOL_DTR == WARM_DTR:
        return WARM_K
    t = (dtr - WARM_DTR) / (COOL_DTR - WARM_DTR)
    t = max(0.0, min(1.0, t))
    return int(round(WARM_K + t * (COOL_K - WARM_K)))


def level_to_pct(level: int) -> float:
    """Convert DALI ARC level (0-254) to percentage (0-100)."""
    return round((max(0, min(254, level)) / 254.0) * 100.0, 1)


def pct_to_level(pct: float) -> int:
    """Convert percentage (0-100) to DALI ARC level (0-254)."""
    return int(round((max(0.0, min(100.0, pct)) / 100.0) * 254))
