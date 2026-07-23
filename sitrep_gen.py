#!/usr/bin/env python3
"""
sitrep_gen.py — Generate daily sitrep from plan + hub + journal.

Reads (all optional — degrades gracefully if absent):
  Plans/YYYY-MM-DD_daily_plan.md   → incomplete tasks (top 3)
  Hubs/YYYY-MM-DD_hub.md           → active threads + states
  Claude Journal/YYYY-MM-DD.md     → session start time

Writes to:
  daily note sitrep section via daily_note.write_section
  OR stdout with --print

Usage:
  python3 sitrep_gen.py [--date YYYY-MM-DD] [--force] [--print] [--dry-run]
  python3 sitrep_gen.py --weather "71°F, Clear" --force
"""

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
HARNESS_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(HARNESS_DIR))


# ── Vault readers ─────────────────────────────────────────────────────────────

def _read_plan_tasks(date_str: str, limit: int = 3) -> list[str]:
    """Return incomplete checkbox items from today's plan (top N)."""
    plan_path = VAULT_DIR / "Plans" / f"{date_str}_daily_plan.md"
    if not plan_path.exists():
        return []
    text = plan_path.read_text(encoding="utf-8")
    tasks = re.findall(r'^[-*]\s+\[ \]\s+(.+)$', text, re.MULTILINE)
    return [t.strip() for t in tasks[:limit]]


def _read_hub_threads(date_str: str) -> list[tuple[str, str]]:
    """Extract active thread names + one-line states from today's hub.
    Returns list of (thread_label, state_snippet) tuples."""
    hub_path = VAULT_DIR / "Hubs" / f"{date_str}_hub.md"
    if not hub_path.exists():
        return []
    text = hub_path.read_text(encoding="utf-8")

    # Isolate the Active Threads section
    section_m = re.search(
        r'## Active Threads.*?\n(.*?)(?=\n## |\Z)',
        text, re.DOTALL | re.MULTILINE,
    )
    if not section_m:
        return []
    section = section_m.group(1)

    threads = []
    for heading_m in re.finditer(r'^### (.+)$', section, re.MULTILINE):
        raw_heading = heading_m.group(1).strip()
        # Strip wikilinks: [[path|label]] → label, [[path]] → path stem
        clean = re.sub(r'\[\[(?:[^\]|]+\|)?([^\]]+)\]\]', r'\1', raw_heading)
        # Strip trailing " — " link references
        clean = re.sub(r'\s*—\s*\S+$', '', clean).strip()

        # Find **State:** line within next N chars
        tail = section[heading_m.end():]
        state_m = re.search(r'\*\*State:\*\*\s*([^\n]+)', tail[:600])
        state = ""
        if state_m:
            raw_state = state_m.group(1).strip()
            # First sentence only, strip trailing link refs
            state = raw_state.split('.')[0].strip()
            state = re.sub(r'\[\[.*?\]\]', '', state).strip()

        # Skip placeholder threads
        if "populated" in (clean + state).lower() or "pending" in (clean + state).lower():
            if not state or state.lower() in ("", "to be populated as work begins"):
                continue

        threads.append((clean, state))

    return threads


def _read_journal_start(date_str: str) -> str:
    """Return session start line from today's journal, or empty string."""
    journal_path = VAULT_DIR / "Claude Journal" / f"{date_str}.md"
    if not journal_path.exists():
        return ""
    text = journal_path.read_text(encoding="utf-8")
    m = re.search(r'\*\*Session start:\*\*\s*([^\n]+)', text)
    return m.group(1).strip() if m else ""


# ── Sitrep builder ────────────────────────────────────────────────────────────

def generate(date_str: str, weather_str: str = "", music: Optional[dict] = None,
             stats_line: str = "") -> str:
    """Build sitrep markdown from vault sources. Pure function, no side effects.

    music: optional music_pick dict (title/video_id/author/vibe_label/iframe)
    stats_line: optional vault_stats.format_md() dashboard line
    """
    tasks   = _read_plan_tasks(date_str)
    threads = _read_hub_threads(date_str)
    session = _read_journal_start(date_str)

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day_label = dt.strftime("%A, %B %-d")
    except ValueError:
        day_label = date_str

    parts = [day_label]
    if weather_str:
        parts.append(weather_str)
    if session:
        parts.append(f"session {session}")
    status_line = " · ".join(parts)

    lines = ["---", "", f"**Status:** {status_line}", ""]

    # Active threads
    lines.append("**Active threads:**")
    if threads:
        for label, state in threads:
            if state:
                lines.append(f"- {label}: {state}")
            else:
                lines.append(f"- {label}")
    else:
        lines.append("- (none yet)")
    lines.append("")

    # Plan focus
    if tasks:
        lines.append("**Plan focus:**")
        for task in tasks:
            lines.append(f"- {task}")
        lines.append("")

    lines.append("**Blockers:** None.")

    if stats_line:
        lines.extend(["", f"**Dashboard:** {stats_line}"])

    if music:
        vibe_label = music.get("vibe_label", "focused work session")
        lines.extend([
            "",
            f"**Music:** [{music.get('title', '')}]"
            f"(https://www.youtube.com/watch?v={music.get('video_id', '')}) — "
            f"{music.get('author', 'Unknown')}",
            f"*Vibe: {vibe_label}*",
        ])
        if music.get("iframe"):
            lines.extend(["", music["iframe"]])

    lines.extend(["", "---"])
    return "\n".join(lines)


# ── Writer ────────────────────────────────────────────────────────────────────

def write_to_note(date_str: str, weather_str: str = "",
                  music: Optional[dict] = None, stats_line: str = "",
                  force: bool = False) -> tuple[bool, str]:
    """Write generated sitrep to today's daily note. Returns (ok, status)."""
    try:
        import daily_note
        status = daily_note.section_status(date_str if date_str else None)
        current = status.get("sitrep", "absent")

        if current == "filled" and not force:
            return True, "skipped (already filled — use --force to overwrite)"

        md = generate(date_str, weather_str, music=music, stats_line=stats_line)
        daily_note.write_section("sitrep", md, actor="claude",
                                 date=date_str if date_str else None)
        return True, f"written ({'updated' if current == 'filled' else 'new'})"
    except Exception as e:
        return False, f"failed ({type(e).__name__}): {e}"


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Generate sitrep from plan + hub + journal")
    p.add_argument("--date",    metavar="YYYY-MM-DD",
                   default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--weather", default="", metavar="STRING",
                   help="Weather blurb to embed in status line")
    p.add_argument("--force",   action="store_true",
                   help="Overwrite existing filled sitrep")
    p.add_argument("--print",   action="store_true", dest="print_only",
                   help="Print to stdout, don't write to note")
    p.add_argument("--dry-run", action="store_true",
                   help="Print generated content, skip write")
    args = p.parse_args()

    md = generate(args.date, args.weather)

    if args.print_only or args.dry_run:
        print(md)
        return 0

    ok, status = write_to_note(args.date, args.weather, args.force)
    icon = "✓" if ok else "✗"
    print(f"  {icon} sitrep: {status}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
