"""
lab_report.py — Morning workbench snapshot for the "In the Lab" section.

Part of the daily-note harness. Pure local reads: git scanner output +
devlog tail + active work efforts. Answers "what's actually on the bench
right now?" before the day starts.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

CODE_ROOT = Path.home() / "Code"
DEVLOG = CODE_ROOT / "_work_efforts" / "devlog.md"
WE_DIR = CODE_ROOT / "_work_efforts" / "10-19_development"


def _devlog_headline() -> str:
    """Most recent markdown heading in the devlog — the last thing worked on."""
    if not DEVLOG.is_file():
        return ""
    try:
        lines = DEVLOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        m = re.match(r"^#{1,4}\s+(.+)$", line.strip())
        if m:
            return m.group(1).strip()
    return ""


def _active_work_efforts(limit: int = 3) -> list:
    """Most recently touched work-effort docs in 10-19_development."""
    if not WE_DIR.is_dir():
        return []
    docs = sorted(WE_DIR.glob("**/*.md"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in docs[:limit]:
        age_days = (datetime.now().timestamp() - p.stat().st_mtime) / 86400
        out.append({"name": p.stem, "age_days": round(age_days, 1)})
    return out


def build(repos: Optional[list] = None) -> dict:
    """
    Build the lab snapshot. `repos` is git_scanner.scan_workspace() output;
    pass it in to avoid a second scan (spin_up already has it).
    """
    repos = repos or []
    dirty = sorted((r for r in repos if r.get("dirty_files", 0) > 0),
                   key=lambda r: r.get("dirty_files", 0), reverse=True)
    ahead = sorted((r for r in repos if r.get("unpushed", 0) > 0),
                   key=lambda r: r.get("unpushed", 0), reverse=True)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hot_repos": [
            {"name": r["name"], "branch": r.get("branch", "?"),
             "dirty_files": r.get("dirty_files", 0),
             "last_msg": (r.get("last_commit_msg") or "")[:80]}
            for r in dirty[:5]
        ],
        "unpushed_repos": [
            {"name": r["name"], "unpushed": r.get("unpushed", 0)}
            for r in ahead[:5]
        ],
        "devlog_headline": _devlog_headline(),
        "work_efforts": _active_work_efforts(),
    }


def format_md(lab: dict) -> str:
    """Markdown block for the In the Lab section."""
    lines = []

    if lab.get("devlog_headline"):
        lines.append(f"**Last on the bench:** {lab['devlog_headline']}")
        lines.append("")

    hot = lab.get("hot_repos", [])
    if hot:
        lines.append("**Hot repos (uncommitted work):**")
        for r in hot:
            lines.append(
                f"- `{r['name']}` ({r['branch']}) — {r['dirty_files']} dirty "
                f"file(s) · last: *{r['last_msg']}*"
            )
        lines.append("")

    unpushed = lab.get("unpushed_repos", [])
    if unpushed:
        summary = " · ".join(f"`{r['name']}` +{r['unpushed']}" for r in unpushed)
        lines.append(f"**Unpushed commits:** {summary}")
        lines.append("")

    efforts = lab.get("work_efforts", [])
    if efforts:
        lines.append("**Recent work efforts:**")
        for e in efforts:
            lines.append(f"- {e['name']} *({e['age_days']}d ago)*")
        lines.append("")

    if not lines:
        return "*Bench is clean — nothing in flight.*"

    lines.append(f"*Snapshot at {lab.get('generated_at', '')} — refreshed each spin-up.*")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import git_scanner
    print(format_md(build(git_scanner.scan_workspace(CODE_ROOT))))
