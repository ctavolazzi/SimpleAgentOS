"""
weather.py — Current conditions for a location via Open-Meteo (free, no API key).

Part of the daily-note harness. Zero external dependencies — stdlib urllib only.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

# Default is Chico, CA — owner's location. Override with fetch(lat, lon, name).
DEFAULT_LOCATION = {"name": "Chico, CA", "lat": 39.7285, "lon": -121.8375}

# WMO weather interpretation codes → (description, emoji).
# https://open-meteo.com/en/docs#weathervariables
WMO_CODES = {
    0:  ("Clear",              "☀️"),
    1:  ("Mainly clear",       "🌤"),
    2:  ("Partly cloudy",      "⛅"),
    3:  ("Overcast",           "☁️"),
    45: ("Fog",                "🌫"),
    48: ("Rime fog",           "🌫"),
    51: ("Light drizzle",      "🌦"),
    53: ("Drizzle",            "🌦"),
    55: ("Heavy drizzle",      "🌧"),
    61: ("Light rain",         "🌦"),
    63: ("Rain",               "🌧"),
    65: ("Heavy rain",         "🌧"),
    71: ("Light snow",         "🌨"),
    73: ("Snow",               "🌨"),
    75: ("Heavy snow",         "❄️"),
    77: ("Snow grains",        "🌨"),
    80: ("Rain showers",       "🌦"),
    81: ("Heavy showers",      "🌧"),
    82: ("Violent showers",    "⛈"),
    85: ("Snow showers",       "🌨"),
    86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm",       "⛈"),
    96: ("Thunder + hail",     "⛈"),
    99: ("Heavy thunder",      "⛈"),
}


def fetch(lat: Optional[float] = None, lon: Optional[float] = None,
          location_name: Optional[str] = None, timezone: str = "America/Los_Angeles",
          timeout: int = 10) -> dict:
    """
    Fetch current weather for a location. Returns a normalized dict.

    Raises on network/HTTP error — caller decides whether to fall back.
    """
    lat = lat if lat is not None else DEFAULT_LOCATION["lat"]
    lon = lon if lon is not None else DEFAULT_LOCATION["lon"]
    name = location_name or DEFAULT_LOCATION["name"]

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,precipitation,"
                   "weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,weather_code,sunrise,sunset",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": timezone,
        "forecast_days": 1,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "daily-note-harness/1.0"})

    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())

    return {
        "location":       name,
        "lat":            data.get("latitude", lat),
        "lon":            data.get("longitude", lon),
        "fetched_at":     datetime.now().isoformat(timespec="seconds"),
        "current_temp_f": data["current"]["temperature_2m"],
        "feels_like_f":   data["current"]["apparent_temperature"],
        "humidity_pct":   data["current"]["relative_humidity_2m"],
        "wind_mph":       data["current"]["wind_speed_10m"],
        "weather_code":   data["current"]["weather_code"],
        "high_f":         data["daily"]["temperature_2m_max"][0],
        "low_f":          data["daily"]["temperature_2m_min"][0],
        "precip_pct":     data["daily"]["precipitation_probability_max"][0],
        "sunrise":        data["daily"]["sunrise"][0],
        "sunset":         data["daily"]["sunset"][0],
    }


def describe(code: int) -> tuple:
    """Return (description, emoji) for a WMO weather code."""
    return WMO_CODES.get(code, ("Unknown", ""))


# Rendered in place of the weather line when the feed gave us nothing usable.
# Says so out loud: a blank line reads as "calm day", which is the same failure
# mode that let an empty arXiv feed pass as a real reading for eleven days.
UNAVAILABLE_MD = "⚠️ *Weather unavailable — the feed returned no data this run.*"


def _fmt(w: dict, key: str, spec: str = "{:.0f}") -> Optional[str]:
    """Format one field, or None if it is absent or not a number."""
    value = w.get(key)
    if value is None:
        return None
    try:
        return spec.format(value)
    except (TypeError, ValueError):
        return None


def format_weather_md(w: dict) -> str:
    """
    Format a weather dict as a markdown block for the daily note.

    Renders whatever subset of fields is present rather than assuming a complete
    payload. `spin_up._fetch_weather` returns `{}` by contract when the API times
    out and there is no cache to fall back on; on 2026-08-07 that empty dict hit
    an unguarded `w["weather_code"]` here and raised KeyError, which the location
    writer caught by discarding the whole section — address, air quality and
    local news included. A formatter renders what it has.
    """
    if not isinstance(w, dict) or not w:
        return UNAVAILABLE_MD

    parts = []

    if w.get("weather_code") is not None:
        desc, emoji = describe(w["weather_code"])
        parts.append(f"{emoji} **{desc}**".strip())

    temp, feels = _fmt(w, "current_temp_f"), _fmt(w, "feels_like_f")
    if temp is not None:
        parts.append(f"{temp}°F (feels {feels}°F)" if feels is not None else f"{temp}°F")

    high, low = _fmt(w, "high_f"), _fmt(w, "low_f")
    if high is not None and low is not None:
        parts.append(f"hi {high}° / lo {low}°")
    elif high is not None:
        parts.append(f"hi {high}°")
    elif low is not None:
        parts.append(f"lo {low}°")

    if w.get("precip_pct") is not None:
        parts.append(f"{w['precip_pct']}% precip")
    wind = _fmt(w, "wind_mph")
    if wind is not None:
        parts.append(f"wind {wind} mph")
    if w.get("humidity_pct") is not None:
        parts.append(f"humidity {w['humidity_pct']}%")

    # A payload carrying only a timestamp is not a reading.
    if not parts:
        return UNAVAILABLE_MD

    lines = [" · ".join(parts)]

    sun = []
    for key, icon in (("sunrise", "☀"), ("sunset", "🌙")):
        raw = w.get(key)
        if raw:
            sun.append(f"{icon} {key} {raw.split('T')[1] if 'T' in raw else raw}")
    if sun:
        lines.append(" · ".join(sun))

    fetched = w.get("fetched_at")
    lines.append(f"*Pulled {fetched} from Open-Meteo*" if fetched
                 else "*Partial reading from Open-Meteo — no fetch timestamp*")

    return "  \n".join(lines)


if __name__ == "__main__":
    w = fetch()
    print(format_weather_md(w))
