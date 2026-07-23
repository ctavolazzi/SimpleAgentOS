"""
work_vibe.py — Derive a music vibe from yesterday's work context.

Reads yesterday's daily note (sitrep, in_the_lab, tomorrows_top_3, commits_today),
scores keyword categories, picks a matching music query, and annotates with a
one-line vibe description for today's expected work.

Zero external dependencies — stdlib only.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Vibe table ─────────────────────────────────────────────────────────────
# Each entry: (keywords, music_query, vibe_label)
# First category whose keywords score highest wins.
# Keywords matched against lowercased section text (substring).

_VIBE_TABLE = [
    (
        ["deploy", "wrangler", "cloudflare", "d1 execute", "migration", "resend",
         "secret put", "pages dev", "e2e", "npm run build"],
        "deep work electronic instrumental focus",
        "deploy day — focused execution energy",
    ),
    (
        ["mcp", "triage", "server", "fail", "warn", "root cause",
         "diagnostic", "debug", "fix", "broken", "error"],
        "dark electronic ambient focus instrumental",
        "debugging session — methodical dark ambient",
    ),
    (
        ["security", "audit", "vulnerability", "pentest", "compliance",
         "cve", "owasp", "auth", "injection"],
        "dark synthwave focus instrumental",
        "security work — sharp focus synthwave",
    ),
    (
        ["frontend", "css", "ui", "component", "page", "design",
         "tailwind", "svelte", "react", "html", "layout", "style"],
        "upbeat indie electronic creative flow",
        "UI/frontend — creative upbeat flow",
    ),
    (
        ["architecture", "refactor", "plan", "we ", "work effort",
         "prune", "cleanup", "reorganize", "structure"],
        "ambient piano instrumental thinking music",
        "planning/architecture — reflective ambient piano",
    ),
    (
        ["arxiv", "research", "paper", "reading", "physics", "ai agent",
         "llm", "model", "study", "literature"],
        "jazz cafe instrumental background music",
        "research day — relaxed jazz cafe",
    ),
    (
        ["doc", "write", "note", "log", "devlog", "readme", "changelog",
         "summarize", "recap"],
        "acoustic coffee shop jazz instrumental",
        "writing/docs — warm acoustic coffee shop",
    ),
    (
        ["commit", "git", "push", "merge", "pr", "branch",
         "changelog", "release", "version"],
        "lo-fi hip hop focus beats chill",
        "shipping commits — lo-fi chill focus",
    ),
]

_DEFAULT_QUERY = "Iranian jazz instrumental fusion"
_DEFAULT_VIBE  = "open session — Persian jazz warmth"


# ── Readers ────────────────────────────────────────────────────────────────

def _yesterday_date() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _read_sections(date: str) -> str:
    """Pull sitrep + in_the_lab + tomorrows_top_3 + commits_today from a note."""
    vault = Path.home() / "Documents" / "Personal-Remote-Vault" / "Daily Notes"
    path = vault / f"{date}.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    # Grab the four most signal-rich sections
    target_headers = {
        "## Sitrep", "## In the Lab",
        "## Tomorrow's Top 3", "## Commits Today",
    }
    chunks: list[str] = []
    current: list[str] = []
    capturing = False

    for line in text.splitlines():
        if line.startswith("## "):
            if capturing and current:
                chunks.append("\n".join(current))
            capturing = line.strip() in target_headers
            current = []
        elif capturing:
            current.append(line)

    if capturing and current:
        chunks.append("\n".join(current))

    return "\n".join(chunks)


# ── Scoring ────────────────────────────────────────────────────────────────

def _score(text: str) -> tuple[str, str]:
    """Return (music_query, vibe_label) for the dominant work category."""
    low = text.lower()
    best_score = 0
    best_query = _DEFAULT_QUERY
    best_vibe  = _DEFAULT_VIBE

    for keywords, query, vibe in _VIBE_TABLE:
        score = sum(1 for kw in keywords if kw in low)
        if score > best_score:
            best_score = score
            best_query = query
            best_vibe  = vibe

    return best_query, best_vibe


# ── Public API ─────────────────────────────────────────────────────────────

def derive(date: Optional[str] = None) -> dict:
    """
    Derive vibe from yesterday's note (or `date`'s note if given).

    Returns:
        {
          "music_query": str,      # YouTube search query
          "vibe_label":  str,      # one-liner for sitrep
          "source_date": str,      # which note was read
          "signal_text": str,      # raw text used for scoring (truncated)
        }
    """
    source = date or _yesterday_date()
    text = _read_sections(source)

    if not text.strip():
        return {
            "music_query": _DEFAULT_QUERY,
            "vibe_label":  _DEFAULT_VIBE,
            "source_date": source,
            "signal_text": "",
        }

    query, vibe = _score(text)
    return {
        "music_query": query,
        "vibe_label":  vibe,
        "source_date": source,
        "signal_text": text[:400],
    }


if __name__ == "__main__":
    result = derive()
    print(f"Source note : {result['source_date']}")
    print(f"Vibe        : {result['vibe_label']}")
    print(f"Music query : {result['music_query']}")
    print()
    print("Signal text (first 400 chars):")
    print(result["signal_text"])
