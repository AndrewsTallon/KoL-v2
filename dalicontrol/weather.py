"""OpenWeatherMap helpers for current weather, forecast, and geocoding."""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List


class WeatherApiError(RuntimeError):
    """Raised when OpenWeather cannot be reached or returns an error."""


def _redact(message: str, redact_values=()) -> str:
    safe = str(message)
    for value in redact_values:
        if value:
            safe = safe.replace(str(value), "[redacted]")
    return safe


def _get_json(url: str, timeout: float = 5.0, redact_values=()) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = payload.get("message") or payload.get("error")
        except Exception:
            detail = None
        message = detail or f"OpenWeather returned HTTP {exc.code}"
        raise WeatherApiError(_redact(message, redact_values)) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise WeatherApiError("OpenWeather request failed") from exc


def _build_url(base_url: str, params: Dict[str, Any]) -> str:
    return f"{base_url}?{urllib.parse.urlencode(params)}"


def _coerce_float(value: Any) -> float:
    return float(value)


def geocode_location(query: str, api_key: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Resolve a user-entered location into selectable coordinate candidates."""
    query = (query or "").strip()
    api_key = (api_key or "").strip()
    if not api_key:
        raise WeatherApiError("Weather API key is required")
    if not query:
        return []

    url = _build_url(
        "https://api.openweathermap.org/geo/1.0/direct",
        {
            "q": query,
            "limit": max(1, min(int(limit), 5)),
            "appid": api_key,
        },
    )
    data = _get_json(url, redact_values=(api_key,))
    if not isinstance(data, list):
        raise WeatherApiError("OpenWeather geocoding returned an unexpected response")

    candidates = []
    for item in data:
        if not isinstance(item, dict) or "lat" not in item or "lon" not in item:
            continue
        name = str(item.get("name") or "").strip()
        state = str(item.get("state") or "").strip()
        country = str(item.get("country") or "").strip()
        label_parts = [part for part in (name, state, country) if part]
        candidates.append({
            "name": name,
            "state": state,
            "country": country,
            "lat": _coerce_float(item["lat"]),
            "lon": _coerce_float(item["lon"]),
            "label": ", ".join(label_parts),
        })
    return candidates


def fetch_current_weather(lat: float, lon: float, api_key: str) -> Dict[str, Any]:
    """Fetch normalized current weather by coordinate."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise WeatherApiError("Weather API key is required")

    url = _build_url(
        "https://api.openweathermap.org/data/2.5/weather",
        {
            "lat": float(lat),
            "lon": float(lon),
            "appid": api_key,
            "units": "metric",
        },
    )
    data = _get_json(url, redact_values=(api_key,))
    if not isinstance(data, dict):
        raise WeatherApiError("OpenWeather current weather returned an unexpected response")

    weather = (data.get("weather") or [{}])[0]
    main = data.get("main") or {}
    return {
        "condition": weather.get("main") or "Unknown",
        "description": weather.get("description") or "",
        "temp_c": main.get("temp"),
        "humidity": main.get("humidity"),
        "city": data.get("name") or "",
        "country": (data.get("sys") or {}).get("country") or "",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fetch_forecast(lat: float, lon: float, api_key: str, cnt: int = 8) -> Dict[str, Any]:
    """Fetch normalized 5 day / 3 hour forecast slots by coordinate."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise WeatherApiError("Weather API key is required")

    url = _build_url(
        "https://api.openweathermap.org/data/2.5/forecast",
        {
            "lat": float(lat),
            "lon": float(lon),
            "appid": api_key,
            "units": "metric",
            "cnt": max(1, int(cnt)),
        },
    )
    data = _get_json(url, redact_values=(api_key,))
    if not isinstance(data, dict):
        raise WeatherApiError("OpenWeather forecast returned an unexpected response")

    slots = []
    for item in data.get("list") or []:
        weather = (item.get("weather") or [{}])[0]
        main = item.get("main") or {}
        slots.append({
            "ts": item.get("dt"),
            "time": item.get("dt_txt") or "",
            "condition": weather.get("main") or "Unknown",
            "description": weather.get("description") or "",
            "temp_c": main.get("temp"),
            "pop": item.get("pop"),
        })

    city = data.get("city") or {}
    return {
        "location": {
            "name": city.get("name") or "",
            "country": city.get("country") or "",
            "lat": (city.get("coord") or {}).get("lat"),
            "lon": (city.get("coord") or {}).get("lon"),
            "timezone": city.get("timezone"),
        },
        "forecast": slots,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
