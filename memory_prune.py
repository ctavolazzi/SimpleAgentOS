#!/usr/bin/env python3
"""
memory_prune.py — Archive stale memory files from the Claude memory index.

Reads MEMORY.md, finds entries pointing to files that haven't been modified
in more than MAX_AGE_DAYS and aren't in the DO_NOT_PRUNE set, and moves them
to an ARCHIVE_DIR. Updates MEMORY.md index after archiving.

Usage:
    python3 memory_prune.py              # dry run (list candidates)
    python3 memory_prune.py --execute    # actually archive + update index
    python3 memory_prune.py --days 14    # lower age threshold
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

MEMORY_DIR = Path.home() / ".claude" / "projects" / "-Users-ctavolazzi" / "memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
ARCHIVE_DIR = MEMORY_DIR / "archive"
MAX_AGE_DAYS = 30

# Files that must never be pruned regardless of age
DO_NOT_PRUNE = {"MEMORY.md"}


def _parse_index() -> list[tuple[str, Path]]:
    """Return list of (label, path) from MEMORY.md link entries."""
    if not MEMORY_INDEX.exists():
        return []
    entries = []
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for line in MEMORY_INDEX.read_text(encoding="utf-8").splitlines():
        m = pattern.search(line)
        if m:
            label = m.group(1)
            rel = m.group(2)
            path = (MEMORY_DIR / rel).resolve()
            entries.append((label, path))
    return entries


def _candidates(max_age_days: int) -> list[tuple[str, Path, float]]:
    """Return (label, path, age_days) for stale memory files."""
    cutoff = datetime.now() - timedelta(days=max_age_days)
    result = []
    for label, path in _parse_index():
        if path.name in DO_NOT_PRUNE:
            continue
        if not path.exists():
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        age = (datetime.now() - mtime).days
        if mtime < cutoff:
            result.append((label, path, age))
    return sorted(result, key=lambda x: -x[2])  # oldest first


def _archive(path: Path) -> Path:
    """Move path into ARCHIVE_DIR, return new location."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / path.name
    # Avoid collision
    if dest.exists():
        stem = path.stem
        suffix = path.suffix
        dest = ARCHIVE_DIR / f"{stem}_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}"
    shutil.move(str(path), dest)
    return dest


def _remove_from_index(path: Path) -> None:
    """Remove the index line pointing to path from MEMORY.md."""
    if not MEMORY_INDEX.exists():
        return
    rel = path.name
    lines = MEMORY_INDEX.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = [l for l in lines if rel not in l]
    MEMORY_INDEX.write_text("".join(new_lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive stale Claude memory files.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually archive files. Default: dry run.")
    parser.add_argument("--days", type=int, default=MAX_AGE_DAYS,
                        help=f"Age threshold in days (default {MAX_AGE_DAYS}).")
    args = parser.parse_args()

    stale = _candidates(args.days)

    if not stale:
        print(f"No memory files older than {args.days} days. Nothing to prune.")
        return 0

    print(f"{'DRY RUN — ' if not args.execute else ''}Found {len(stale)} stale file(s):\n")
    for label, path, age in stale:
        print(f"  [{age:3d}d]  {path.name}  —  {label}")

    if not args.execute:
        print(f"\nRun with --execute to archive {len(stale)} file(s).")
        return 0

    archived = 0
    for label, path, age in stale:
        dest = _archive(path)
        _remove_from_index(path)
        print(f"  archived  {path.name}  →  archive/{dest.name}")
        archived += 1

    print(f"\nArchived {archived} file(s) to {ARCHIVE_DIR}")
    print(f"MEMORY.md index updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
