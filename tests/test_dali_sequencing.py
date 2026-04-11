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


class StateAwarenessTests(unittest.TestCase):
    """Tests for ambient lux estimation and max-step brightness clamping."""

    def make_engine(
        self,
        *,
        prediction=(80.0, 4000),
        initial_brightness=50.0,
        initial_cct=4000,
        lamp_lux_at_100pct=400.0,
        max_brightness_step_pct=15.0,
        reader_snaps=None,
    ):
        controls = FakeControls()
        lamp = LampController(
            controls,
            LampState(
                last_level=pct_to_level(initial_brightness),
                last_temp=kelvin_to_dtr(initial_cct),
                is_off=(initial_brightness == 0),
            ),
        )
        settings = Settings(
            lamp_lux_at_100pct=lamp_lux_at_100pct,
            max_brightness_step_pct=max_brightness_step_pct,
            brightness_feedback_window_s=0.0,
        )
        engine = FixedPredictionEngine(
            lamp,
            threading.Lock(),
            settings=settings,
            prediction=prediction,
        )
        engine._reader = FakeReader(reader_snaps or [FakeSnap(), FakeSnap(lux_smooth=105.0)])
        engine._current_brightness_pct = initial_brightness
        engine.on_action_calls = []

        def on_action(action, reason, rationale, context):
            engine.on_action_calls.append((action, reason, rationale, context))

        engine.on_action = on_action
        return engine, controls

    # ---- Ambient lux estimation tests ----

    def test_ambient_lux_estimation_subtracts_lamp_contribution(self):
        engine, _ = self.make_engine(
            initial_brightness=50.0, lamp_lux_at_100pct=400.0
        )
        # Sensor reads 400 lux, lamp at 50% contributes 200 lux
        ambient = engine._estimate_ambient_lux(400.0)
        self.assertAlmostEqual(ambient, 200.0, delta=1.0)

    def test_ambient_lux_floors_at_zero(self):
        engine, _ = self.make_engine(
            initial_brightness=100.0, lamp_lux_at_100pct=400.0
        )
        # Sensor reads 300 lux, lamp at 100% contributes 400 lux -> clamp to 0
        ambient = engine._estimate_ambient_lux(300.0)
        self.assertEqual(ambient, 0.0)

    def test_lamp_off_returns_raw_lux(self):
        engine, _ = self.make_engine(initial_brightness=0.0)
        engine.lamp.state.is_off = True
        ambient = engine._estimate_ambient_lux(250.0)
        self.assertAlmostEqual(ambient, 250.0)

    def test_no_prior_state_uses_lamp_level(self):
        engine, _ = self.make_engine(
            initial_brightness=50.0, lamp_lux_at_100pct=400.0
        )
        engine._current_brightness_pct = None  # simulate no prior state
        # Should fall back to lamp.state.last_level (50%)
        ambient = engine._estimate_ambient_lux(400.0)
        self.assertAlmostEqual(ambient, 200.0, delta=1.0)

    # ---- Max-step clamping tests ----

    def test_brightness_step_clamped_to_max(self):
        # Engine at 50%, prediction says 90% -> should clamp to 50+15=65%
        engine, controls = self.make_engine(
            prediction=(90.0, 4000),
            initial_brightness=50.0,
            initial_cct=4000,
            max_brightness_step_pct=15.0,
        )

        engine._apply_adaptive(FakeSnap())

        brightness_calls = [c for c in controls.calls if c[0] == "brightness"]
        self.assertTrue(len(brightness_calls) >= 1)
        sent_level = brightness_calls[0][1]
        # 65% of 254 ≈ 165
        self.assertLessEqual(sent_level, pct_to_level(65.0) + 1)
        self.assertGreaterEqual(sent_level, pct_to_level(65.0) - 1)

    def test_brightness_step_clamped_downward(self):
        # Engine at 80%, prediction says 20% -> should clamp to 80-15=65%
        engine, controls = self.make_engine(
            prediction=(20.0, 4000),
            initial_brightness=80.0,
            initial_cct=4000,
            max_brightness_step_pct=15.0,
        )

        engine._apply_adaptive(FakeSnap())

        brightness_calls = [c for c in controls.calls if c[0] == "brightness"]
        self.assertTrue(len(brightness_calls) >= 1)
        sent_level = brightness_calls[0][1]
        # 65% of 254 ≈ 165
        self.assertLessEqual(sent_level, pct_to_level(65.0) + 1)
        self.assertGreaterEqual(sent_level, pct_to_level(65.0) - 1)

    def test_no_clamping_on_presence_restore(self):
        # When lamp was off, brightness should jump directly to recommended
        engine, controls = self.make_engine(
            prediction=(80.0, 4000),
            initial_brightness=0.0,
            initial_cct=4000,
            max_brightness_step_pct=15.0,
        )
        engine.lamp.state.is_off = True
        engine._current_brightness_pct = 0.0

        engine._apply_adaptive(FakeSnap(), reason="presence_restore")

        brightness_calls = [c for c in controls.calls if c[0] == "brightness"]
        self.assertTrue(len(brightness_calls) >= 1)
        sent_level = brightness_calls[0][1]
        # Should be approximately 80%, not clamped to 15%
        self.assertGreater(sent_level, pct_to_level(60.0))

    def test_small_delta_not_clamped(self):
        # Engine at 50%, prediction says 55% -> within 15% step, no clamping
        engine, controls = self.make_engine(
            prediction=(55.0, 4000),
            initial_brightness=50.0,
            initial_cct=4000,
            max_brightness_step_pct=15.0,
        )

        engine._apply_adaptive(FakeSnap())

        brightness_calls = [c for c in controls.calls if c[0] == "brightness"]
        self.assertTrue(len(brightness_calls) >= 1)
        sent_level = brightness_calls[0][1]
        # Should be approximately 55%, not altered
        self.assertAlmostEqual(sent_level, pct_to_level(55.0), delta=1)

    # ---- Settings serialization test ----

    def test_settings_serialize_state_awareness_fields(self):
        settings = Settings()
        self.assertEqual(settings.to_dict()["max_brightness_step_pct"], 15.0)
        self.assertEqual(settings.to_dict()["lamp_lux_at_100pct"], 400.0)


if __name__ == "__main__":
    unittest.main()
