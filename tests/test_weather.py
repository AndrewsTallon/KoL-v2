import json
import unittest
from unittest.mock import patch

from dalicontrol.weather import (
    WeatherApiError,
    fetch_current_weather,
    fetch_forecast,
    geocode_location,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class WeatherHelperTests(unittest.TestCase):
    @patch("dalicontrol.weather.urllib.request.urlopen")
    def test_geocode_location_returns_selectable_candidates(self, urlopen):
        urlopen.return_value = FakeResponse([
            {
                "name": "London",
                "state": "England",
                "country": "GB",
                "lat": 51.5073,
                "lon": -0.1276,
            },
            {
                "name": "London",
                "state": "Ontario",
                "country": "CA",
                "lat": 42.9834,
                "lon": -81.233,
            },
        ])

        locations = geocode_location("London", "secret-key")

        self.assertEqual(len(locations), 2)
        self.assertEqual(locations[0]["label"], "London, England, GB")
        self.assertEqual(locations[0]["lat"], 51.5073)
        self.assertIn("limit=5", urlopen.call_args.args[0])
        self.assertIn("appid=secret-key", urlopen.call_args.args[0])

    @patch("dalicontrol.weather.urllib.request.urlopen")
    def test_geocode_location_returns_empty_list_for_no_candidates(self, urlopen):
        urlopen.return_value = FakeResponse([])

        self.assertEqual(geocode_location("Not A Place", "secret-key"), [])

    @patch("dalicontrol.weather.urllib.request.urlopen")
    def test_forecast_is_normalized(self, urlopen):
        urlopen.return_value = FakeResponse({
            "city": {
                "name": "Pfarrkirchen",
                "country": "DE",
                "coord": {"lat": 48.43, "lon": 12.94},
                "timezone": 7200,
            },
            "list": [
                {
                    "dt": 1775919600,
                    "dt_txt": "2026-04-11 15:00:00",
                    "main": {"temp": 12.6},
                    "weather": [{"main": "Clouds", "description": "broken clouds"}],
                    "pop": 0.25,
                }
            ],
        })

        data = fetch_forecast(48.43, 12.94, "secret-key", cnt=8)

        self.assertEqual(data["location"]["name"], "Pfarrkirchen")
        self.assertEqual(data["forecast"][0]["condition"], "Clouds")
        self.assertEqual(data["forecast"][0]["temp_c"], 12.6)
        self.assertEqual(data["forecast"][0]["pop"], 0.25)
        self.assertIn("cnt=8", urlopen.call_args.args[0])

    @patch("dalicontrol.weather.urllib.request.urlopen")
    def test_current_weather_is_normalized(self, urlopen):
        urlopen.return_value = FakeResponse({
            "name": "Pfarrkirchen",
            "sys": {"country": "DE"},
            "main": {"temp": 11.2, "humidity": 88},
            "weather": [{"main": "Rain", "description": "light rain"}],
        })

        data = fetch_current_weather(48.43, 12.94, "secret-key")

        self.assertEqual(data["condition"], "Rain")
        self.assertEqual(data["description"], "light rain")
        self.assertEqual(data["temp_c"], 11.2)
        self.assertEqual(data["humidity"], 88)

    @patch("dalicontrol.weather.urllib.request.urlopen")
    def test_api_error_does_not_expose_api_key(self, urlopen):
        urlopen.side_effect = TimeoutError("timed out for secret-key")

        with self.assertRaises(WeatherApiError) as ctx:
            fetch_current_weather(48.43, 12.94, "secret-key")

        self.assertNotIn("secret-key", str(ctx.exception))
        self.assertIn("OpenWeather request failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
