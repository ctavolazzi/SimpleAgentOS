#!/usr/bin/env python3
"""Bootstrap script for genesis vault exploration and documentation."""

import sys
from pathlib import Path

# Configure harness for genesis vault
sys.path.insert(0, str(Path(__file__).parent))
import daily_note

daily_note.VAULT_DIR = Path("/Users/ctavolazzi/Documents/Agent-Vaults/genesis-20260707")
daily_note.DAILY_NOTES_DIR = daily_note.VAULT_DIR / "Daily Notes"
daily_note.TEMPLATE_PATH = daily_note.VAULT_DIR / "System" / "Daily_Note_Template.md"

def bootstrap_findings():
    """Write initial architecture findings to in_the_lab."""
    findings = """**SimpleAgentOS Harness Discovery (Genesis Boot)**

**Core Architecture:**
- Single source of truth: daily note (SSOT)
- Section-based document model (20+ named sections via ## headers)
- Atomic vault writes with flock-style concurrency control
- Permission-based editing (claude, gemma, cron, user actors)
- Wikilink mandate (every artifact → [[link]] in daily note)

**API Surface (daily_note.py):**
- `read_section(name, date)` — extract section content
- `write_section(name, content, mode="replace"|"append")` — atomic writes
- `append_session_log(focus, changes, next_steps, files)` — nested callout journal
- `create_from_template(date)` — headless Templater rendering
- `last_handoff()` — gap-tolerant cross-session continuity
- `section_status(date)` — filled|empty|template|absent state

**Harness Modules (43 discovered):**
Core: daily_note, atomic_io, yaml_io, harness_log, claude_journal, atomic_io
Infrastructure: arxiv, weather, local_news, music_pick, work_vibe
Vault ops: vault_commit, vault_genesis, fill_sections, update_tasks
AI: brain, ranch, haiku_minion, llm_pipeline
Admin: git_audit, git_scanner, check_in, preflight, spin_up, wrap_up

**Dead Ends / Dependencies:**
- harness_log has optional import (graceful fail if unavailable)
- yaml_io uses ruamel.yaml for YAML RoundTrip
- atomic_io uses portalocker for cross-process concurrency

**Next Steps:**
[ ] Map all 43 modules to capability categories
[ ] Document section registry fully
[ ] Build unified harness explorer (read-only CLI)
"""
    return daily_note.write_section("in_the_lab", findings, actor="claude", mode="replace")

def bootstrap_work_efforts():
    """Set up work tracking."""
    tasks = """- [ ] Explore all 43 harness modules — categorize by purpose
- [ ] Document section registry (SECTIONS dict) — what each slot is for
- [ ] Build harness-explorer tool — unified read-only CLI interface
- [ ] Create vault state snapshot — current directory structure
- [ ] Seed tomorrow's work from findings
"""
    return daily_note.write_section("work_efforts", tasks, actor="claude", mode="replace")

def bootstrap_session_log():
    """Append bootstrap session to log."""
    return daily_note.append_session_log(
        focus="Harness exploration & architecture discovery",
        changes=[
            "Read Daily_Note_OS.md contract",
            "Explored daily_note.py (core SSOT API)",
            "Discovered 43 harness modules across 3 categories",
            "Documented atomic_io concurrency model",
        ],
        next_steps="Build unified harness explorer tool",
        files=["daily_note.py", "atomic_io.py", "Daily_Note_OS.md"],
        context="Genesis vault bootstrap — first session waking in this harness",
    )

if __name__ == "__main__":
    try:
        print("🔄 Bootstrapping genesis vault...")

        # Verify note exists
        note_path = daily_note.daily_path("2026-07-07")
        if not note_path.exists():
            print(f"✗ Daily note missing: {note_path}")
            sys.exit(1)

        # Write sections
        result1 = bootstrap_findings()
        print(f"✓ {result1['status']}: {result1['section']}")

        result2 = bootstrap_work_efforts()
        print(f"✓ {result2['status']}: {result2['section']}")

        result3 = bootstrap_session_log()
        print(f"✓ {result3['status']}: {result3['section']}")

        print("\n✓ Bootstrap complete. Vault ready for exploration.")

    except Exception as e:
        print(f"✗ Bootstrap failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
