"""
air_quality.py — Current air quality via Open-Meteo Air Quality API (free, no key).

Part of the daily-note harness. Zero external dependencies — stdlib urllib only.
Chico summers mean wildfire smoke; AQI belongs next to the weather.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

DEFAULT_LOCATION = {"name": "Chico, CA", "lat": 39.7285, "lon": -121.8375}

# US AQI breakpoints → (label, emoji)
AQI_BANDS = [
    (50,  ("Good",                           "🟢")),
    (100, ("Moderate",                       "🟡")),
    (150, ("Unhealthy for sensitive groups", "🟠")),
    (200, ("Unhealthy",                      "🔴")),
    (300, ("Very unhealthy",                 "🟣")),
    (999, ("Hazardous",                      "🟤")),
]


def describe(aqi: float) -> tuple:
    """Return (label, emoji) for a US AQI value."""
    for ceiling, band in AQI_BANDS:
        if aqi <= ceiling:
            return band
    return ("Hazardous", "🟤")


def fetch(lat: Optional[float] = None, lon: Optional[float] = None,
          timeout: int = 10) -> dict:
    """
    Fetch current air quality. Returns a normalized dict.
    Raises on network/HTTP error — caller decides whether to fall back.
    """
    lat = lat if lat is not None else DEFAULT_LOCATION["lat"]
    lon = lon if lon is not None else DEFAULT_LOCATION["lon"]

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "us_aqi,pm2_5,pm10,ozone",
        "timezone": "America/Los_Angeles",
    }
    url = ("https://air-quality-api.open-meteo.com/v1/air-quality?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"User-Agent": "daily-note-harness/1.0"})

    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())

    cur = data["current"]
    return {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "us_aqi":     cur["us_aqi"],
        "pm2_5":      cur["pm2_5"],
        "pm10":       cur["pm10"],
        "ozone":      cur["ozone"],
    }


def format_md(aq: dict) -> str:
    """One-line markdown summary for the Location section."""
    label, emoji = describe(aq["us_aqi"])
    return (
        f"{emoji} **AQI {aq['us_aqi']:.0f}** ({label}) · "
        f"PM2.5 {aq['pm2_5']:.1f} · PM10 {aq['pm10']:.1f} · "
        f"O₃ {aq['ozone']:.0f} μg/m³"
    )


if __name__ == "__main__":
    print(format_md(fetch()))
