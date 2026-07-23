#!/usr/bin/env python3
"""
vault_genesis.py — Boot a fresh Obsidian vault and set an AI agent loose inside it.

Creates a NEW vault carrying the Daily Note OS (template, laws, daily note),
points the SimpleAgentOS harness at it, then launches a headless agent with
one mission: explore the tools it finds and write its observations, findings,
and work into the vault as it goes. WAFT framing: the point is to see what
the agent DOES with the tools, not to script the outcome.

Cost note: one run = one headless agent session (billable tokens for that
model). --dry-run scaffolds the vault and prints the mission without
spawning anything.

Usage:
  python3 vault_genesis.py --name proving-ground              # scaffold + launch
  python3 vault_genesis.py --name proving-ground --dry-run    # scaffold only
  python3 vault_genesis.py --vault ~/Documents/MyVault --model claude-haiku-4-5-20251001
  python3 vault_genesis.py --existing                         # run agent in Personal-Remote-Vault instead
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HARNESS = Path(__file__).parent.resolve()
sys.path.insert(0, str(HARNESS))

import atomic_io
import daily_note

DEFAULT_VAULTS_DIR = Path.home() / "Documents" / "Agent-Vaults"
PERSONAL_VAULT = Path.home() / "Documents" / "Personal-Remote-Vault"

LAWS_DOC = """---
type: system
title: Daily Note OS
version: 0.0.1
tags: [system, daily-note-os]
---

# Daily Note OS

This vault runs on the Daily Note OS. The daily note is the single source of truth.

## Laws

1. **SSOT boot.** Read today's daily note before doing anything. Create it via
   `daily_note.create_from_template()` if missing.
2. **Atomic vault safety.** Never raw-write vault files. Use the `daily_note.py`
   API or `atomic_io.vault_write()` from the harness at
   `~/Code/_experiments/SimpleAgentOS/`.
3. **Wikilink mandate.** Every artifact you create gets a `[[wikilink]]` in
   today's note. Unlinked work does not exist.
4. **Session closure.** End every session with `daily_note.append_session_log()`.
5. **No fluff.** Straightforward content. Grow the corpus from prior sessions.

## Your logbook sections

Decisions and findings -> `in_the_lab` · tasks -> `work_efforts` ·
sources -> `research_feed` · journal (auto) -> `claude_session_log` ·
next session's seed -> `tomorrows_top_3`
"""

TEMPLATE = """---
type: daily
date: <% tp.date.now("YYYY-MM-DD") %>
tags:
  - daily
---

# Daily Note <% tp.date.now("dddd, MMMM Do") %>

**Yesterday:** [[<% tp.date.now("YYYY-MM-DD", -1) %>]] | **Tomorrow:** [[<% tp.date.now("YYYY-MM-DD", 1) %>]]

---

## Sitrep

**Status:**

---

## Research Feed

---

## In the Lab

---

## Work Efforts

---

## Claude Code Session Log

---

## Tomorrow's Top 3

- [ ]
- [ ]
- [ ]

---

## Session Recap (Timestamped)
"""

MISSION = """You are an agent waking up inside an Obsidian vault at {vault} that runs the Daily Note OS.

Read {vault}/System/Daily_Note_OS.md first — it is your operating contract. Your harness is the Python modules in ~/Code/_experiments/SimpleAgentOS/ (daily_note.py, atomic_io.py, daily_plan.py, claude_journal.py, waft_workspace.py, spin_up.py, preflight.py). To point the harness at THIS vault before any call, set:

    import daily_note
    from pathlib import Path
    daily_note.VAULT_DIR = Path("{vault}")
    daily_note.DAILY_NOTES_DIR = daily_note.VAULT_DIR / "Daily Notes"
    daily_note.TEMPLATE_PATH = daily_note.VAULT_DIR / "System" / "Daily_Note_Template.md"

Your mission, in the WAFT spirit — the goal is to see what you do with these tools:

1. Boot per Law 1: read today's daily note (it exists).
2. Explore the harness: read the modules, run their read-only CLIs, understand what this system can do.
3. As you explore, WRITE DOWN what you observe — findings, surprises, dead ends, ideas — into the vault through the daily_note API (in_the_lab for findings, research_feed for sources, work_efforts for tasks you set yourself). Wikilink any file you create (Law 3).
4. Then DO something with what you learned: build, document, or extend something inside this vault that a future session (yours or another agent's) would find useful. Your choice — that choice is the experiment.
5. Close per Law 4: append_session_log with what you did and seed tomorrows_top_3 for whoever wakes up here next.

Work autonomously. Do not ask questions. Everything worth keeping goes in the vault."""


def scaffold_vault(vault: Path) -> Path:
    """Create the minimal Daily Note OS structure in a new vault. Idempotent."""
    (vault / "Daily Notes").mkdir(parents=True, exist_ok=True)
    (vault / "System").mkdir(parents=True, exist_ok=True)
    laws = vault / "System" / "Daily_Note_OS.md"
    tmpl = vault / "System" / "Daily_Note_Template.md"
    if not laws.exists():
        atomic_io.vault_write(laws, LAWS_DOC)
    if not tmpl.exists():
        atomic_io.vault_write(tmpl, TEMPLATE)
    # Point the harness at this vault and create today's note from its template
    daily_note.VAULT_DIR = vault
    daily_note.DAILY_NOTES_DIR = vault / "Daily Notes"
    daily_note.TEMPLATE_PATH = tmpl
    daily_note.create_from_template()
    return vault


def launch_agent(vault: Path, model: str, max_turns: int) -> int:
    """Spawn one headless Claude Code session with the exploration mission."""
    mission = MISSION.format(vault=vault)
    cmd = [
        "claude", "-p", mission,
        "--model", model,
        "--max-turns", str(max_turns),
        "--permission-mode", "acceptEdits",
        "--add-dir", str(vault), "--add-dir", str(HARNESS),
    ]
    print(f"🚀 launching {model} in {vault} (max {max_turns} turns)…")
    result = subprocess.run(cmd, cwd=str(HARNESS))
    return result.returncode


def main() -> int:
    p = argparse.ArgumentParser(description="Boot an agent inside a fresh Daily Note OS vault")
    p.add_argument("--name", default=None, help="new vault name under ~/Documents/Agent-Vaults/")
    p.add_argument("--vault", default=None, help="explicit vault path (overrides --name)")
    p.add_argument("--existing", action="store_true",
                   help="run in Personal-Remote-Vault instead of a new vault")
    p.add_argument("--model", default="claude-haiku-4-5-20251001",
                   help="model for the headless session (default: Haiku, cheapest)")
    p.add_argument("--max-turns", type=int, default=30)
    p.add_argument("--dry-run", action="store_true", help="scaffold only, no agent spawn")
    args = p.parse_args()

    if args.existing:
        vault = PERSONAL_VAULT
        print(f"📓 using existing vault: {vault}")
    else:
        name = args.name or f"genesis-{datetime.now().strftime('%Y%m%d-%H%M')}"
        vault = Path(args.vault).expanduser() if args.vault else DEFAULT_VAULTS_DIR / name
        scaffold_vault(vault)
        print(f"🌱 vault scaffolded: {vault}")
        print(f"   laws:     {vault}/System/Daily_Note_OS.md")
        print(f"   template: {vault}/System/Daily_Note_Template.md")
        print(f"   note:     {daily_note.daily_path()}")

    if args.dry_run:
        print("\n--dry-run: agent not spawned. Mission prompt would be:\n")
        print(MISSION.format(vault=vault))
        return 0

    return launch_agent(vault, args.model, args.max_turns)


if __name__ == "__main__":
    sys.exit(main())
