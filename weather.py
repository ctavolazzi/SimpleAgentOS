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


def format_weather_md(w: dict) -> str:
    """Format a weather dict as a markdown block for the daily note."""
    desc, emoji = describe(w["weather_code"])
    sunrise = w["sunrise"].split("T")[1] if "T" in w["sunrise"] else w["sunrise"]
    sunset  = w["sunset"].split("T")[1]  if "T" in w["sunset"]  else w["sunset"]
    return (
        f"{emoji} **{desc}** · "
        f"{w['current_temp_f']:.0f}°F (feels {w['feels_like_f']:.0f}°F) · "
        f"hi {w['high_f']:.0f}° / lo {w['low_f']:.0f}° · "
        f"{w['precip_pct']}% precip · wind {w['wind_mph']:.0f} mph · "
        f"humidity {w['humidity_pct']}%  \n"
        f"☀ sunrise {sunrise} · 🌙 sunset {sunset}  \n"
        f"*Pulled {w['fetched_at']} from Open-Meteo*"
    )


if __name__ == "__main__":
    w = fetch()
    print(format_weather_md(w))
