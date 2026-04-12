"""
Energy estimation from telemetry data.

Implements thesis Section 3.5.2:
  E_estimated = sum(P_nominal * dimming_level_i * dt_i)

Since direct power measurement is not available, energy is estimated from
luminaire runtime and dimming levels using nominal power specifications.
"""

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .cct_utils import level_to_pct

logger = logging.getLogger(__name__)


def _is_true(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _float_or_none(value) -> Optional[float]:
    try:
        if value in ("", None, "None"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_dt(row: dict, next_row: Optional[dict], fallback_s: float) -> float:
    sample_dt = _float_or_none(row.get("sample_dt_s"))
    if sample_dt is not None and sample_dt >= 0:
        return sample_dt

    ts = _float_or_none(row.get("ts_epoch"))
    next_ts = _float_or_none((next_row or {}).get("ts_epoch"))
    if ts is not None and next_ts is not None and next_ts >= ts:
        return next_ts - ts

    return fallback_s


@dataclass
class EnergyReport:
    """Summary of energy-related metrics for a telemetry run."""

    total_runtime_s: float        # seconds lamp was on
    total_absence_lit_s: float    # seconds lamp was on while unoccupied
    estimated_energy_wh: float    # watt-hours
    average_dimming_pct: float    # average brightness %
    sample_count: int
    nominal_power_w: float


def estimate_energy(
    csv_path: Path,
    nominal_power_watts: float = 40.0,
    sampling_interval_s: float = 5.0,
) -> Optional[EnergyReport]:
    """Compute energy estimation from a telemetry CSV file.

    Args:
        csv_path: Path to the telemetry CSV.
        nominal_power_watts: Rated power of luminaire at 100%.
        sampling_interval_s: Fallback time between samples in seconds.

    Returns:
        EnergyReport or None if file cannot be processed.
    """
    try:
        total_runtime_s = 0.0
        total_absence_lit_s = 0.0
        total_energy_ws = 0.0  # watt-seconds
        dimming_sum = 0.0
        on_count = 0

        with open(csv_path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        for index, row in enumerate(rows):
            next_row = rows[index + 1] if index + 1 < len(rows) else None
            dt_s = _row_dt(row, next_row, sampling_interval_s)

            # A DALI level can be retained while off, so off rows are always 0 W.
            if _is_true(row.get("lamp_is_off", "")):
                continue

            level_str = row.get("lamp_level", "")
            if not level_str or level_str == "None":
                continue

            level = int(level_str)
            dimming_frac = level / 254.0

            total_runtime_s += dt_s
            dimming_sum += level_to_pct(level)
            on_count += 1
            total_energy_ws += nominal_power_watts * dimming_frac * dt_s

            if not _is_true(row.get("filt_occupied", "")):
                total_absence_lit_s += dt_s

        avg_dimming = (dimming_sum / on_count) if on_count > 0 else 0.0

        return EnergyReport(
            total_runtime_s=round(total_runtime_s, 3),
            total_absence_lit_s=round(total_absence_lit_s, 3),
            estimated_energy_wh=total_energy_ws / 3600.0,
            average_dimming_pct=round(avg_dimming, 1),
            sample_count=len(rows),
            nominal_power_w=nominal_power_watts,
        )

    except Exception as exc:
        logger.error("Energy estimation failed for %s: %s", csv_path, exc)
        return None
