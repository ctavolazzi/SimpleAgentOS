"""
vault_stats.py — Local vault metrics: streaks, note counts, yesterday's output.

Part of the daily-note harness. Pure local filesystem reads, no network.
Feeds a dashboard line in the Sitrep — the "you showed up" counter.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
DAILY_NOTES_DIR = VAULT_DIR / "Daily Notes"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def compute(date: Optional[str] = None) -> dict:
    """
    Compute vault stats as of a date (YYYY-MM-DD, default today).

    Returns:
      streak_days     — consecutive daily notes ending at `date` (inclusive)
      total_daily     — count of date-named daily notes
      prev_note_date  — most recent daily note before `date` (or None)
      prev_note_words — word count of that note
      journal_today   — Claude Journal entry exists for `date`
      plan_today      — daily plan exists for `date`
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    existing = set()
    if DAILY_NOTES_DIR.is_dir():
        existing = {p.stem for p in DAILY_NOTES_DIR.glob("*.md")
                    if _DATE_RE.match(p.stem)}

    # Streak: walk backwards from `date` while notes exist
    streak = 0
    cursor = datetime.strptime(date, "%Y-%m-%d")
    while cursor.strftime("%Y-%m-%d") in existing:
        streak += 1
        cursor -= timedelta(days=1)

    prev_dates = sorted(d for d in existing if d < date)
    prev_date = prev_dates[-1] if prev_dates else None
    prev_words = 0
    if prev_date:
        try:
            prev_words = len((DAILY_NOTES_DIR / f"{prev_date}.md")
                             .read_text(encoding="utf-8").split())
        except OSError:
            pass

    return {
        "date": date,
        "streak_days": streak,
        "total_daily": len(existing),
        "prev_note_date": prev_date,
        "prev_note_words": prev_words,
        "journal_today": (VAULT_DIR / "Claude Journal" / f"{date}.md").is_file(),
        "plan_today": (VAULT_DIR / "Plans" / f"{date}_daily_plan.md").is_file(),
    }


def format_md(stats: dict) -> str:
    """One dashboard line for the Sitrep."""
    streak = stats["streak_days"]
    flame = "🔥" if streak >= 3 else "📅"
    parts = [
        f"{flame} **{streak}-day streak** · {stats['total_daily']} daily notes",
    ]
    if stats.get("prev_note_date"):
        parts.append(
            f"last note {stats['prev_note_date']} ({stats['prev_note_words']:,} words)"
        )
    scaffold = []
    if stats.get("plan_today"):
        scaffold.append("plan")
    if stats.get("journal_today"):
        scaffold.append("journal")
    if scaffold:
        parts.append(f"{' + '.join(scaffold)} ready")
    return " · ".join(parts)


if __name__ == "__main__":
    print(format_md(compute()))
