#!/usr/bin/env python3
"""
arxiv.py — Daily arXiv digest for scientific author.

Pulls new preprints from arXiv in specified categories, formats as a daily
digest section. No API key needed; uses public REST API.

Categories of interest for mysteries of the universe:
  - astro-ph.CO: Cosmology and Nongalactic Astrophysics
  - astro-ph.GA: Astrophysics of Galaxies
  - gr-qc: General Relativity and Quantum Cosmology
  - hep-th: High Energy Physics - Theory
  - quant-ph: Quantum Physics
  - physics.gen-ph: General Physics

Usage:
  python3 arxiv.py                              # All defaults (today)
  python3 arxiv.py --categories astro-ph.CO gr-qc  # Specific categories
  python3 arxiv.py --days 3                     # Last N days
"""

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Optional, List
from xml.etree import ElementTree as ET

DEFAULT_CATEGORIES = [
    "astro-ph.CO",      # Cosmology
    "gr-qc",            # General Relativity
    "hep-th",           # High Energy Theory
    "quant-ph",         # Quantum Physics
    "astro-ph.GA",      # Galaxies
]


def fetch(categories: Optional[List[str]] = None, days: int = 1,
          max_results: int = 20, timeout: int = 15) -> dict:
    """
    Fetch new preprints from arXiv.

    Args:
        categories: List of arXiv category identifiers
        days: How many days back to search (1 = today)
        max_results: Max results per category
        timeout: HTTP timeout in seconds

    Returns:
        {
            "categories": [...],
            "fetched_at": ISO timestamp,
            "date_range": (start_date_str, end_date_str),
            "papers": [
                {"id": "2404.xxxx", "title": "...", "authors": [...],
                 "published": "2026-04-14", "summary": "...", "category": "..."}
            ]
        }
    """
    cats = categories or DEFAULT_CATEGORIES
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")

    all_papers = []
    for cat in cats:
        # arXiv API: returns Atom feed
        # https://arxiv.org/help/api/user-manual
        query = f"cat:{cat} AND submittedDate:[{start_date}000000 TO {end_date}235959]"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "daily-note-harness/1.0"})

        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                feed_xml = r.read().decode("utf-8")
            root = ET.fromstring(feed_xml)

            # Namespace handling for Atom
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns)

            for entry in entries:
                paper_id = entry.findtext("atom:id", namespaces=ns) or ""
                paper_id = paper_id.split("/abs/")[-1] if "/abs/" in paper_id else paper_id

                authors_el = entry.findall("atom:author", ns)
                authors = [a.findtext("atom:name", namespaces=ns) or "?" for a in authors_el]

                published = entry.findtext("atom:published", namespaces=ns) or ""
                pub_date = published[:10] if published else ""

                paper = {
                    "id": paper_id,
                    "title": (entry.findtext("atom:title", namespaces=ns) or "").strip(),
                    "authors": authors,
                    "published": pub_date,
                    "summary": (entry.findtext("atom:summary", namespaces=ns) or "").strip(),
                    "category": cat,
                    "url": f"https://arxiv.org/abs/{paper_id}",
                }
                all_papers.append(paper)
        except Exception as e:
            # Continue on failure for one category
            print(f"Warning: Failed to fetch {cat}: {e}", file=sys.stderr)

    # Deduplicate by ID, keep first (most recent due to sort order)
    seen = set()
    unique = []
    for p in all_papers:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)

    return {
        "categories": cats,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "date_range": (start_date, end_date),
        "count": len(unique),
        "papers": unique[:max_results],
    }


def format_digest_md(digest: dict) -> str:
    """Format arxiv digest as markdown for daily note."""
    if not digest["papers"]:
        return f"*No new papers in {', '.join(digest['categories'])} for {digest['date_range'][0]}.*"

    lines = []
    for paper in digest["papers"]:
        authors_str = ", ".join(paper["authors"][:2])
        if len(paper["authors"]) > 2:
            authors_str += ", et al."

        lines.append(f"**[{paper['id']}]({paper['url']})** — {paper['title']}")
        lines.append(f"*{authors_str}* ({paper['published']})")
        lines.append("")

    lines.append(f"*{digest['count']} papers from {', '.join(digest['categories'])} "
                 f"— pulled {digest['fetched_at']}*")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch daily arXiv digest.")
    parser.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES,
                        help="arXiv category codes")
    parser.add_argument("--days", type=int, default=1,
                        help="Days back to search")
    parser.add_argument("--max", type=int, default=20,
                        help="Max results per category")
    args = parser.parse_args()

    digest = fetch(args.categories, args.days, args.max)
    print(format_digest_md(digest))
