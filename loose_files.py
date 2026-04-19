#!/usr/bin/env python3
"""
loose_files.py — Vault loose file scanner.

Finds .md files that are orphaned (not referenced from anything),
misplaced (in wrong dir relative to type), or unlinked from daily notes.

Usage:
  python3 loose_files.py                    # full scan, today's vault
  python3 loose_files.py --orphans          # only orphaned files
  python3 loose_files.py --misplaced        # only misplaced files
  python3 loose_files.py --vault /path      # override vault dir
  python3 loose_files.py --json             # machine-readable output
"""

import json
import re
import sys
from pathlib import Path
from typing import Optional

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
DAILY_NOTES_DIR = VAULT_DIR / "Daily Notes"

# Dirs that are "organized" — files here are expected to be in a folder
ORGANIZED_DIRS = {
    "Daily Notes", "System", "active", "archived",
    "Attachments", "Templates", "People", "Projects",
}

# Patterns that indicate a file is intentionally in vault root
ROOT_OK_PATTERNS = [
    r"^\d{4}-\d{2}-\d{2}_",      # date-prefixed notes (journal, idea dumps)
    r"^README",
    r"^index",
    r"^00\.00_",
]


EXCLUDE_DIRS = {".obsidian", ".git", "node_modules", ".trash", ".archive", "Archive", "Backups"}


def _collect_all_md(vault: Path) -> list[Path]:
    results = []
    for f in vault.rglob("*.md"):
        if any(part in EXCLUDE_DIRS for part in f.parts):
            continue
        results.append(f)
    return sorted(results)


def _collect_all_links(vault: Path) -> set[str]:
    """Collect every wikilink target referenced across the vault."""
    links = set()
    for f in _collect_all_md(vault):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # [[Note Name]] and [[Note Name|alias]] patterns
        for m in re.finditer(r'\[\[([^\]|#]+)', text):
            target = m.group(1).strip()
            links.add(target)
            # Normalize: strip date prefix for matching
            links.add(target.lower())
    return links


def _stem_matches(path: Path, links: set[str]) -> bool:
    """True if any link could resolve to this file."""
    stem = path.stem
    return (
        stem in links
        or stem.lower() in links
        or str(path.relative_to(VAULT_DIR)).replace(".md", "") in links
    )


def _is_root_ok(path: Path) -> bool:
    """True if file is expected at vault root (date-prefix, README, etc.)."""
    name = path.name
    return any(re.match(pat, name) for pat in ROOT_OK_PATTERNS)


def find_orphans(vault: Path) -> list[dict]:
    """Files with no incoming wikilinks from anywhere in vault."""
    all_files = _collect_all_md(vault)
    links = _collect_all_links(vault)
    orphans = []
    for f in all_files:
        # Skip daily notes — they're entry points, not expected to be linked
        if f.parent == DAILY_NOTES_DIR:
            continue
        if not _stem_matches(f, links):
            rel = str(f.relative_to(vault))
            size = f.stat().st_size
            orphans.append({
                "path": rel,
                "size_bytes": size,
                "suggestion": "Link from daily note or index, or move to archive"
            })
    return orphans


def find_misplaced(vault: Path) -> list[dict]:
    """Files sitting at vault root that probably belong in a subdirectory."""
    misplaced = []
    for f in vault.glob("*.md"):  # root only, not recursive
        if _is_root_ok(f):
            continue
        # Guess where it belongs based on frontmatter type or name patterns
        suggestion = _suggest_dir(f)
        misplaced.append({
            "path": f.name,
            "suggestion": suggestion,
        })
    return misplaced


def find_unlinked_from_daily(vault: Path) -> list[dict]:
    """Date-prefixed notes that exist but aren't linked from their parent daily note."""
    unlinked = []
    for f in vault.glob("*.md"):
        # Must be date-prefixed (e.g. 2026-04-18_Claude_Journal.md)
        m = re.match(r'^(\d{4}-\d{2}-\d{2})_(.+)\.md$', f.name)
        if not m:
            continue
        date, slug = m.group(1), m.group(2)
        daily = DAILY_NOTES_DIR / f"{date}.md"
        if not daily.exists():
            continue
        daily_text = daily.read_text(encoding="utf-8", errors="ignore")
        stem = f.stem
        if f"[[{stem}]]" not in daily_text and f"[[{stem}|" not in daily_text:
            unlinked.append({
                "path": f.name,
                "parent_daily": f"{date}.md",
                "suggestion": f"Add [[{stem}]] to {date}.md (related or body)",
            })
    return unlinked


def _suggest_dir(f: Path) -> str:
    """Heuristic: guess target subdir from frontmatter or name."""
    try:
        text = f.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "move to appropriate subdir"
    fm_type = re.search(r'^type:\s*(\w+)', text, re.MULTILINE)
    if fm_type:
        t = fm_type.group(1)
        mapping = {
            "project": "Projects/",
            "person": "People/",
            "reference": "References/",
            "system": "System/",
            "template": "Templates/",
        }
        if t in mapping:
            return f"move to {mapping[t]}"
    return "move to appropriate subdir or archive"


def scan(vault: Path = VAULT_DIR) -> dict:
    orphans = find_orphans(vault)
    misplaced = find_misplaced(vault)
    unlinked = find_unlinked_from_daily(vault)
    return {
        "vault": str(vault),
        "orphans": orphans,
        "misplaced": misplaced,
        "unlinked_from_daily": unlinked,
        "summary": {
            "orphans": len(orphans),
            "misplaced": len(misplaced),
            "unlinked_from_daily": len(unlinked),
            "total_issues": len(orphans) + len(misplaced) + len(unlinked),
        }
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_results(results: dict):
    s = results["summary"]
    print(f"\nLoose Files Scan — {results['vault']}")
    print(f"{'─' * 60}")
    print(f"  {s['orphans']} orphans  ·  {s['misplaced']} misplaced  ·  "
          f"{s['unlinked_from_daily']} unlinked from daily\n")

    if results["orphans"]:
        print("Orphans (no incoming wikilinks):")
        for o in results["orphans"]:
            print(f"  · {o['path']}")
            print(f"    → {o['suggestion']}")
        print()

    if results["misplaced"]:
        print("Misplaced (unexpected in vault root):")
        for m in results["misplaced"]:
            print(f"  · {m['path']}")
            print(f"    → {m['suggestion']}")
        print()

    if results["unlinked_from_daily"]:
        print("Unlinked from parent daily note:")
        for u in results["unlinked_from_daily"]:
            print(f"  · {u['path']}")
            print(f"    → {u['suggestion']}")
        print()

    if s["total_issues"] == 0:
        print("  ✓ Vault tidy — no loose files found\n")


def main():
    import argparse
    p = argparse.ArgumentParser(description="Vault loose file scanner.")
    p.add_argument("--orphans",   action="store_true", help="Show orphans only")
    p.add_argument("--misplaced", action="store_true", help="Show misplaced only")
    p.add_argument("--unlinked",  action="store_true", help="Show unlinked-from-daily only")
    p.add_argument("--vault",     default=str(VAULT_DIR), help="Vault path override")
    p.add_argument("--json",      action="store_true", help="JSON output")
    args = p.parse_args()

    vault = Path(args.vault)
    results = scan(vault)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    filter_mode = args.orphans or args.misplaced or args.unlinked
    if filter_mode:
        if not args.orphans:
            results["orphans"] = []
        if not args.misplaced:
            results["misplaced"] = []
        if not args.unlinked:
            results["unlinked_from_daily"] = []

    _print_results(results)


if __name__ == "__main__":
    main()
