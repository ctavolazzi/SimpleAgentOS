#!/usr/bin/env python3
"""
blank_files.py — Blank and near-blank file detector for the vault.

Surfaces .md files that exist but have no meaningful content:
frontmatter-only, title-only, stub notes, or daily notes with
mostly-empty sections. Low priority — intended to run on /spin-up
or when explicitly called.

Usage:
  python3 blank_files.py                    # full vault scan
  python3 blank_files.py --daily            # daily notes with empty sections only
  python3 blank_files.py --stubs            # stub notes (minimal body)
  python3 blank_files.py --threshold 50     # bytes of body content to count as "filled"
  python3 blank_files.py --json             # machine-readable output
"""

import json
import re
import sys
from pathlib import Path
from typing import Optional

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
DAILY_NOTES_DIR = VAULT_DIR / "Daily Notes"

# Minimum non-whitespace body chars to consider a file "filled"
DEFAULT_THRESHOLD = 100

# Sections that matter in daily notes (agent-writable ones)
DAILY_SECTIONS_TO_CHECK = [
    ("## In the Lab",       "in_the_lab"),
    ("## Commits Today",    "commits_today"),
    ("## Tomorrow's Top 3", "tomorrows_top_3"),
    ("## Idea Dump",        "idea_dump"),
    ("## Sitrep",           "sitrep"),
]

# Lines that count as "boilerplate only" (not real content)
BOILERPLATE_PATTERNS = [
    r'^\s*$',                            # blank
    r'^---\s*$',                         # hr
    r'^#+ ',                             # headers themselves
    r'^>\s*\*[^*]+\*\s*$',              # italic instruction blockquotes
    r'^- \[ \]\s*$',                     # empty checkboxes
    r'^\*\*\w[\w\s]*:\*\*\s*$',         # **Label:** with no value
    r'^\*No .+ yet',                     # *No commits yet*
    r'^\*Scanned',                       # scan placeholders
]

_BP_RE = re.compile('|'.join(BOILERPLATE_PATTERNS))


def _extract_body(text: str) -> str:
    """Strip YAML frontmatter, return body."""
    m = re.match(r'^---\n.*?\n---\n?', text, re.DOTALL)
    return text[m.end():] if m else text


def _meaningful_chars(text: str) -> int:
    """Count non-boilerplate, non-whitespace chars."""
    lines = text.splitlines()
    real = [l for l in lines if not _BP_RE.match(l)]
    return sum(len(l.strip()) for l in real)


def _section_body(text: str, header: str) -> str:
    """Extract body of a markdown section (between header and next ## or EOF)."""
    level = len(header) - len(header.lstrip("#"))
    escaped = re.escape(header)
    pat = rf'^{escaped}\s*\n(.*?)(?=^#{{{1},{level}}} |\Z)'
    m = re.search(pat, text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def find_stub_notes(vault: Path, threshold: int = DEFAULT_THRESHOLD) -> list[dict]:
    """Files that exist but have < threshold meaningful chars in body."""
    stubs = []
    for f in vault.rglob("*.md"):
        # Skip daily notes — checked separately
        if f.parent == DAILY_NOTES_DIR:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        body = _extract_body(text)
        chars = _meaningful_chars(body)
        if chars < threshold:
            rel = str(f.relative_to(vault))
            stubs.append({
                "path": rel,
                "meaningful_chars": chars,
                "threshold": threshold,
                "suggestion": "Fill content or move to archive if no longer needed",
            })
    return stubs


def find_daily_empties(vault: Path) -> list[dict]:
    """Daily notes with sections that should be filled but aren't."""
    issues = []
    for f in sorted(DAILY_NOTES_DIR.glob("*.md"), reverse=True):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        empty_sections = []
        for header, slug in DAILY_SECTIONS_TO_CHECK:
            if header not in text:
                continue  # section absent, skip (not same as empty)
            body = _section_body(text, header)
            if _meaningful_chars(body) < 20:
                empty_sections.append(slug)
        if empty_sections:
            issues.append({
                "path": f"Daily Notes/{f.name}",
                "empty_sections": empty_sections,
                "suggestion": f"Fill: {', '.join(empty_sections)}",
            })
    return issues


def find_frontmatter_only(vault: Path) -> list[dict]:
    """Files that have frontmatter but zero body content."""
    fo = []
    for f in vault.rglob("*.md"):
        if f.parent == DAILY_NOTES_DIR:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not re.match(r'^---\n', text):
            continue  # no frontmatter, skip
        body = _extract_body(text).strip()
        if not body:
            rel = str(f.relative_to(vault))
            fo.append({
                "path": rel,
                "suggestion": "Add content or delete if placeholder",
            })
    return fo


def scan(vault: Path = VAULT_DIR, threshold: int = DEFAULT_THRESHOLD) -> dict:
    stubs = find_stub_notes(vault, threshold)
    daily_empties = find_daily_empties(vault)
    fm_only = find_frontmatter_only(vault)
    return {
        "vault": str(vault),
        "threshold": threshold,
        "stub_notes": stubs,
        "daily_empties": daily_empties,
        "frontmatter_only": fm_only,
        "summary": {
            "stub_notes": len(stubs),
            "daily_empties": len(daily_empties),
            "frontmatter_only": len(fm_only),
            "total": len(stubs) + len(daily_empties) + len(fm_only),
        },
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_results(results: dict):
    s = results["summary"]
    print(f"\nBlank Files Scan — {results['vault']}")
    print(f"{'─' * 60}")
    print(f"  {s['daily_empties']} daily empties  ·  "
          f"{s['frontmatter_only']} frontmatter-only  ·  "
          f"{s['stub_notes']} stubs (<{results['threshold']} chars)\n")

    if results["daily_empties"]:
        print("Daily notes with empty sections:")
        for d in results["daily_empties"]:
            print(f"  · {d['path']}")
            print(f"    → {d['suggestion']}")
        print()

    if results["frontmatter_only"]:
        print("Frontmatter-only files (no body):")
        for f in results["frontmatter_only"]:
            print(f"  · {f['path']}")
            print(f"    → {f['suggestion']}")
        print()

    if results["stub_notes"]:
        print(f"Stub notes (< {results['threshold']} meaningful chars):")
        for st in results["stub_notes"]:
            print(f"  · {st['path']}  [{st['meaningful_chars']} chars]")
            print(f"    → {st['suggestion']}")
        print()

    if s["total"] == 0:
        print("  ✓ No blank files found\n")
    else:
        print(f"  {s['total']} file(s) need attention\n")


def main():
    import argparse
    p = argparse.ArgumentParser(description="Blank file detector for vault.")
    p.add_argument("--daily",     action="store_true", help="Daily empties only")
    p.add_argument("--stubs",     action="store_true", help="Stub notes only")
    p.add_argument("--fm-only",   action="store_true", help="Frontmatter-only notes")
    p.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                   help=f"Min chars to count as filled (default: {DEFAULT_THRESHOLD})")
    p.add_argument("--vault",     default=str(VAULT_DIR), help="Vault path override")
    p.add_argument("--json",      action="store_true", help="JSON output")
    args = p.parse_args()

    vault = Path(args.vault)
    results = scan(vault, args.threshold)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    filter_mode = args.daily or args.stubs or args.fm_only
    if filter_mode:
        if not args.daily:
            results["daily_empties"] = []
        if not args.stubs:
            results["stub_notes"] = []
        if not args.fm_only:
            results["frontmatter_only"] = []

    _print_results(results)


if __name__ == "__main__":
    main()
