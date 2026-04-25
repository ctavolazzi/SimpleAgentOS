"""
daily_image.py — Fetch the day's featured image using a rotating schedule.

Schedule (by weekday):
  Mon/Wed/Fri → Wikimedia POTD (same image Wikipedia features)
  Tue/Thu     → Bing Image of the Day (landscape photography)
  Sat/Sun     → Lorem Picsum seeded by date (always-available fallback)

Wikimedia and Bing both fall back to Picsum on error.
All fetches are cached by date via spin_up's shared cache — daily_image.fetch()
is only called once per day regardless of spin_up re-runs.
"""

import json
import re
import urllib.request
from datetime import datetime

TIMEOUT = 3  # hard cap — never hang the morning harness


# ── Wikimedia POTD ───────────────────────────────────────────────────────────


def _fetch_wikimedia() -> dict:
    dt = datetime.now()
    url = (
        f"https://en.wikipedia.org/api/rest_v1/feed/featured/"
        f"{dt.year}/{dt.month:02d}/{dt.day:02d}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "SimpleAgentOS/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read())

    potd = data.get("image", {})
    image_url = potd.get("image", {}).get("source") or potd.get("thumbnail", {}).get("source", "")
    if not image_url:
        raise ValueError("no image URL in Wikimedia featured feed")

    desc_html = potd.get("description", {}).get("html", "")
    caption = re.sub(r"<[^>]+>", "", desc_html).strip()[:200]
    file_page = potd.get("file_page", "")

    return {
        "url": image_url,
        "caption": caption,
        "source_url": file_page,
        "source": "Wikimedia POTD",
    }


# ── Bing Image of the Day ────────────────────────────────────────────────────


def _fetch_bing() -> dict:
    url = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=en-US"
    req = urllib.request.Request(url, headers={"User-Agent": "SimpleAgentOS/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read())

    images = data.get("images", [])
    if not images:
        raise ValueError("no images in Bing response")

    img = images[0]
    base_url = img.get("urlbase", "")
    if not base_url:
        raise ValueError("no urlbase in Bing image")

    image_url = f"https://www.bing.com{base_url}_1920x1080.jpg"
    caption = img.get("copyright", "").split(" (")[0].strip()[:200]

    return {
        "url": image_url,
        "caption": caption,
        "source_url": "https://www.bing.com",
        "source": "Bing IOTD",
    }


# ── Lorem Picsum fallback ────────────────────────────────────────────────────


def _picsum_fallback() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "url": f"https://picsum.photos/seed/{today}/1200/400",
        "caption": "",
        "source_url": "https://picsum.photos",
        "source": "Lorem Picsum",
    }


# ── Schedule ─────────────────────────────────────────────────────────────────


def _scheduled_source() -> str:
    """Return 'wikimedia', 'bing', or 'picsum' based on day of week."""
    weekday = datetime.now().weekday()  # 0=Mon … 6=Sun
    if weekday in (1, 3):    # Tue, Thu
        return "bing"
    if weekday in (5, 6):    # Sat, Sun
        return "picsum"
    return "wikimedia"       # Mon, Wed, Fri


# ── Public API ───────────────────────────────────────────────────────────────


def fetch() -> dict:
    """Return {url, caption, source_url, source} for today's image.

    Uses the rotation schedule; falls back to Picsum on any network error.
    """
    source = _scheduled_source()

    if source == "picsum":
        return _picsum_fallback()

    try:
        if source == "bing":
            return _fetch_bing()
        return _fetch_wikimedia()
    except Exception:
        try:
            # Cross-fallback: Bing day → try Wikimedia; Wikimedia day → try Bing
            if source == "bing":
                return _fetch_wikimedia()
            return _fetch_bing()
        except Exception:
            return _picsum_fallback()
