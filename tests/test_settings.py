import json
import os
import tempfile
import unittest
from pathlib import Path

import dalicontrol.settings as settings_module
from dalicontrol.ai_operator import AIOperator
from dalicontrol.settings import Settings


class SettingsStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_settings_path = settings_module.SETTINGS_PATH
        self.old_openai_key = os.environ.get("OPENAI_API_KEY")
        self.old_openai_model = os.environ.get("OPENAI_MODEL")
        settings_module.SETTINGS_PATH = Path(self.tmp.name) / "settings.json"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_MODEL", None)

    def tearDown(self):
        settings_module.SETTINGS_PATH = self.old_settings_path
        if self.old_openai_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self.old_openai_key
        if self.old_openai_model is None:
            os.environ.pop("OPENAI_MODEL", None)
        else:
            os.environ["OPENAI_MODEL"] = self.old_openai_model
        self.tmp.cleanup()

    def test_load_imports_openai_env_key_into_portable_settings(self):
        os.environ["OPENAI_API_KEY"] = "sk-from-env"

        settings = Settings.load()

        self.assertEqual(settings.openai_api_key, "sk-from-env")
        with settings_module.SETTINGS_PATH.open("r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["openai_api_key"], "sk-from-env")
        self.assertEqual(saved["weather_api_key"], "")

    def test_load_keeps_existing_openai_key_over_env_fallback(self):
        settings_module.SETTINGS_PATH.write_text(
            json.dumps({
                "openai_api_key": "sk-existing",
                "openai_model": "gpt-custom",
                "weather_api_key": "weather-existing",
                "weather_location": "Berlin",
            }),
            encoding="utf-8",
        )
        os.environ["OPENAI_API_KEY"] = "sk-from-env"

        settings = Settings.load()

        self.assertEqual(settings.openai_api_key, "sk-existing")
        self.assertEqual(settings.openai_model, "gpt-custom")
        self.assertEqual(settings.weather_api_key, "weather-existing")
        self.assertEqual(settings.weather_location, "Berlin")

    def test_update_persists_openai_and_weather_credentials(self):
        settings = Settings.load()

        settings.update({
            "openai_api_key": "sk-updated",
            "openai_model": "gpt-4o-mini",
            "weather_api_key": "weather-updated",
            "weather_location": "51.5,-0.1",
            "weather_lat": 51.5,
            "weather_lon": -0.1,
            "weather_location_label": "London, GB",
        })

        with settings_module.SETTINGS_PATH.open("r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["openai_api_key"], "sk-updated")
        self.assertEqual(saved["openai_model"], "gpt-4o-mini")
        self.assertEqual(saved["weather_api_key"], "weather-updated")
        self.assertEqual(saved["weather_location"], "51.5,-0.1")
        self.assertEqual(saved["weather_lat"], 51.5)
        self.assertEqual(saved["weather_lon"], -0.1)
        self.assertEqual(saved["weather_location_label"], "London, GB")

    def test_weather_coordinates_are_validated_and_clearable(self):
        settings = Settings.load()

        with self.assertRaises(ValueError):
            settings.update({"weather_lat": 91})
        with self.assertRaises(ValueError):
            settings.update({"weather_lon": -181})

        settings.update({
            "weather_lat": 48.43,
            "weather_lon": 12.94,
            "weather_location_label": "Pfarrkirchen, Bavaria, DE",
        })
        settings.update({"weather_lat": None, "weather_lon": None})

        self.assertIsNone(settings.weather_lat)
        self.assertIsNone(settings.weather_lon)
        self.assertEqual(settings.weather_location_label, "Pfarrkirchen, Bavaria, DE")

    def test_ai_operator_prefers_settings_then_env_then_default_model(self):
        os.environ["OPENAI_API_KEY"] = "sk-env"
        os.environ["OPENAI_MODEL"] = "gpt-env"
        settings = Settings(openai_api_key="sk-settings", openai_model="gpt-settings")

        operator = AIOperator(lamp=object(), settings=settings)

        self.assertEqual(operator._openai_api_key(), "sk-settings")
        self.assertEqual(operator._openai_model(), "gpt-settings")

        settings.openai_api_key = ""
        settings.openai_model = ""
        self.assertEqual(operator._openai_api_key(), "sk-env")
        self.assertEqual(operator._openai_model(), "gpt-env")

        os.environ.pop("OPENAI_MODEL")
        self.assertEqual(operator._openai_model(), "gpt-4o-mini")


if __name__ == "__main__":
    unittest.main()
