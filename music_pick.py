"""
music_pick.py — Pick and verify a YouTube embed for a music query.

Strategy: search YouTube HTML → parse videoIds → verify each via the public
oembed endpoint → return the first one that resolves. That way we never embed
a dead/private/removed video.

Zero external dependencies — stdlib only.
"""

import json
import re
import urllib.parse
import urllib.request
from typing import Optional

DEFAULT_QUERY = "Iranian jazz music"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) daily-note-harness/1.0"
)


def search(query: Optional[str] = None, max_candidates: int = 10,
           timeout: int = 10) -> list:
    """Scrape YouTube search results for unique videoIds. Order preserved."""
    q = query or DEFAULT_QUERY
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": q})
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", errors="ignore")

    seen = []
    for m in re.finditer(r'"videoId":"([a-zA-Z0-9_-]{11})"', html):
        vid = m.group(1)
        if vid not in seen:
            seen.append(vid)
        if len(seen) >= max_candidates:
            break
    return seen


def verify(video_id: str, timeout: int = 5) -> Optional[dict]:
    """
    Verify a video is publicly embeddable via YouTube's oembed endpoint.
    Returns {video_id, title, author, thumbnail, iframe} or None on failure.
    """
    url = (f"https://www.youtube.com/oembed?"
           f"url=https://www.youtube.com/watch?v={video_id}&format=json")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return None

    return {
        "video_id":  video_id,
        "title":     data.get("title", "").strip(),
        "author":    data.get("author_name", "").strip(),
        "thumbnail": data.get("thumbnail_url", ""),
        "iframe":    (f'<iframe width="560" height="315" '
                      f'src="https://www.youtube.com/embed/{video_id}" '
                      f'frameborder="0" allowfullscreen></iframe>'),
    }


def pick(query: Optional[str] = None, max_candidates: int = 10) -> Optional[dict]:
    """Search → verify loop. Returns first verified result or None."""
    for vid in search(query, max_candidates=max_candidates):
        result = verify(vid)
        if result:
            return result
    return None


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or DEFAULT_QUERY
    r = pick(q)
    if r:
        print(f"{r['title']}")
        print(f"  by {r['author']}")
        print(f"  https://www.youtube.com/watch?v={r['video_id']}")
        print()
        print(r["iframe"])
    else:
        print(f"No verified video found for query: {q!r}")
