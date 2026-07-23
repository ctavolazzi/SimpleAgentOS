"""
on_this_day.py — Historical events for today's date via Wikimedia (free, no key).

Part of the daily-note harness. Zero external dependencies — stdlib urllib only.
Feeds the Daily Reading section: a little historical texture with the coffee.
"""

import json
import random
import urllib.request
from datetime import datetime
from typing import Optional

API = "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/{mm}/{dd}"


def fetch(date: Optional[str] = None, top_n: int = 3, timeout: int = 10) -> dict:
    """
    Fetch curated "on this day" events for a date (YYYY-MM-DD, default today).

    Returns {"date", "fetched_at", "events": [{"year", "text", "url"}]}.
    Selection is seeded by the date so re-runs on the same day pick the same
    events. Raises on network/HTTP error — caller decides whether to fall back.
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    dt = datetime.strptime(date, "%Y-%m-%d")

    url = API.format(mm=f"{dt.month:02d}", dd=f"{dt.day:02d}")
    req = urllib.request.Request(url, headers={"User-Agent": "daily-note-harness/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())

    pool = data.get("selected", [])
    rng = random.Random(date)  # same picks all day, fresh picks tomorrow
    picks = rng.sample(pool, min(top_n, len(pool))) if pool else []
    picks.sort(key=lambda e: e.get("year", 0))

    events = []
    for e in picks:
        page_url = ""
        for page in e.get("pages", []):
            page_url = page.get("content_urls", {}).get("desktop", {}).get("page", "")
            if page_url:
                break
        events.append({
            "year": e.get("year"),
            "text": e.get("text", "").strip(),
            "url":  page_url,
        })

    return {
        "date": date,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "events": events,
    }


def format_md(otd: dict) -> str:
    """Markdown block for the Daily Reading section."""
    events = otd.get("events", [])
    if not events:
        return ""
    lines = ["**📜 On this day:**"]
    for e in events:
        link = f" ([more]({e['url']}))" if e.get("url") else ""
        lines.append(f"- **{e['year']}** — {e['text']}{link}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_md(fetch()))
