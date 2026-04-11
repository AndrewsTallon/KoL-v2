import threading
import unittest

from dalicontrol.adaptive_engine import AdaptiveEngine
from dalicontrol.lamp_state import LampController
from dalicontrol.main import _decisions_lock, _recent_decisions, record_decision


class FakeControls:
    def set_arc_level(self, level: int):
        pass

    def dt8_set_temp_raw(self, dtr0: int, dtr1: int):
        pass

    def off(self):
        pass


class FakeSnap:
    lux = 250.0
    filt_occupied = True


def make_engine(weather):
    lamp = LampController(FakeControls())
    engine = AdaptiveEngine(lamp, threading.Lock())
    engine._fetch_weather = lambda: weather
    return engine


class AdaptiveWeatherBrightnessTests(unittest.TestCase):
    def test_rain_adds_bounded_brightness_nudge_under_dim_lux(self):
        engine = make_engine({"condition": "Rain"})

        brightness, _ = engine.predict(250.0, hour=12.0)

        base = engine._circadian_brightness(250.0, 12.0)
        self.assertEqual(brightness, base + 6.0)
        self.assertEqual(
            engine._last_brightness_context["weather_brightness_adjust_pct"],
            6.0,
        )
        reason = engine._build_brightness_reasoning(
            "midday peak alertness", brightness
        )
        self.assertIn("rain weather added 6%", reason)
        self.assertIn("target is 500 lux", reason)

    def test_clouds_add_smaller_nudge_for_moderate_lux(self):
        engine = make_engine({"condition": "Clouds"})

        brightness, _ = engine.predict(400.0, hour=12.0)

        base = engine._circadian_brightness(400.0, 12.0)
        self.assertEqual(brightness, base + 2.0)

    def test_clear_and_missing_weather_do_not_change_brightness(self):
        clear_engine = make_engine({"condition": "Clear"})
        missing_engine = make_engine(None)

        clear_brightness, _ = clear_engine.predict(250.0, hour=12.0)
        missing_brightness, _ = missing_engine.predict(250.0, hour=12.0)

        base = clear_engine._circadian_brightness(250.0, 12.0)
        self.assertEqual(clear_brightness, base)
        self.assertEqual(missing_brightness, base)
        self.assertEqual(
            missing_engine._last_brightness_context[
                "weather_brightness_adjust_pct"
            ],
            0.0,
        )

    def test_weather_nudge_is_clamped_to_max_brightness(self):
        engine = make_engine({"condition": "Thunderstorm"})

        brightness, _ = engine.predict(0.0, hour=12.0)

        self.assertEqual(brightness, 100.0)
        self.assertEqual(
            engine._last_brightness_context["weather_brightness_adjust_pct"],
            8.0,
        )

    def test_record_decision_preserves_brightness_reasoning_fields(self):
        with _decisions_lock:
            _recent_decisions.clear()

        record_decision(
            action="set_brightness_pct(56)",
            reason="adaptive_eval",
            rationale="Brightness 56% because rain weather added 6%.",
            snap=FakeSnap(),
            mode="ai",
            context={
                "brightness_reasoning": "Brightness 56% because rain weather added 6%.",
                "target_lux": 500.0,
                "brightness_base_pct": 50.0,
                "weather_brightness_adjust_pct": 6.0,
            },
        )

        with _decisions_lock:
            entry = _recent_decisions[-1]

        self.assertEqual(
            entry["brightness_reasoning"],
            "Brightness 56% because rain weather added 6%.",
        )
        self.assertEqual(entry["target_lux"], 500.0)
        self.assertEqual(entry["brightness_base_pct"], 50.0)
        self.assertEqual(entry["weather_brightness_adjust_pct"], 6.0)


if __name__ == "__main__":
    unittest.main()
