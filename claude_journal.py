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


_PLACEHOLDER_PATTERNS = [
    r"<!-- One bullet per realization -->",
    r"<!-- One bullet per question -->",
    r"<!-- Freeform\. What actually caught your attention\? -->",
    r"<!-- What should carry forward\? -->",
    r"<!-- Fill in during/after session -->",
]


def _append_to_section(path: Path, section_header: str, bullet: str) -> bool:
    """Append a bullet under a section header. Returns True on success.

    Handles three cases the original could not, each of which returned False
    silently while the CLI printed a timestamp that looked like success:

    1. The section is LAST in the file. The original inserted before the next
       `---` or `## `, so with nothing following it there was no insertion point
       and the section was unwritable forever.
    2. The section is absent entirely. Two writers disagreed on the template:
       spin_up's scaffold wrote only `## Session Log` and `## Notes`, while every
       add-* verb here targets `## Realizations`, `## Open Questions`,
       `## Threads I'm Holding` and `## What I Find Interesting`. Since
       create_entry is idempotent it never repaired the headings, so every add
       against a spin_up-created journal failed. Create the section instead.
    3. Placeholder removal shifted the offset. The original computed insert_pos
       against the pre-substitution string and then sliced the post-substitution
       string, so any placeholder earlier in the file moved the split point by
       its own length.
    """
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")

    # Strip placeholders FIRST so every offset below refers to the final string.
    for pat in _PLACEHOLDER_PATTERNS:
        text = re.sub(pat, "", text)

    idx = text.find(section_header)
    if idx == -1:
        # Case 2: create the section rather than failing.
        text = text.rstrip("\n") + f"\n\n---\n\n{section_header}\n\n- {bullet}\n"
        atomic_io.vault_write(path, text)
        return True

    after_header = idx + len(section_header)
    section_text = text[after_header:]
    next_divider = re.search(r"\n---\n|\n## ", section_text)
    # Case 1: EOF is a valid boundary, not a failure.
    end = after_header + (next_divider.start() if next_divider else len(section_text))

    text = text[:end].rstrip("\n") + f"\n- {bullet}\n" + text[end:]
    atomic_io.vault_write(path, text)
    return True


def _remember(text: str, kind: str, section: str, d: str) -> None:
    """Mirror the bullet into the OS's PocketBase journal so it's queryable later.

    The markdown file stays the human-readable record; this is the index. Purely
    additive — if pb_journal or PocketBase is unavailable the markdown write has
    already succeeded and nothing here may change that.
    """
    try:
        import pb_journal

        pb_journal.journal(
            text,
            kind=kind,
            source="claude_journal",
            tags=["claude-journal", section],
            path_ref=str(_journal_path(d)),
            importance=0.6,
            metadata={"section": section, "note_date": d},
        )
    except Exception:  # noqa: BLE001 — journaling must never break journaling
        pass


def add_realization(text: str, d: Optional[str] = None) -> dict:
    if d is None:
        d = _today()
    path = _journal_path(d)
    ts = _hm()
    bullet = f"[{ts}] {text}"
    ok = _append_to_section(path, "## Realizations", bullet)
    if ok:
        _remember(text, "reflection", "realization", d)
    return {"ok": ok, "realization": text, "timestamp": ts}


def add_question(text: str, d: Optional[str] = None) -> dict:
    if d is None:
        d = _today()
    path = _journal_path(d)
    ts = _hm()
    bullet = f"[{ts}] {text}"
    ok = _append_to_section(path, "## Open Questions", bullet)
    if ok:
        _remember(text, "question", "open-question", d)
    return {"ok": ok, "question": text, "timestamp": ts}


def add_thread(text: str, d: Optional[str] = None) -> dict:
    if d is None:
        d = _today()
    path = _journal_path(d)
    ts = _hm()
    bullet = f"[{ts}] {text}"
    ok = _append_to_section(path, "## Threads I'm Holding", bullet)
    if ok:
        _remember(text, "note", "thread", d)
    return {"ok": ok, "thread": text, "timestamp": ts}


def add_interesting(text: str, d: Optional[str] = None) -> dict:
    if d is None:
        d = _today()
    path = _journal_path(d)
    ts = _hm()
    bullet = f"[{ts}] {text}"
    ok = _append_to_section(path, "## What I Find Interesting", bullet)
    if ok:
        _remember(text, "note", "interesting", d)
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

    def emit(result: dict) -> None:
        """Print the result and make a failed write cost an exit code.

        These verbs used to exit 0 whatever happened. On 2026-07-28 every add
        was returning {"ok": false} against a journal written from the other
        template, and the failure went unnoticed for a session because the
        output was piped through `tail -2`, which cropped the `ok` field and
        left a timestamp on screen that read like success. A non-zero exit
        survives cropping.
        """
        print(json.dumps(result, indent=2))
        if result.get("ok") is False:
            sys.exit(1)

    if args.cmd == "create":
        emit(create_entry())
    elif args.cmd == "add-realization":
        emit(add_realization(args.text, args.date))
    elif args.cmd == "add-question":
        emit(add_question(args.text, args.date))
    elif args.cmd == "add-thread":
        emit(add_thread(args.text, args.date))
    elif args.cmd == "add-interesting":
        emit(add_interesting(args.text, args.date))
    elif args.cmd == "status":
        emit(get_entry(args.date))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
