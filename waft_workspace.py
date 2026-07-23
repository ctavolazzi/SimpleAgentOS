"""
waft_workspace.py — Bridge between the daily harness and the local WAFT framework.

Reads the local WAFT Being state, maps today's expected work to Scint quest types,
and renders a markdown workspace block for the daily note.

The WAFT Workspace section is an *evolving* artifact: each day it reflects what
the agent has accumulated — fitness, memories, lessons, active quests. Over time
the Being gains skills and lessons as quests are completed and failures are logged.

Design:
    Being "the_one"  →  persists across days in waft_memory.db
    Daily quests     →  derived from yesterday's tomorrows_top_3 section
    Scint types      →  ontological error categories the Being must stabilize
    Chronicle        →  append-only event log (sqlite: chronicle table)
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import atomic_io

# ── WAFT path ──────────────────────────────────────────────────────────────

WAFT_PROJECT = Path.home() / "Code" / "active" / "waft"
WAFT_MEMORY_DB = WAFT_PROJECT / "waft_memory.db"

# Being journal — persistent vault file the Being writes to each session
BEING_JOURNAL = (
    Path.home()
    / "Documents"
    / "Personal-Remote-Vault"
    / "waft"
    / "being"
    / "the_one.md"
)


# ── Scint type mapping ─────────────────────────────────────────────────────
# Maps work-keyword patterns → WAFT Scint type + fitness challenge description

_SCINT_MAP = [
    (["deploy", "wrangler", "migration", "d1 execute", "resend", "e2e", "secret put"],
     "LOGIC_FRACTURE",
     "Deploy gates: ordered multi-step execution; any misordering = fracture"),
    (["debug", "triage", "fail", "error", "fix", "broken", "mcp", "root cause"],
     "SYNTAX_TEAR",
     "Diagnostic pass: surface malformed system state, patch tears"),
    (["security", "audit", "vulnerability", "cve", "compliance", "auth"],
     "SAFETY_VOID",
     "Security surface: identify voids, close unsafe paths"),
    (["memory", "prune", "stale", "archive", "cleanup", "refresh"],
     "HALLUCINATION",
     "Epistemic hygiene: remove stale beliefs, verify freshness"),
    (["frontend", "ui", "css", "component", "design", "page", "layout"],
     "SYNTAX_TEAR",
     "Interface coherence: visual and structural correctness"),
    (["research", "arxiv", "paper", "reading", "llm", "model"],
     "LOGIC_FRACTURE",
     "Knowledge integration: synthesize new evidence into stable beliefs"),
    (["doc", "write", "log", "devlog", "readme", "note", "commit"],
     "SYNTAX_TEAR",
     "Signal clarity: output well-formed, accurate records"),
]

_DEFAULT_SCINT = ("LOGIC_FRACTURE", "General execution — maintain ordered reasoning")


# ── Chronicle reader ───────────────────────────────────────────────────────

def _read_chronicle(n: int = 5) -> list[dict]:
    """Read last n entries from waft_memory.db chronicle table."""
    if not WAFT_MEMORY_DB.exists():
        return []
    try:
        db = sqlite3.connect(str(WAFT_MEMORY_DB))
        cols_raw = db.execute("PRAGMA table_info(chronicle)").fetchall()
        # Chronicle columns are 0,1,2,3,4 (no names) — map by position
        rows = db.execute(
            "SELECT * FROM chronicle ORDER BY rowid DESC LIMIT ?", (n,)
        ).fetchall()
        db.close()
        entries = []
        for r in rows:
            entries.append({
                "id": r[0],
                "timestamp": r[1],
                "type": r[2],
                "content": r[3],
                "metadata": r[4],
            })
        return entries
    except Exception:
        return []


# ── Being state reader ─────────────────────────────────────────────────────


def _derive_genome_id(bv: dict) -> str:
    """Return a 16-char genome token. Uses stored field if present, else
    derives deterministically from being_id via SHA-256 — same algorithm
    as Being.scientific_name so the value is stable across calls."""
    stored = (bv.get("genome_id") or "").strip()
    if stored:
        return stored[:16]
    being_id = bv.get("being_id") or "the_one"
    return hashlib.sha256(being_id.encode()).hexdigest()[:16]


def _read_being() -> dict:
    """Load the_one Being from waft. Falls back to defaults on error."""
    defaults = {
        "being_id": "the_one",
        "state": "SPAWNING",
        "fitness": 0.0,
        "generation": 0,
        "genome_id": "",
        "will_to_live": 100.0,
        "luck": 50.0,
        "skills_count": 0,
        "memories_count": 0,
        "lessons_count": 0,
        "reality_id": "unknown",
        "goals": [],
    }
    try:
        from waft import BeingSystem
        bs = BeingSystem(WAFT_PROJECT)
        being = bs.get_or_create_the_one()
        bv = vars(being)
        state = bv.get("state")
        state_str = state.value if hasattr(state, "value") else str(state)
        return {
            "being_id": bv.get("being_id", "the_one"),
            "state": state_str,
            "fitness": bv.get("fitness", 0.0),
            "generation": bv.get("generation") or 0,
            "genome_id": _derive_genome_id(bv),
            "will_to_live": bv.get("will_to_live", 100.0),
            "luck": bv.get("luck", 50.0),
            "skills_count": len(bv.get("skills", {})),
            "memories_count": len(bv.get("memories", [])),
            "lessons_count": len(bv.get("lessons", [])),
            "reality_id": bv.get("reality_id", "unknown"),
            "goals": bv.get("goals", []),
        }
    except Exception as e:
        defaults["load_error"] = str(e)
        return defaults


# ── Quest derivation ───────────────────────────────────────────────────────

def _yesterday_top3() -> list[str]:
    """Parse yesterday's tomorrows_top_3 section into a list of task strings."""
    vault = Path.home() / "Documents" / "Personal-Remote-Vault" / "Daily Notes"
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    path = vault / f"{yesterday}.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    in_section = False
    tasks = []
    for line in text.splitlines():
        if line.strip() == "## Tomorrow's Top 3":
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            m = re.match(r"^- \[[ x]\] (.+)", line)
            if m:
                tasks.append(m.group(1).strip())
    return tasks


def _map_quests(tasks: list[str]) -> list[dict]:
    """Map task strings to WAFT quest dicts with Scint type annotation."""
    quests = []
    for task in tasks:
        low = task.lower()
        scint_type, challenge = _DEFAULT_SCINT
        for keywords, stype, desc in _SCINT_MAP:
            if any(kw in low for kw in keywords):
                scint_type = stype
                challenge = desc
                break
        quests.append({
            "task": task,
            "scint_type": scint_type,
            "challenge": challenge,
            "complete": False,
        })
    return quests


# ── Fitness summary ────────────────────────────────────────────────────────

def _fitness_bar(score: float) -> str:
    filled = int(score * 10)
    return "█" * filled + "░" * (10 - filled) + f" {score:.2f}"


def _state_emoji(state: str) -> str:
    return {
        "spawning": "🥚",
        "learning": "🌱",
        "evolving": "🔄",
        "completing": "✅",
        "archived": "📦",
        "dead": "💀",
    }.get(state.lower(), "❓")


# ── Public API ─────────────────────────────────────────────────────────────

def fetch() -> dict:
    """
    Gather all WAFT workspace state.

    Returns:
        {
          "being": dict,
          "quests": list[dict],
          "chronicle": list[dict],
          "fetched_at": str,
        }
    """
    return {
        "being": _read_being(),
        "quests": _map_quests(_yesterday_top3()),
        "chronicle": _read_chronicle(n=3),
        "fetched_at": datetime.now().isoformat(),
    }


def format_md(data: dict) -> str:
    """Render WAFT workspace state as Obsidian-compatible markdown."""
    being = data["being"]
    quests = data["quests"]
    chronicle = data["chronicle"]

    state_str = being["state"]
    emoji = _state_emoji(state_str)
    genome = being["genome_id"] or "uninitialized"
    reality_short = being["reality_id"].replace("reality_", "")[:24]

    lines = [
        f"> [!abstract]+ WAFT Being · `{being['being_id']}`",
        f"> **State:** {emoji} `{state_str}` · **Gen:** {being['generation']} · "
        f"**Fitness:** `{_fitness_bar(being['fitness'])}`",
        f"> **Reality:** `{reality_short}` · **Genome:** `{genome}`",
        f"> **Vitals:** will_to_live `{being['will_to_live']:.0f}` · "
        f"luck `{being['luck']:.0f}` · "
        f"skills `{being['skills_count']}` · "
        f"memories `{being['memories_count']}` · "
        f"lessons `{being['lessons_count']}`",
        "",
    ]

    # Today's quests
    lines.append("**Today's Quests** *(derived from yesterday's top 3)*")
    if quests:
        for q in quests:
            check = "x" if q["complete"] else " "
            # Strip outer bold markers if task already contains them
            task_text = re.sub(r"^\*\*(.+)\*\*$", r"\1", q["task"].strip())
            lines.append(f"- [{check}] {task_text}")
            lines.append(f"  - Scint: `{q['scint_type']}` — {q['challenge']}")
    else:
        lines.append("- [ ] No quests derived — check yesterday's Tomorrow's Top 3")

    lines.append("")

    # Chronicle
    lines.append("**Chronicle** *(last 3 entries)*")
    if chronicle:
        for entry in chronicle:
            ts = entry.get("timestamp", "?")[:10]
            etype = entry.get("type", "?")
            content = (entry.get("content") or "").strip()[:80]
            if len(entry.get("content", "")) > 80:
                content += "…"
            lines.append(f"> `{ts}` `{etype}` — {content}")
    else:
        lines.append("> No chronicle entries yet.")

    lines.append("")
    lines.append(f"*Fetched {data['fetched_at'][:16]} · "
                 f"[waft v0.10.0](active/waft/README) · "
                 f"[Being API](active/waft/src/waft/being.py) · "
                 f"[Journal](../waft/being/the_one.md)*")

    return "\n".join(lines)


# ── Being journal writer ───────────────────────────────────────────────────

def write_being_journal_entry(
    entry_type: str,
    summary: str,
    *,
    details: Optional[list] = None,
    being_state: Optional[dict] = None,
) -> bool:
    """
    Append a dated callout entry to the being's persistent vault journal.

    entry_type: "session_start" | "session_end" | "quest_complete" | "thought"
    summary:    one-line description of the event
    details:    optional list of bullet strings
    being_state: optional being dict from fetch()["being"] — logs fitness/state snapshot

    Returns True on success, False on any write error (never raises).
    """
    try:
        BEING_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        date = datetime.now().strftime("%Y-%m-%d")

        lines = [f"> [!note]- {ts} · {entry_type} — {summary}"]

        if being_state:
            fitness = being_state.get("fitness", 0.0)
            state = being_state.get("state", "?")
            gen = being_state.get("generation", 0)
            lines.append(f"> **State:** {state} · Gen {gen} · Fitness {fitness:.2f}")

        if details:
            for d in details:
                lines.append(f"> - {d}")

        entry_text = "\n".join(lines)

        if BEING_JOURNAL.exists():
            existing = BEING_JOURNAL.read_text(encoding="utf-8")
            # Insert after the `---` separator (end of front matter + intro block)
            # Find the last `---` divider and append after it
            sep_idx = existing.rfind("\n---\n")
            if sep_idx != -1:
                new_text = (
                    existing[: sep_idx + 5]  # up to and including \n---\n
                    + "\n"
                    + entry_text
                    + "\n"
                    + existing[sep_idx + 5 :]
                )
            else:
                new_text = existing.rstrip() + "\n\n" + entry_text + "\n"
            atomic_io.vault_write(BEING_JOURNAL, new_text)
        else:
            # Bootstrap the file if missing
            header = (
                "---\n"
                "type: being_journal\n"
                f"being_id: the_one\n"
                f"created: \"{date}\"\n"
                "source: waft_workspace.py\n"
                "tags: [waft, being, journal, auto-generated]\n"
                "---\n\n"
                "# Being Journal · `the_one`\n\n"
                "Auto-generated by the daily harness.\n\n"
                "---\n\n"
            )
            atomic_io.vault_write(BEING_JOURNAL, header + entry_text + "\n")

        # Also add chronicle entry to SQLite
        _append_chronicle(entry_type.upper(), summary)
        return True

    except Exception:
        return False


def _append_chronicle(severity: str, message: str, context: str = "") -> bool:
    """Insert a row into waft_memory.db chronicle table. Never raises.

    Deduplicates: skips insert if identical (severity, message) already logged today.
    This prevents repeated spin_up runs from stacking duplicate SESSION_START entries.
    """
    if not WAFT_MEMORY_DB.exists():
        return False
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db = sqlite3.connect(str(WAFT_MEMORY_DB))
        existing = db.execute(
            "SELECT 1 FROM chronicle WHERE date(timestamp) = date('now') "
            "AND severity = ? AND message = ? LIMIT 1",
            (severity, message),
        ).fetchone()
        if existing:
            db.close()
            return True  # already recorded today — skip duplicate
        db.execute(
            "INSERT INTO chronicle (timestamp, severity, message, context) VALUES (?, ?, ?, ?)",
            (ts, severity, message, context),
        )
        db.commit()
        db.close()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    data = fetch()
    print(format_md(data))
    print()
    print("Being:", data["being"])
    print("Quests:", data["quests"])
