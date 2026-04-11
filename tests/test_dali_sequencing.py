import threading
import time
import unittest
from unittest.mock import patch

from dalicontrol.adaptive_engine import AdaptiveEngine
from dalicontrol.cct_utils import kelvin_to_dtr, pct_to_level
from dalicontrol.lamp_state import LampController, LampState
from dalicontrol.settings import Settings


class FakeControls:
    def __init__(self):
        self.calls = []

    def set_arc_level(self, level: int):
        self.calls.append(("brightness", level))

    def dt8_set_temp_raw(self, dtr0: int, dtr1: int):
        self.calls.append(("cct", dtr0, dtr1))

    def off(self):
        self.calls.append(("off",))


class FakeSnap:
    def __init__(self, *, lux=100.0, lux_smooth=100.0, updated_at=None):
        self.lux = lux
        self.lux_smooth = lux_smooth
        self.updated_at = time.time() if updated_at is None else updated_at
        self.filt_occupied = True


class FakeReader:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    def snapshot(self):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


class FixedPredictionEngine(AdaptiveEngine):
    def __init__(self, *args, prediction, **kwargs):
        super().__init__(*args, **kwargs)
        self.prediction = prediction

    def predict(self, lux, hour=None):
        self._brightness_source = "test"
        self._cct_source = "test"
        self._prediction_source = "test / test"
        return self.prediction

    def _get_weather_context(self, lux, hour):
        return "test weather"


class DaliSequencingTests(unittest.TestCase):
    def make_engine(
        self,
        *,
        prediction=(80.0, 6500),
        initial_brightness=50.0,
        initial_cct=2700,
        reader_snaps=None,
        command_gap=0.75,
        feedback_window=0.0,
        feedback_min_delta=3.0,
    ):
        controls = FakeControls()
        lamp = LampController(
            controls,
            LampState(
                last_level=pct_to_level(initial_brightness),
                last_temp=kelvin_to_dtr(initial_cct),
                is_off=False,
            ),
        )
        settings = Settings(
            dali_command_gap_s=command_gap,
            brightness_feedback_window_s=feedback_window,
            brightness_feedback_min_lux_delta=feedback_min_delta,
        )
        engine = FixedPredictionEngine(
            lamp,
            threading.Lock(),
            settings=settings,
            prediction=prediction,
        )
        engine._reader = FakeReader(reader_snaps or [FakeSnap(), FakeSnap(lux_smooth=105.0)])
        engine.on_action_calls = []

        def on_action(action, reason, rationale, context):
            engine.on_action_calls.append((action, reason, rationale, context))

        engine.on_action = on_action
        return engine, controls

    def test_combined_adjustment_sends_cct_before_brightness_and_applies_gap(self):
        engine, controls = self.make_engine()
        sleeps = []

        with patch("dalicontrol.adaptive_engine.time.sleep", side_effect=sleeps.append):
            engine._apply_adaptive(FakeSnap())

        self.assertEqual(controls.calls[0][0], "cct")
        self.assertEqual(controls.calls[1][0], "brightness")
        self.assertIn(0.75, sleeps)

    def test_brightness_only_change_uses_feedback_path(self):
        engine, controls = self.make_engine(
            prediction=(80.0, 2700),
            initial_cct=2700,
        )

        engine._apply_adaptive(FakeSnap())

        self.assertEqual([call[0] for call in controls.calls], ["brightness"])
        action = engine.on_action_calls[0][0]
        self.assertIn("brightness_feedback(confirmed)", action)

    def test_fresh_lux_movement_prevents_retry(self):
        engine, controls = self.make_engine(
            prediction=(80.0, 2700),
            initial_cct=2700,
            reader_snaps=[FakeSnap(lux_smooth=100.0), FakeSnap(lux_smooth=104.0)],
        )

        engine._apply_adaptive(FakeSnap())

        self.assertEqual([call[0] for call in controls.calls], ["brightness"])
        rationale = engine.on_action_calls[0][2]
        self.assertIn("confirmed lux_smooth", rationale)

    def test_missing_lux_does_not_retry_and_logs_unverified(self):
        engine, controls = self.make_engine(
            prediction=(80.0, 2700),
            initial_cct=2700,
            reader_snaps=[FakeSnap(lux_smooth=None), FakeSnap(lux_smooth=104.0)],
        )

        engine._apply_adaptive(FakeSnap())

        self.assertEqual([call[0] for call in controls.calls], ["brightness"])
        action = engine.on_action_calls[0][0]
        self.assertIn("brightness_feedback(unverified)", action)

    def test_no_lux_movement_retries_brightness_once(self):
        engine, controls = self.make_engine(
            prediction=(80.0, 2700),
            initial_cct=2700,
            reader_snaps=[FakeSnap(lux_smooth=100.0), FakeSnap(lux_smooth=101.0)],
            command_gap=0.0,
        )

        engine._apply_adaptive(FakeSnap())

        self.assertEqual([call[0] for call in controls.calls], ["brightness", "brightness"])
        action = engine.on_action_calls[0][0]
        self.assertIn("brightness_feedback(retried)", action)

    def test_settings_serialize_new_timing_fields(self):
        settings = Settings()

        self.assertEqual(settings.to_dict()["dali_command_gap_s"], 0.75)
        self.assertEqual(settings.to_dict()["brightness_feedback_window_s"], 2.5)
        self.assertEqual(settings.to_dict()["brightness_feedback_min_lux_delta"], 3.0)

        with patch.object(Settings, "save", lambda self: None):
            settings.update({
                "dali_command_gap_s": 1.25,
                "brightness_feedback_window_s": 4.0,
                "brightness_feedback_min_lux_delta": 5.5,
            })

        self.assertEqual(settings.dali_command_gap_s, 1.25)
        self.assertEqual(settings.brightness_feedback_window_s, 4.0)
        self.assertEqual(settings.brightness_feedback_min_lux_delta, 5.5)


if __name__ == "__main__":
    unittest.main()
