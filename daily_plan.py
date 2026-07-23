"""
daily_plan.py — Daily Plan lifecycle module.

A daily plan is a single vault doc (Plans/YYYY-MM-DD_daily_plan.md) that acts
as a living container for ALL intellectual labor in a session. It is:
  - Created at spin-up (seeded from yesterday's rollover)
  - Written to throughout the day (threads, thoughts, satellites)
  - Locked at wrap-up (immutable after EOD)
  - Read-only source for the next day's rollover

Locking: sets frontmatter status=locked + locked_at=ISO, plus optional chmod 444.
Override: use unlock(date) with explicit intent — creates audit trail.

Plan file lives at: VAULT_ROOT/Plans/YYYY-MM-DD_daily_plan.md
Daily note frontmatter gets: plan + plan_status fields

Usage (CLI):
    python3 daily_plan.py create [--date YYYY-MM-DD]
    python3 daily_plan.py lock [--date YYYY-MM-DD]
    python3 daily_plan.py unlock --date YYYY-MM-DD --reason "..."
    python3 daily_plan.py status [--date YYYY-MM-DD]
    python3 daily_plan.py add-thought "..." [--date YYYY-MM-DD]
    python3 daily_plan.py add-satellite "..." [--url URL] [--date YYYY-MM-DD]

Python API:
    from daily_plan import create_today, lock, get_plan, add_thought, add_satellite
"""

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import atomic_io

# ── Config ────────────────────────────────────────────────────────────────────

VAULT_ROOT = Path("/Users/ctavolazzi/Documents/Personal-Remote-Vault")
PLANS_DIR = VAULT_ROOT / "Plans"
DAILY_NOTES_DIR = VAULT_ROOT / "Daily Notes"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _plan_path(d: Optional[str] = None) -> Path:
    if d is None:
        d = date.today().isoformat()
    return PLANS_DIR / f"{d}_daily_plan.md"


def _today() -> str:
    return date.today().isoformat()


def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _day_label(d: str) -> str:
    """'2026-04-27' → 'Monday, April 27th 2026'"""
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        day = dt.day
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10 if day not in (11, 12, 13) else 0, "th")
        return dt.strftime(f"%A, %B {day}{suffix} %Y")
    except ValueError:
        return d


# ── Frontmatter helpers (no ruamel dependency) ────────────────────────────────

def _read_frontmatter_raw(text: str) -> tuple[dict, str]:
    """Extract frontmatter into a simple dict + the body text."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 5:] if text[end + 4:end + 5] == "\n" else text[end + 4:]
    fm: dict = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"')
    return fm, body


def _update_frontmatter(path: Path, updates: dict) -> None:
    """Merge key-value pairs into a file's YAML frontmatter. Simple regex approach."""
    try:
        import yaml_io
        text = path.read_text(encoding="utf-8")
        new_text = yaml_io.update_fields(text, updates)
        atomic_io.vault_write(path, new_text)
        return
    except Exception:
        pass
    # Fallback: naive line replacement + append
    text = path.read_text(encoding="utf-8")
    for key, val in updates.items():
        # Try to replace existing key
        pattern = re.compile(rf"^({re.escape(key)}:).*$", re.M)
        val_str = f'"{val}"' if isinstance(val, str) and " " in val else str(val)
        if pattern.search(text):
            text = pattern.sub(rf"\1 {val_str}", text)
        else:
            # Insert before closing ---
            text = text.replace("\n---\n", f"\n{key}: {val_str}\n---\n", 1)
    atomic_io.vault_write(path, text)


def _update_daily_note_frontmatter(d: str, updates: dict) -> bool:
    """Update the daily note's frontmatter with plan-related fields."""
    note_path = DAILY_NOTES_DIR / f"{d}.md"
    if not note_path.exists():
        return False
    try:
        _update_frontmatter(note_path, updates)
        return True
    except Exception:
        return False


# ── Plan template ─────────────────────────────────────────────────────────────

_PLAN_TEMPLATE = """\
---
type: daily_plan
date: {date}
parent: "[[Daily Notes/{date}]]"
status: active
locked_at: "null"
rolled_over_from: "[[Plans/{rolled_over_from}_daily_plan]]"
threads_total: 0
threads_completed: 0
tags:
  - daily_plan
---

# Daily Plan — {day_label}

> **Living document.** Active TODAY ONLY. Locked at EOD by wrap_up.py.
> After lock: read-only source for tomorrow's rollover.
> Purpose: capture every thread, thought, and satellite — nothing lost.

---

## Active Threads

{threads_section}

---

## Completed

<!-- Move items here when done — strike-through + [x] -->

---

## Preserved Thoughts

<!-- Insights, half-formed ideas, observations. One line each. Nothing too small. -->

---

## Satellites Spun Off

<!-- New files, repos, systems, or plans this day's work spawned. -->

---

## Rollover Report

> Auto-populated at lock time by wrap_up.py. Do not edit manually.

**Status:** Active — not yet locked.
"""

_THREADS_FROM_ROLLOVER = """\
<!-- Seeded from {source_date} rollover -->
{items}"""

_BLANK_THREADS = """\
<!-- Add threads below. Each thread = one unit of focused work. -->

### T1: [Thread Title]
- [ ]
"""


# ── Core API ──────────────────────────────────────────────────────────────────

def get_plan(d: Optional[str] = None) -> dict:
    """Return plan metadata for a given date."""
    if d is None:
        d = _today()
    path = _plan_path(d)
    if not path.exists():
        return {"date": d, "exists": False, "status": "missing", "path": str(path)}
    text = path.read_text(encoding="utf-8")
    fm, _ = _read_frontmatter_raw(text)
    return {
        "date": d,
        "exists": True,
        "status": fm.get("status", "unknown"),
        "locked_at": fm.get("locked_at", "null"),
        "rolled_over_from": fm.get("rolled_over_from", ""),
        "threads_total": fm.get("threads_total", "0"),
        "threads_completed": fm.get("threads_completed", "0"),
        "path": str(path),
    }


def is_locked(d: Optional[str] = None) -> bool:
    return get_plan(d).get("status") == "locked"


def create_today(rollover_items: Optional[list[str]] = None,
                 source_date: Optional[str] = None,
                 force: bool = False) -> dict:
    """
    Create today's plan file. Idempotent unless force=True.
    Optionally seeds Active Threads from rollover_items list.
    Returns {created, path, status}.
    """
    d = _today()
    path = _plan_path(d)
    PLANS_DIR.mkdir(parents=True, exist_ok=True)

    if path.exists() and not force:
        return {"created": False, "path": str(path), "status": "exists"}

    if rollover_items:
        items_md = "\n".join(f"- [ ] {item}" for item in rollover_items)
        src = source_date or _yesterday()
        threads_section = _THREADS_FROM_ROLLOVER.format(
            source_date=src, items=items_md
        )
    else:
        threads_section = _BLANK_THREADS

    content = _PLAN_TEMPLATE.format(
        date=d,
        day_label=_day_label(d),
        rolled_over_from=source_date or _yesterday(),
        threads_section=threads_section,
    )

    atomic_io.vault_write(path, content)

    # Update daily note frontmatter
    _update_daily_note_frontmatter(d, {
        "plan": f"Plans/{d}_daily_plan",
        "plan_status": "active",
    })

    return {"created": True, "path": str(path), "status": "active"}


def lock(d: Optional[str] = None, hard: bool = False) -> dict:
    """
    Lock today's (or given date's) plan.
    - Writes status=locked + locked_at to frontmatter
    - hard=True: also chmod 444 (filesystem read-only)
    Returns {ok, path, locked_at, rollover_count}.
    """
    if d is None:
        d = _today()
    path = _plan_path(d)

    if not path.exists():
        return {"ok": False, "error": "plan not found", "path": str(path)}
    if is_locked(d):
        return {"ok": False, "error": "already locked", "path": str(path)}

    now = _iso_now()
    rollover = extract_rollover(d)

    # Build rollover report section
    rollover_count = len(rollover.get("incomplete_items", []))
    report_lines = [
        f"**Locked at:** {now}",
        f"**Incomplete threads:** {rollover_count}",
    ]
    if rollover_count:
        report_lines.append(f"**Carried to:** [[Plans/{d}_daily_plan|{_day_label(d)} (today)]]")
        report_lines.append("")
        report_lines.append("**Items carried forward:**")
        for item in rollover.get("incomplete_items", []):
            report_lines.append(f"- {item}")

    # Replace Rollover Report section content
    text = path.read_text(encoding="utf-8")
    rollover_header = "## Rollover Report"
    if rollover_header in text:
        new_report_body = "\n".join(report_lines)
        # Replace from the header through the end of the file (it's the last section)
        pattern = re.compile(
            rf"({re.escape(rollover_header)}\n).*",
            re.S,
        )
        text = pattern.sub(rf"\g<1>\n{new_report_body}\n", text)
        atomic_io.vault_write(path, text)

    # Update frontmatter
    _update_frontmatter(path, {"status": "locked", "locked_at": now})

    # Update daily note
    _update_daily_note_frontmatter(d, {"plan_status": "locked"})

    if hard:
        os.chmod(path, 0o444)

    return {
        "ok": True,
        "path": str(path),
        "locked_at": now,
        "rollover_count": rollover_count,
        "rollover_items": rollover.get("incomplete_items", []),
    }


def unlock(d: str, reason: str = "") -> dict:
    """
    Unlock a locked plan (override / audit trail).
    Writes an unlock event comment into the plan + restores status=active.
    Removes chmod 444 if set.
    """
    path = _plan_path(d)
    if not path.exists():
        return {"ok": False, "error": "plan not found"}
    if not is_locked(d):
        return {"ok": False, "error": "not locked"}

    # chmod 644 in case it was hard-locked
    try:
        os.chmod(path, 0o644)
    except Exception:
        pass

    now = _iso_now()
    _update_frontmatter(path, {"status": "active", "locked_at": "null"})

    # Append override notice
    text = path.read_text(encoding="utf-8")
    notice = f"\n\n> [!warning]+ OVERRIDE UNLOCK\n> Unlocked at {now} · Reason: {reason or 'not specified'}\n"
    atomic_io.vault_write(path, text + notice)

    return {"ok": True, "path": str(path), "unlocked_at": now, "reason": reason}


def add_thought(thought: str, d: Optional[str] = None) -> dict:
    """Append a preserved thought to the plan's ## Preserved Thoughts section."""
    if d is None:
        d = _today()
    path = _plan_path(d)
    if not path.exists():
        return {"ok": False, "error": "plan not found"}
    if is_locked(d):
        return {"ok": False, "error": "plan is locked — use unlock() to override"}

    now = datetime.now().strftime("%H:%M")
    text = path.read_text(encoding="utf-8")
    header = "## Preserved Thoughts"
    if header in text:
        bullet = f"- [{now}] {thought}"
        # Insert after the header line
        text = text.replace(
            header + "\n",
            header + "\n" + bullet + "\n",
            1,
        )
    else:
        text += f"\n{header}\n- [{now}] {thought}\n"
    atomic_io.vault_write(path, text)
    return {"ok": True, "thought": thought, "timestamp": now}


def add_satellite(title: str, url: str = "", path_ref: str = "", d: Optional[str] = None) -> dict:
    """Append a satellite (new file/repo/system) to the plan's ## Satellites section."""
    if d is None:
        d = _today()
    plan_path = _plan_path(d)
    if not plan_path.exists():
        return {"ok": False, "error": "plan not found"}
    if is_locked(d):
        return {"ok": False, "error": "plan is locked"}

    text = plan_path.read_text(encoding="utf-8")
    header = "## Satellites Spun Off"
    entry = f"- **{title}**"
    if path_ref:
        entry += f" — [[{path_ref}]]"
    if url:
        entry += f" ({url})"

    if header in text:
        text = text.replace(header + "\n", header + "\n" + entry + "\n", 1)
    else:
        text += f"\n{header}\n{entry}\n"
    atomic_io.vault_write(plan_path, text)
    return {"ok": True, "title": title}


def extract_rollover(d: Optional[str] = None) -> dict:
    """
    Extract incomplete items from a plan file.
    Returns {date, incomplete_items, completed_items, preserved_thoughts, satellites}.
    """
    if d is None:
        d = _today()
    path = _plan_path(d)
    if not path.exists():
        return {"date": d, "error": "not found", "incomplete_items": [], "completed_items": []}

    text = path.read_text(encoding="utf-8")

    # Find all unchecked items across Active Threads
    incomplete = re.findall(r"^- \[ \] (.+)$", text, re.M)
    completed = re.findall(r"^- \[x\] (.+)$", text, re.I | re.M)

    # Extract preserved thoughts
    thoughts_section = _extract_section_content(text, "## Preserved Thoughts")
    thoughts = [
        re.sub(r"^\- \[\d{2}:\d{2}\] ", "", line).strip()
        for line in thoughts_section.splitlines()
        if line.strip().startswith("- [")
    ]

    # Extract satellites
    satellites_section = _extract_section_content(text, "## Satellites Spun Off")
    satellites = [
        line.strip()
        for line in satellites_section.splitlines()
        if line.strip().startswith("- **")
    ]

    return {
        "date": d,
        "incomplete_items": incomplete,
        "completed_items": completed,
        "preserved_thoughts": thoughts,
        "satellites": satellites,
    }


def _extract_section_content(text: str, header: str) -> str:
    """Extract content of a markdown section (between header and next ## or EOF)."""
    idx = text.find(header)
    if idx == -1:
        return ""
    start = text.find("\n", idx) + 1
    next_h2 = text.find("\n## ", start)
    end = next_h2 if next_h2 != -1 else len(text)
    return text[start:end].strip()


# ── Markdown formatter ────────────────────────────────────────────────────────

def format_plan_summary_md(d: Optional[str] = None) -> str:
    """One-paragraph summary of plan state for daily note or session log."""
    info = get_plan(d)
    if not info["exists"]:
        return f"No plan for {d or _today()}."
    rollover = extract_rollover(d or _today())
    total_items = len(rollover["incomplete_items"]) + len(rollover["completed_items"])
    completed = len(rollover["completed_items"])
    pct = int(completed / total_items * 100) if total_items else 0
    status_badge = "🔒 Locked" if info["status"] == "locked" else "🟢 Active"
    return (
        f"**{status_badge}** · {completed}/{total_items} tasks ({pct}%) · "
        f"[[Plans/{info['date']}_daily_plan|View plan]]"
    )


# ── Preflight check (for preflight.py integration) ───────────────────────────

def preflight_check() -> dict:
    """Return a preflight-compatible check result for today's plan."""
    d = _today()
    info = get_plan(d)
    if not info["exists"]:
        return {
            "id": "D4",
            "category": "epistemic",
            "name": "Daily plan",
            "status": "warn",
            "message": "no plan for today — run: python3 daily_plan.py create",
            "action": "python3 daily_plan.py create",
        }
    status = info["status"]
    return {
        "id": "D4",
        "category": "epistemic",
        "name": "Daily plan",
        "status": "pass" if status == "active" else "warn",
        "message": f"plan {status} — {info['path'].split('/')[-1]}",
        "action": None,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily Plan lifecycle manager.")
    sub = parser.add_subparsers(dest="cmd")

    # create
    p_create = sub.add_parser("create", help="Create today's plan")
    p_create.add_argument("--date", default=None)
    p_create.add_argument("--force", action="store_true")
    p_create.add_argument("--from-rollover", default=None, metavar="YYYY-MM-DD",
                          help="Seed from rollover of this date")

    # lock
    p_lock = sub.add_parser("lock", help="Lock a plan (EOD)")
    p_lock.add_argument("--date", default=None)
    p_lock.add_argument("--hard", action="store_true", help="Also chmod 444")

    # unlock
    p_unlock = sub.add_parser("unlock", help="Unlock a locked plan (override)")
    p_unlock.add_argument("--date", required=True)
    p_unlock.add_argument("--reason", default="")

    # status
    p_status = sub.add_parser("status", help="Show plan status")
    p_status.add_argument("--date", default=None)

    # add-thought
    p_thought = sub.add_parser("add-thought", help="Append a preserved thought")
    p_thought.add_argument("thought")
    p_thought.add_argument("--date", default=None)

    # add-satellite
    p_sat = sub.add_parser("add-satellite", help="Record a satellite spun off")
    p_sat.add_argument("title")
    p_sat.add_argument("--url", default="")
    p_sat.add_argument("--path-ref", default="")
    p_sat.add_argument("--date", default=None)

    # rollover
    p_ro = sub.add_parser("rollover", help="Extract rollover items from a plan")
    p_ro.add_argument("--date", default=None, help="Defaults to yesterday")

    args = parser.parse_args()

    import json

    if args.cmd == "create":
        rollover_items = None
        source = None
        if args.from_rollover:
            ro = extract_rollover(args.from_rollover)
            rollover_items = ro.get("incomplete_items")
            source = args.from_rollover
        result = create_today(rollover_items=rollover_items, source_date=source,
                               force=args.force)
        print(json.dumps(result, indent=2))

    elif args.cmd == "lock":
        result = lock(args.date, hard=args.hard)
        print(json.dumps(result, indent=2))

    elif args.cmd == "unlock":
        result = unlock(args.date, args.reason)
        print(json.dumps(result, indent=2))

    elif args.cmd == "status":
        result = get_plan(args.date)
        print(json.dumps(result, indent=2))

    elif args.cmd == "add-thought":
        result = add_thought(args.thought, args.date)
        print(json.dumps(result, indent=2))

    elif args.cmd == "add-satellite":
        result = add_satellite(args.title, url=args.url,
                               path_ref=args.path_ref, d=args.date)
        print(json.dumps(result, indent=2))

    elif args.cmd == "rollover":
        d = args.date or _yesterday()
        result = extract_rollover(d)
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
