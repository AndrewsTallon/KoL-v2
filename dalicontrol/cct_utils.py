"""
CCT Kelvin ↔ DALI DTR0/DTR1 mapping utility.

The luminaire uses Mirek (reciprocal megakelvin) encoding for DT8
tunable-white control.  The 16-bit Mirek value is split across two
data-transfer registers:

    DTR0 = Mirek & 0xFF          (LSB)
    DTR1 = (Mirek >> 8) & 0xFF   (MSB)

Conversion:
    Mirek  = 1,000,000 / Kelvin
    Kelvin = 1,000,000 / Mirek
"""

from typing import Tuple

# Lamp range (same constants as dali_controls.py)
MIREK_WARMEST = 370   # ≈ 2703 K
MIREK_COOLEST = 154   # ≈ 6494 K


def kelvin_to_dtr(kelvin: int) -> Tuple[int, int]:
    """Convert a CCT value in Kelvin to (DTR0, DTR1) register values.

    Uses Mirek encoding: Mirek = 1,000,000 / Kelvin, clamped to lamp range,
    then split into LSB/MSB.
    """
    kelvin = max(1000, min(20000, int(kelvin)))
    mirek = int(round(1_000_000 / kelvin))
    mirek = max(MIREK_COOLEST, min(MIREK_WARMEST, mirek))
    dtr0 = mirek & 0xFF
    dtr1 = (mirek >> 8) & 0xFF
    return (dtr0, dtr1)


def dtr_to_kelvin(dtr0: int, dtr1: int) -> int:
    """Convert (DTR0, DTR1) register values back to CCT in Kelvin.

    Reconstructs the 16-bit Mirek value and converts to Kelvin.
    """
    mirek = ((dtr1 & 0xFF) << 8) | (dtr0 & 0xFF)
    if mirek == 0:
        return 0
    return int(round(1_000_000 / mirek))


def level_to_pct(level: int) -> float:
    """Convert DALI ARC level (0-254) to percentage (0-100)."""
    return round((max(0, min(254, level)) / 254.0) * 100.0, 1)


def pct_to_level(pct: float) -> int:
    """Convert percentage (0-100) to DALI ARC level (0-254)."""
    return int(round((max(0.0, min(100.0, pct)) / 100.0) * 254))
