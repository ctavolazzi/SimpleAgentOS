"""
local_news.py — Top local news stories via Google News RSS (free, no API key).

Part of the daily-note harness. Zero external dependencies — stdlib only.
"""

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

DEFAULT_QUERY = "Chico California"


def fetch(query: Optional[str] = None, limit: int = 3,
          hl: str = "en-US", gl: str = "US", ceid: str = "US:en",
          timeout: int = 10) -> dict:
    """
    Fetch top local news stories. Returns a dict with query, timestamp, and stories.

    Each story has: title, link, source, published (RFC 822 string).
    """
    q = query or DEFAULT_QUERY
    params = {"q": q, "hl": hl, "gl": gl, "ceid": ceid}
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "daily-note-harness/1.0"})

    with urllib.request.urlopen(req, timeout=timeout) as r:
        xml_text = r.read().decode("utf-8", errors="replace")

    root = ET.fromstring(xml_text)
    items = root.findall(".//item")[:limit]

    stories = []
    for item in items:
        src_el = item.find("source")
        stories.append({
            "title":     (item.findtext("title")   or "").strip(),
            "link":      (item.findtext("link")    or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
            "source":    src_el.text if src_el is not None else "Unknown",
        })

    return {
        "query":      q,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "count":      len(stories),
        "stories":    stories,
    }


def format_news_md(news: dict) -> str:
    """Format a news dict as a markdown list for the daily note."""
    if not news["stories"]:
        return f"*No stories returned for '{news['query']}' at {news['fetched_at']}*"

    lines = []
    for i, s in enumerate(news["stories"], 1):
        lines.append(f"{i}. [{s['title']}]({s['link']})")
        lines.append(f"   *{s['source']}* · {s['published']}")
    lines.append("")
    lines.append(f"*Pulled {news['fetched_at']} from Google News RSS (query: {news['query']!r})*")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or DEFAULT_QUERY
    n = fetch(q)
    print(format_news_md(n))
