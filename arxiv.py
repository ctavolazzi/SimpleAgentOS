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

PHYSICS_CATEGORIES = DEFAULT_CATEGORIES
AI_CATEGORIES = [
    "cs.AI",            # Artificial Intelligence
    "cs.LG",            # Machine Learning
    "cs.CL",            # Computation and Language
    "cs.MA",            # Multi-Agent Systems
]

PHYSICS_KEYWORDS = {
    "cosmology": 3, "quantum gravity": 3, "dark matter": 3, "dark energy": 3,
    "inflation": 2, "black hole": 3, "holograph": 2, "cmb": 3,
    "cosmic microwave": 3, "entanglement": 2, "string theory": 2,
    "supergravity": 2, "gravitational wave": 2, "early universe": 2,
    "quantum information": 2, "decoherence": 1, "de sitter": 2,
    "ads/cft": 3, "holography": 2,
}

AI_KEYWORDS = {
    "agent": 3, "multi-agent": 4, "llm": 3, "language model": 3,
    "reasoning": 3, "tool use": 3, "tool-use": 3, "rag": 3, "retrieval": 2,
    "self-improv": 3, "self improv": 3, "alignment": 3, "rlhf": 3,
    "reinforcement learning": 2, "epistemic": 3,
    "cognitive architecture": 4, "autonomous": 2, "chain of thought": 2,
    "chain-of-thought": 2, "planning": 2, "in-context": 2,
    "fine-tuning": 1, "benchmark": 1, "emergent": 2, "scaling": 1,
    "safety": 2, "interpret": 2,
}

DEFAULT_TOP_N = 5


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


def score_paper(paper: dict, keywords: dict) -> tuple:
    """Score a paper by keyword matches in title + summary.

    Returns (score, matched_keywords_list). Case-insensitive substring match.
    """
    text = (paper.get("title", "") + " " + paper.get("summary", "")).lower()
    matches = []
    score = 0
    for kw, weight in keywords.items():
        if kw.lower() in text:
            score += weight
            matches.append(kw)
    return score, matches


def fetch_dual_pane(days: int = 2, top_n: int = DEFAULT_TOP_N,
                    per_cat: int = 30, timeout: int = 15) -> dict:
    """Fetch two panes: physics + AI/agents, scored by keyword relevance.

    Args:
        days: How many days back (2 default — covers weekends when no submits)
        top_n: Top N per pane after scoring
        per_cat: Max results per category before scoring
    """
    phys_raw = fetch(PHYSICS_CATEGORIES, days=days, max_results=per_cat, timeout=timeout)
    ai_raw = fetch(AI_CATEGORIES, days=days, max_results=per_cat, timeout=timeout)

    for p in phys_raw["papers"]:
        p["score"], p["matches"] = score_paper(p, PHYSICS_KEYWORDS)
    for p in ai_raw["papers"]:
        p["score"], p["matches"] = score_paper(p, AI_KEYWORDS)

    phys_sorted = sorted(phys_raw["papers"],
                         key=lambda x: (x["score"], x["published"]), reverse=True)
    ai_sorted = sorted(ai_raw["papers"],
                       key=lambda x: (x["score"], x["published"]), reverse=True)

    return {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "date_range": phys_raw["date_range"],
        "days": days,
        "physics": {
            "categories": PHYSICS_CATEGORIES,
            "papers": phys_sorted[:top_n],
            "total_fetched": len(phys_raw["papers"]),
        },
        "ai": {
            "categories": AI_CATEGORIES,
            "papers": ai_sorted[:top_n],
            "total_fetched": len(ai_raw["papers"]),
        },
    }


def format_dual_pane_md(digest: dict) -> str:
    """Format dual-pane digest as markdown with score annotations."""
    def fmt_paper(p):
        authors_str = ", ".join(p["authors"][:2])
        if len(p["authors"]) > 2:
            authors_str += ", et al."
        score = p.get("score", 0)
        matches = p.get("matches", [])
        match_str = f" · {', '.join(matches[:4])}" if matches else ""
        score_str = f"score {score}{match_str}" if score else "no keyword match"
        return (f"**[{p['id']}]({p['url']})** — {p['title']}  \n"
                f"*{authors_str}* ({p['published']} · {score_str})")

    lines = []
    lines.append("### Physics")
    phys = digest["physics"]["papers"]
    if phys:
        for p in phys:
            lines.append(fmt_paper(p))
            lines.append("")
    else:
        lines.append(f"*No papers in {', '.join(digest['physics']['categories'])} "
                     f"over last {digest.get('days', 1)}d.*")
        lines.append("")

    lines.append("### AI / Agents")
    ai = digest["ai"]["papers"]
    if ai:
        for p in ai:
            lines.append(fmt_paper(p))
            lines.append("")
    else:
        lines.append(f"*No papers in {', '.join(digest['ai']['categories'])} "
                     f"over last {digest.get('days', 1)}d.*")
        lines.append("")

    lines.append(f"*Scored from {digest['physics']['total_fetched']} physics "
                 f"+ {digest['ai']['total_fetched']} AI papers "
                 f"({digest['date_range'][0]}→{digest['date_range'][1]}) "
                 f"— pulled {digest['fetched_at']}*")
    return "\n".join(lines)


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
    parser.add_argument("--mode", choices=["dual", "single"], default="dual",
                        help="dual: physics + AI panes with scoring; single: legacy flat list")
    parser.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES,
                        help="arXiv category codes (single mode only)")
    parser.add_argument("--days", type=int, default=2,
                        help="Days back to search")
    parser.add_argument("--max", type=int, default=20,
                        help="Max results per category")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                        help="Top N per pane (dual mode)")
    args = parser.parse_args()

    if args.mode == "dual":
        digest = fetch_dual_pane(days=args.days, top_n=args.top)
        print(format_dual_pane_md(digest))
    else:
        digest = fetch(args.categories, args.days, args.max)
        print(format_digest_md(digest))
