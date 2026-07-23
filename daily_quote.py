"""
daily_quote.py — Quote of the day via ZenQuotes (free, no key).

Part of the daily-note harness. Zero external dependencies — stdlib urllib only.
One quote per day, same for everyone, changes at midnight UTC.
"""

import json
import urllib.request
from datetime import datetime

API = "https://zenquotes.io/api/today"


def fetch(timeout: int = 10) -> dict:
    """
    Fetch today's quote. Returns {"quote", "author", "fetched_at"}.
    Raises on network/HTTP error or malformed payload.
    """
    req = urllib.request.Request(API, headers={"User-Agent": "daily-note-harness/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())

    if not isinstance(data, list) or not data or "q" not in data[0]:
        raise ValueError(f"unexpected ZenQuotes payload: {str(data)[:120]}")

    return {
        "quote":      data[0]["q"].strip(),
        "author":     data[0].get("a", "Unknown").strip(),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def format_md(q: dict) -> str:
    """Markdown block for the Daily Reading section."""
    if not q.get("quote"):
        return ""
    return f"**💬 Quote of the day:**\n> \"{q['quote']}\"\n> — *{q['author']}*"


if __name__ == "__main__":
    print(format_md(fetch()))
