"""
Energy estimation from telemetry data.

Implements thesis Section 3.5.2:
  E_estimated = Σ (P_nominal × dimming_level_i × Δt_i)

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
        sampling_interval_s: Time between samples (seconds).

    Returns:
        EnergyReport or None if file cannot be processed.
    """
    try:
        total_runtime_s = 0.0
        total_absence_lit_s = 0.0
        total_energy_ws = 0.0  # watt-seconds
        dimming_sum = 0.0
        on_count = 0
        sample_count = 0

        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sample_count += 1

                is_off_str = row.get("lamp_is_off", "").lower()
                is_off = is_off_str in ("true", "1")

                if is_off:
                    continue

                # Lamp is on
                level_str = row.get("lamp_level", "")
                if not level_str or level_str == "None":
                    continue

                level = int(level_str)
                dimming_frac = level / 254.0  # 0.0 to 1.0

                total_runtime_s += sampling_interval_s
                dimming_sum += level_to_pct(level)
                on_count += 1

                # Energy: P_nominal × dimming_fraction × Δt
                total_energy_ws += nominal_power_watts * dimming_frac * sampling_interval_s

                # Check if unoccupied while lit
                occupied_str = row.get("filt_occupied", "").lower()
                if occupied_str not in ("true", "1"):
                    total_absence_lit_s += sampling_interval_s

        avg_dimming = (dimming_sum / on_count) if on_count > 0 else 0.0

        return EnergyReport(
            total_runtime_s=total_runtime_s,
            total_absence_lit_s=total_absence_lit_s,
            estimated_energy_wh=total_energy_ws / 3600.0,
            average_dimming_pct=round(avg_dimming, 1),
            sample_count=sample_count,
            nominal_power_w=nominal_power_watts,
        )

    except Exception as exc:
        logger.error("Energy estimation failed for %s: %s", csv_path, exc)
        return None
