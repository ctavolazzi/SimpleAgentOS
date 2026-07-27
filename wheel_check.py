#!/usr/bin/env python3
"""
wheel_check.py — Daily Note wagonwheel integrity checker.

Codifies the exhaustive, line-by-line verification that a day's vault state is
fully wired: the daily note, its hub, journal, plan, and every spoke form a
connected wheel with no dangling links, no unfilled containers, and no orphans.

This is the guard against the 2026-07-10 failure mode, where a full day of work
lived only in the daily note while the Hub and Journal sat as spin-up
placeholders and several spokes had no back-link to the hub.

Checks (per date, default today):
  1. FRONTMATTER   — daily note has all required fields; every link field
                     resolves to a real file (delegates to frontmatter.validate).
  2. CONTAINERS    — Hub and Journal exist and are filled (no surviving spin-up
                     placeholder text; Journal has a non-empty ## Notes).
  3. RECIPROCITY   — every spoke listed in the hub's `spokes:` exists and carries
                     a `hub:` back-link to today's hub (closed rim).
  4. PARENT CHAIN  — walking `parent:` from the daily note reaches the vault
                     index without a broken hop.
  5. ORPHANS       — vault .md files modified on this date that have zero inbound
                     wikilinks (excluding known entry/container files).

Severity: ERROR = wheel is broken (fix required). WARN = worth a look.
CLI exits 1 if any ERROR is present, 0 otherwise — safe for fail-loud use.

Usage:
  python3 wheel_check.py                 # check today
  python3 wheel_check.py --date 2026-07-10
  python3 wheel_check.py --json          # machine-readable
  python3 wheel_check.py --warn-as-error # treat WARN as failure too
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import daily_note
import frontmatter as fm

# Text spin-up leaves in Hub/Journal scaffolds. Survival to check-time means the
# container was never filled during the session.
PLACEHOLDER_MARKERS = (
    "To be populated",
    "(none yet)",
    "(pending)",
    "(current model)",
    "(check on startup)",
)

# Any one of these carrying real text means the journal was actually written
# in. The first is spin_up's minimal template; the rest come from
# claude_journal.py's own _TEMPLATE, which the `add-*` verbs append to.
JOURNAL_CONTENT_SECTIONS = (
    "## Notes",
    "## Session Recap",
    "## Realizations",
    "## Open Questions",
    "## What I Find Interesting",
    "## Threads I'm Holding",
)


def _journal_has_content(text: str) -> bool:
    """True if any journal content section holds more than its own scaffolding.

    Callouts (`> [!abstract]+ ...`), italic prompts and HTML comments are what
    the template ships with, so they do not count as having been written in.
    """
    for heading in JOURNAL_CONTENT_SECTIONS:
        body = daily_note._extract_section(text, heading)
        for line in body.splitlines():
            line = line.strip()
            if not line or line == "---":
                continue
            if line.startswith(("<!--", ">", "*")):
                continue
            return True
    return False

# Files that legitimately have few/no inbound links (entry points + containers).
def _entry_basenames(date_str: str) -> set:
    return {
        date_str,                    # daily note + journal share this stem
        f"{date_str}_hub",
        f"{date_str}_daily_plan",
        f"{date_str}_Idea_Dump",     # linked from daily note body, sometimes only there
        "00.00_vault_index",
    }


class Result:
    """Accumulates findings by severity."""
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.oks: list[str] = []

    def err(self, msg): self.errors.append(msg)
    def warn(self, msg): self.warnings.append(msg)
    def ok(self, msg): self.oks.append(msg)

    @property
    def broken(self) -> bool:
        return bool(self.errors)


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _hub_spokes(hub_text: str) -> list[str]:
    """Extract wikilink targets from a hub's `spokes:` frontmatter list."""
    fmatch = re.search(r'^---\n(.*?)\n---', hub_text, re.DOTALL)
    if not fmatch:
        return []
    block = fmatch.group(1)
    sp = re.search(r'^spokes:\s*\n((?:\s*-\s*.*\n?)*)', block, re.MULTILINE)
    if not sp:
        return []
    return re.findall(r'\[\[([^\]|#]+)', sp.group(1))


def _resolve(target: str) -> Optional[Path]:
    """Resolve a wikilink target (pathed or bare) to a real vault file."""
    vault = daily_note.VAULT_DIR
    target = target.strip()
    for cand in (vault / target, vault / f"{target}.md"):
        if cand.is_file():
            return cand
    base = Path(target).name
    for p in vault.rglob(f"{base}.md"):
        if ".obsidian" not in p.parts and ".trash" not in p.parts:
            return p
    return None


def _fm_field(text: str, field: str) -> Optional[str]:
    m = re.search(rf'^{re.escape(field)}:\s*(.+)$',
                  text.split('---')[1] if '---' in text else text, re.MULTILINE)
    return m.group(1).strip().strip('"').strip("'") if m else None


# ── Individual checks ────────────────────────────────────────────────────────

def check_frontmatter(date_str: str, r: Result):
    if not daily_note.exists(date_str):
        r.err(f"FRONTMATTER: no daily note for {date_str}")
        return
    issues = fm.validate(date_str)
    if issues:
        for iss in issues:
            r.err(f"FRONTMATTER: {iss}")
    else:
        r.ok("FRONTMATTER: all required fields present, all links resolve")


def check_containers(date_str: str, r: Result):
    vault = daily_note.VAULT_DIR
    targets = {
        "Hub":     vault / "Hubs" / f"{date_str}_hub.md",
        "Journal": vault / "Claude Journal" / f"{date_str}.md",
    }
    for label, path in targets.items():
        text = _read(path)
        if text is None:
            r.err(f"CONTAINER: {label} missing ({path.name})")
            continue
        hits = sorted({m for m in PLACEHOLDER_MARKERS if m in text})
        if label == "Journal":
            # Two templates produce journals. spin_up writes a minimal one with
            # a single "## Notes" section; claude_journal.py's own _TEMPLATE
            # writes "## Realizations", "## Open Questions" and friends and has
            # no "## Notes" at all. Requiring one specific heading meant a
            # journal created by the canonical generator could never pass, and
            # a journal that passed could not accept `claude_journal add-*`.
            # Accept either shape: what matters is that something was written.
            if not _journal_has_content(text):
                hits = sorted(set(hits) | {"no journal content"})
        if hits:
            r.err(f"CONTAINER: {label} still has placeholders: {', '.join(hits)}")
        else:
            r.ok(f"CONTAINER: {label} filled")


def check_reciprocity(date_str: str, r: Result):
    vault = daily_note.VAULT_DIR
    hub_path = vault / "Hubs" / f"{date_str}_hub.md"
    hub_text = _read(hub_path)
    if hub_text is None:
        r.err("RECIPROCITY: hub missing — cannot check spokes")
        return
    spokes = _hub_spokes(hub_text)
    if not spokes:
        r.warn("RECIPROCITY: hub lists no spokes")
        return
    for spoke in spokes:
        spath = _resolve(spoke)
        if spath is None:
            r.err(f"RECIPROCITY: spoke '{spoke}' listed in hub but file not found")
            continue
        stext = _read(spath) or ""
        hub_field = _fm_field(stext, "hub")
        if not hub_field or f"{date_str}_hub" not in hub_field:
            r.err(f"RECIPROCITY: spoke '{spoke}' has no hub back-link to {date_str}_hub")
        else:
            r.ok(f"RECIPROCITY: spoke '{spoke}' ↔ hub")


def check_parent_chain(date_str: str, r: Result):
    """Walk parent: from the daily note up to the vault index."""
    seen = set()
    text = _read(daily_note.daily_path(date_str))
    hop = daily_note.daily_path(date_str)
    depth = 0
    while text is not None and depth < 12:
        parent = _fm_field(text, "parent")
        if not parent:
            r.err(f"PARENT-CHAIN: {hop.name} has no parent field — chain breaks")
            return
        m = re.search(r'\[\[([^\]|#]+)', parent)
        if not m:
            r.err(f"PARENT-CHAIN: {hop.name} parent is not a wikilink: {parent!r}")
            return
        target = m.group(1)
        if "00.00_vault_index" in target:
            r.ok("PARENT-CHAIN: reaches vault index")
            return
        nxt = _resolve(target)
        if nxt is None:
            r.err(f"PARENT-CHAIN: {hop.name} → {target} (dangling)")
            return
        if nxt in seen:
            r.err(f"PARENT-CHAIN: cycle at {nxt.name}")
            return
        seen.add(nxt)
        hop, text, depth = nxt, _read(nxt), depth + 1
    r.warn("PARENT-CHAIN: did not reach vault index within 12 hops")


def check_orphans(date_str: str, r: Result):
    """Vault .md files modified on date_str with zero inbound wikilinks."""
    vault = daily_note.VAULT_DIR
    entry = _entry_basenames(date_str)
    # Gather files modified on the target date
    try:
        start = datetime.strptime(date_str, "%Y-%m-%d").timestamp()
        end = start + 86400
    except ValueError:
        return
    modified = []
    for p in vault.rglob("*.md"):
        if ".obsidian" in p.parts or ".trash" in p.parts:
            continue
        try:
            if start <= p.stat().st_mtime < end:
                modified.append(p)
        except OSError:
            continue
    if not modified:
        return
    # Build a single corpus of all wikilinks in the vault for inbound counting
    for p in modified:
        base = p.stem
        if base in entry:
            r.ok(f"ORPHAN: {base} (entry/container — exempt)")
            continue
        found = False
        for q in vault.rglob("*.md"):
            if q == p or ".obsidian" in q.parts or ".trash" in q.parts:
                continue
            qt = _read(q) or ""
            if re.search(rf'\[\[[^\]]*{re.escape(base)}', qt):
                found = True
                break
        if found:
            r.ok(f"ORPHAN: {base} has inbound links")
        else:
            r.warn(f"ORPHAN: {base} modified {date_str} but has 0 inbound wikilinks")


# ── Orchestration ────────────────────────────────────────────────────────────

def check(date_str: Optional[str] = None) -> Result:
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    r = Result()
    check_frontmatter(date_str, r)
    check_containers(date_str, r)
    check_reciprocity(date_str, r)
    check_parent_chain(date_str, r)
    check_orphans(date_str, r)
    return r


def main():
    import argparse
    p = argparse.ArgumentParser(description="Daily Note wagonwheel integrity checker.")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="Target date (default: today)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.add_argument("--warn-as-error", action="store_true",
                   help="Exit 1 on warnings too")
    p.add_argument("--quiet", action="store_true", help="Only print failures")
    args = p.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    r = check(date_str)

    if args.json:
        print(json.dumps({
            "date": date_str,
            "errors": r.errors,
            "warnings": r.warnings,
            "ok": r.oks,
            "broken": r.broken,
        }, indent=2))
    else:
        print(f"\n🛞 Wheel integrity — {date_str}\n" + "─" * 52)
        if not args.quiet:
            for m in r.oks:
                print(f"  ✓ {m}")
        for m in r.warnings:
            print(f"  ⚠ {m}")
        for m in r.errors:
            print(f"  ✗ {m}")
        print("─" * 52)
        if r.broken:
            print(f"  ✗ WHEEL BROKEN — {len(r.errors)} error(s), {len(r.warnings)} warning(s)\n")
        elif r.warnings:
            print(f"  ⚠ wheel intact — {len(r.warnings)} warning(s)\n")
        else:
            print("  ✓ WHEEL INTACT — fully wired, no dangling links, no orphans\n")

    fail = r.broken or (args.warn_as_error and r.warnings)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
