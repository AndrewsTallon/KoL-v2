import csv
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalicontrol.energy_estimator import estimate_energy
from dalicontrol.lamp_state import LampState
from dalicontrol.main import TelemetryLogger, build_row


class ThesisTelemetryTests(unittest.TestCase):
    def make_snap(self, occupied=True):
        return SimpleNamespace(
            updated_at=time.time(),
            raw_present=occupied,
            filt_occupied=occupied,
            moving=False,
            stationary=occupied,
            lux=100.0,
            lux_smooth=100.0,
            moving_age_ms=0,
            moving_events=0,
        )

    def make_lamp(self, *, level=127, is_off=False):
        return SimpleNamespace(
            state=LampState(
                last_level=level,
                last_temp=(0x9A, 0x00),
                is_off=is_off,
            )
        )

    def test_build_row_logs_effective_brightness_pct(self):
        row = build_row(
            mode="ai",
            snap=self.make_snap(),
            lamp=self.make_lamp(level=127, is_off=False),
            runtime_tracker={"total_s": 1.0, "energy_wh": 0.0},
            nominal_power_watts=40.0,
        )

        self.assertEqual(row["lamp_level"], 127)
        self.assertAlmostEqual(row["lamp_brightness_pct"], 50.0, places=1)
        self.assertAlmostEqual(row["lamp_brightness_frac"], 0.5, places=2)
        self.assertAlmostEqual(row["lamp_estimated_power_w"], 20.0, places=1)
        self.assertEqual(row["sample_type"], "heartbeat")

    def test_build_row_treats_off_lamp_as_zero_brightness(self):
        row = build_row(
            mode="ai",
            snap=self.make_snap(occupied=False),
            lamp=self.make_lamp(level=210, is_off=True),
            runtime_tracker={"total_s": 1.0, "energy_wh": 0.0},
            nominal_power_watts=40.0,
        )

        self.assertEqual(row["lamp_level"], 210)
        self.assertEqual(row["lamp_brightness_pct"], 0.0)
        self.assertEqual(row["lamp_brightness_frac"], 0.0)
        self.assertEqual(row["lamp_estimated_power_w"], 0.0)
        self.assertFalse(row["lighting_during_absence"])

    def test_build_row_logs_ai_context_fields(self):
        row = build_row(
            mode="ai",
            snap=self.make_snap(),
            lamp=self.make_lamp(),
            runtime_tracker={"total_s": 1.0, "energy_wh": 0.0},
            sample_type="ai_action",
            rec_brightness_pct=75.5,
            rec_cct_kelvin=5000,
            brightness_delta_pct=12.5,
            cct_delta_kelvin=300,
            brightness_final_pct=75.5,
            brightness_unclamped_pct=80.0,
            ml_brightness_adjust_pct=1.0,
            preference_brightness_adjust_pct=2.0,
            weather_brightness_adjust_pct=3.0,
            model_type="brightness: test, cct: test",
            cct_reasoning="test cct rationale",
        )

        self.assertEqual(row["sample_type"], "ai_action")
        self.assertEqual(row["rec_brightness_pct"], 75.5)
        self.assertEqual(row["rec_cct_kelvin"], 5000)
        self.assertEqual(row["brightness_delta_pct"], 12.5)
        self.assertEqual(row["cct_delta_kelvin"], 300)
        self.assertEqual(row["cct_reasoning"], "test cct rationale")

    def test_telemetry_logger_populates_sample_dt(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dalicontrol.main.TELEM_DIR", Path(tmp)):
                logger = TelemetryLogger("ai")
                try:
                    logger.log_row({"ts_epoch": 10.0, "ts_iso": "t1"})
                    logger.log_row({"ts_epoch": 12.5, "ts_iso": "t2"})
                finally:
                    logger.close()

                with logger.path.open("r", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))

        self.assertEqual(rows[0]["sample_dt_s"], "0.0")
        self.assertEqual(rows[1]["sample_dt_s"], "2.5")

    def test_energy_estimator_uses_sample_dt_and_skips_off_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "ts_epoch",
                        "sample_dt_s",
                        "lamp_is_off",
                        "lamp_level",
                        "filt_occupied",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "ts_epoch": 1,
                    "sample_dt_s": 0,
                    "lamp_is_off": "False",
                    "lamp_level": 254,
                    "filt_occupied": "True",
                })
                writer.writerow({
                    "ts_epoch": 2,
                    "sample_dt_s": 2,
                    "lamp_is_off": "False",
                    "lamp_level": 127,
                    "filt_occupied": "True",
                })
                writer.writerow({
                    "ts_epoch": 4,
                    "sample_dt_s": 10,
                    "lamp_is_off": "True",
                    "lamp_level": 254,
                    "filt_occupied": "False",
                })

            report = estimate_energy(path, nominal_power_watts=40.0)

        self.assertIsNotNone(report)
        self.assertEqual(report.total_runtime_s, 2.0)
        self.assertEqual(report.total_absence_lit_s, 0.0)
        self.assertAlmostEqual(report.estimated_energy_wh, (40.0 * 0.5 * 2.0) / 3600.0, places=4)

    def test_energy_estimator_falls_back_to_timestamp_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old_run.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["ts_epoch", "lamp_is_off", "lamp_level", "filt_occupied"],
                )
                writer.writeheader()
                writer.writerow({
                    "ts_epoch": 1,
                    "lamp_is_off": "False",
                    "lamp_level": 254,
                    "filt_occupied": "False",
                })
                writer.writerow({
                    "ts_epoch": 4,
                    "lamp_is_off": "False",
                    "lamp_level": 254,
                    "filt_occupied": "True",
                })

            report = estimate_energy(path, nominal_power_watts=40.0, sampling_interval_s=5.0)

        self.assertIsNotNone(report)
        self.assertEqual(report.total_runtime_s, 8.0)
        self.assertEqual(report.total_absence_lit_s, 3.0)


if __name__ == "__main__":
    unittest.main()
