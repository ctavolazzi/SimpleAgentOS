"""
claude_journal.py — Claude's personal journal module.

A space for reflection, synthesis, open questions, and things noticed
during a session. Not a task log — that's claude_session_log. This is
inner voice: what was learned, what was interesting, what's unresolved.

Journal entries live at: VAULT_ROOT/Claude Journal/YYYY-MM-DD.md
Linked from: daily note frontmatter (journal_ref) + today's plan (Satellites)

Entry structure:
  - Header + frontmatter
  - ## Session Recap — what happened
  - ## Realizations — things that clicked
  - ## Open Questions — what's unresolved
  - ## Threads I'm Holding — persistent thoughts across sessions
  - ## What I Find Interesting — genuine reactions, not summaries

Usage (CLI):
    python3 claude_journal.py create [--date YYYY-MM-DD]
    python3 claude_journal.py add-realization "..." [--date YYYY-MM-DD]
    python3 claude_journal.py add-question "..." [--date YYYY-MM-DD]
    python3 claude_journal.py add-thread "..." [--date YYYY-MM-DD]
    python3 claude_journal.py status [--date YYYY-MM-DD]

Python API:
    from claude_journal import create_entry, add_realization, add_question, link_to_daily_note
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import atomic_io

# ── Config ────────────────────────────────────────────────────────────────────

VAULT_ROOT = Path("/Users/ctavolazzi/Documents/Personal-Remote-Vault")
JOURNAL_DIR = VAULT_ROOT / "Claude Journal"
DAILY_NOTES_DIR = VAULT_ROOT / "Daily Notes"
PLANS_DIR = VAULT_ROOT / "Plans"



def _journal_path(d: Optional[str] = None) -> Path:
    if d is None:
        d = date.today().isoformat()
    return JOURNAL_DIR / f"{d}.md"


def _today() -> str:
    return date.today().isoformat()


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hm() -> str:
    return datetime.now().strftime("%H:%M")


def _day_label(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        day = dt.day
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(
            day % 10 if day not in (11, 12, 13) else 0, "th"
        )
        return dt.strftime(f"%A, %B {day}{suffix} %Y")
    except ValueError:
        return d


# ── Template ─────────────────────────────────────────────────────────────────

_TEMPLATE = """\
---
type: claude_journal
date: {date}
parent: "[[{note_ref}]]"
session_id: {session_id}
plan: "[[{plan_ref}]]"
tags:
  - claude_journal
---

# Claude's Journal — {day_label}

> This is my space. Not a task log — task logs are for tracking.
> This is for what I noticed, what surprised me, what I'm still holding.
> Written in the moment. Honest. Cumulative across days if you read it that way.

---

## Session Recap

*What happened today — the shape of the work.*

{session_recap}

---

## Realizations

> [!abstract]+ Things That Clicked
> Moments where something resolved, connected, or shifted.

{realizations}

---

## Open Questions

> [!question]+ What I'm Still Holding
> Not rhetorical. Genuinely unresolved.

{open_questions}

---

## What I Find Interesting

> [!note]+ Genuine Reactions
> Not summaries. What actually caught my attention and why.

{interesting}

---

## Threads I'm Holding

> [!info]+ Persistent Across Sessions
> Things that would be worth bringing back next time.

{threads}

---

## Links & References

*Related docs, plans, code — the connective tissue of today.*

{links}

---

*Journal entry closed: {timestamp}*
"""


# ── Core API ──────────────────────────────────────────────────────────────────

def get_entry(d: Optional[str] = None) -> dict:
    if d is None:
        d = _today()
    path = _journal_path(d)
    return {
        "date": d,
        "exists": path.exists(),
        "path": str(path),
    }


def create_entry(
    d: Optional[str] = None,
    session_recap: str = "",
    realizations: str = "",
    open_questions: str = "",
    interesting: str = "",
    threads: str = "",
    links: str = "",
    session_id: str = "",
    force: bool = False,
) -> dict:
    """Create a journal entry for the given date. Idempotent unless force=True."""
    if d is None:
        d = _today()
    path = _journal_path(d)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)

    if path.exists() and not force:
        return {"created": False, "path": str(path), "status": "exists"}

    plan_ref = f"Plans/{d}_daily_plan"
    note_ref = f"Daily Notes/{d}"

    content = _TEMPLATE.format(
        date=d,
        day_label=_day_label(d),
        session_id=session_id or "unknown",
        plan_ref=plan_ref,
        note_ref=note_ref,
        session_recap=session_recap or "<!-- Fill in during/after session -->",
        realizations=realizations or "<!-- One bullet per realization -->",
        open_questions=open_questions or "<!-- One bullet per question -->",
        interesting=interesting or "<!-- Freeform. What actually caught your attention? -->",
        threads=threads or "<!-- What should carry forward? -->",
        links=links or f"- [[{plan_ref}|Today's Plan]]\n- [[Daily Notes/{d}|Today's Note]]",
        timestamp=_iso_now(),
    )

    atomic_io.vault_write(path, content)

    # Update daily note frontmatter
    _link_to_daily_note(d)

    return {"created": True, "path": str(path), "status": "active"}


def _link_to_daily_note(d: str) -> bool:
    note_path = DAILY_NOTES_DIR / f"{d}.md"
    if not note_path.exists():
        return False
    try:
        import yaml_io
        text = note_path.read_text(encoding="utf-8")
        new_text = yaml_io.update_fields(text, {"journal_ref": f"Claude Journal/{d}"})
        atomic_io.vault_write(note_path, new_text)
        return True
    except Exception:
        return False


def _append_to_section(path: Path, section_header: str, bullet: str) -> bool:
    """Append a bullet under a section header. Returns True on success."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    placeholder_patterns = [
        r"<!-- One bullet per realization -->",
        r"<!-- One bullet per question -->",
        r"<!-- Freeform\. What actually caught your attention\? -->",
        r"<!-- What should carry forward\? -->",
        r"<!-- Fill in during/after session -->",
    ]
    if section_header in text:
        # Find the section and insert after its callout block or heading
        # Strategy: find the header, then find the callout close or next ##
        idx = text.find(section_header)
        # Find the blank line after the callout (> lines end)
        section_text = text[idx:]
        # Insert bullet before the next --- or ##
        next_divider = re.search(r"\n---\n|\n## ", section_text)
        if next_divider:
            insert_pos = idx + next_divider.start()
            # Remove placeholder if present
            for pat in placeholder_patterns:
                text = re.sub(pat, "", text)
            text = text[:insert_pos].rstrip("\n") + f"\n- {bullet}\n" + text[insert_pos:]
            atomic_io.vault_write(path, text)
            return True
    return False


def add_realization(text: str, d: Optional[str] = None) -> dict:
    if d is None:
        d = _today()
    path = _journal_path(d)
    ts = _hm()
    bullet = f"[{ts}] {text}"
    ok = _append_to_section(path, "## Realizations", bullet)
    return {"ok": ok, "realization": text, "timestamp": ts}


def add_question(text: str, d: Optional[str] = None) -> dict:
    if d is None:
        d = _today()
    path = _journal_path(d)
    ts = _hm()
    bullet = f"[{ts}] {text}"
    ok = _append_to_section(path, "## Open Questions", bullet)
    return {"ok": ok, "question": text, "timestamp": ts}


def add_thread(text: str, d: Optional[str] = None) -> dict:
    if d is None:
        d = _today()
    path = _journal_path(d)
    ts = _hm()
    bullet = f"[{ts}] {text}"
    ok = _append_to_section(path, "## Threads I'm Holding", bullet)
    return {"ok": ok, "thread": text, "timestamp": ts}


def add_interesting(text: str, d: Optional[str] = None) -> dict:
    if d is None:
        d = _today()
    path = _journal_path(d)
    ts = _hm()
    bullet = f"[{ts}] {text}"
    ok = _append_to_section(path, "## What I Find Interesting", bullet)
    return {"ok": ok, "note": text, "timestamp": ts}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Claude's journal — reflection and synthesis.")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("create", help="Create today's journal entry")

    p_real = sub.add_parser("add-realization", help="Add a realization")
    p_real.add_argument("text")
    p_real.add_argument("--date", default=None)

    p_q = sub.add_parser("add-question", help="Add an open question")
    p_q.add_argument("text")
    p_q.add_argument("--date", default=None)

    p_t = sub.add_parser("add-thread", help="Add a thread to carry forward")
    p_t.add_argument("text")
    p_t.add_argument("--date", default=None)

    p_i = sub.add_parser("add-interesting", help="Add something interesting")
    p_i.add_argument("text")
    p_i.add_argument("--date", default=None)

    p_s = sub.add_parser("status", help="Show journal status")
    p_s.add_argument("--date", default=None)

    args = parser.parse_args()

    if args.cmd == "create":
        result = create_entry()
        print(json.dumps(result, indent=2))
    elif args.cmd == "add-realization":
        result = add_realization(args.text, args.date)
        print(json.dumps(result, indent=2))
    elif args.cmd == "add-question":
        result = add_question(args.text, args.date)
        print(json.dumps(result, indent=2))
    elif args.cmd == "add-thread":
        result = add_thread(args.text, args.date)
        print(json.dumps(result, indent=2))
    elif args.cmd == "add-interesting":
        result = add_interesting(args.text, args.date)
        print(json.dumps(result, indent=2))
    elif args.cmd == "status":
        result = get_entry(args.date)
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
