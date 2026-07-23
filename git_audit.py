"""
git_audit.py — Categorize ~/Code dirty paths as WIP vs. stale.

Clears preflight warning E2 (dirty git workspace).
Uses `git ls-files --others --exclude-standard` (respects .gitignore).

Usage:
  python3 git_audit.py                          # compact console report
  python3 git_audit.py --json                   # raw structured output
  python3 git_audit.py --write-report           # write to Vault/Audits/
  python3 git_audit.py --threshold-hours 48     # custom WIP window
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import atomic_io
import harness_lib

VAULT_DIR   = Path.home() / "Documents" / "Personal-Remote-Vault"
AUDITS_DIR  = VAULT_DIR / "Audits"


# ── Dirty file classifier ─────────────────────────────────────────────────────

def classify_dirty_path(repo: Path, porcelain_line: str, threshold_hours: int) -> dict:
    """Parse one `git status --porcelain` line and classify it."""
    xy     = porcelain_line[:2]
    fpath  = porcelain_line[3:].strip().strip('"')
    abs_path = repo / fpath

    age_class = harness_lib.classify_mtime(abs_path, threshold_hours)

    try:
        age_hours = round(
            (datetime.now().timestamp() - abs_path.stat().st_mtime) / 3600, 1
        )
    except OSError:
        age_hours = -1

    # Suggest action based on classification
    if xy.startswith("?"):
        action = "review → stage or .gitignore" if age_class == "wip" else "consider .gitignore"
    elif age_class == "wip":
        action = "stage + commit"
    else:
        action = "stale — consider committing or reverting"

    return {
        "path": fpath,
        "xy": xy.strip(),
        "age_hours": age_hours,
        "class": age_class,
        "suggested_action": action,
    }


def _get_dirty_lines(repo: Path) -> list[str]:
    """Return porcelain lines for a repo. Respects .gitignore via ls-files."""
    lines = []
    try:
        # Modified/staged/deleted tracked files
        tracked = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=repo, text=True, stderr=subprocess.DEVNULL
        ).strip()
        for line in tracked.splitlines():
            xy = line[:2]
            if not xy.startswith("?"):
                lines.append(line)
    except subprocess.CalledProcessError:
        pass

    try:
        # Untracked files (gitignore-aware)
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=repo, text=True, stderr=subprocess.DEVNULL
        ).strip()
        for f in untracked.splitlines():
            if f.strip():
                lines.append(f"?? {f.strip()}")
    except subprocess.CalledProcessError:
        pass

    return lines


# ── Core API ─────────────────────────────────────────────────────────────────

def audit_workspace(
    workspace: Path | None = None,
    wip_threshold_hours: int = 24,
) -> dict:
    """
    Audit all repos in workspace for dirty paths.

    Returns:
      {repos: [{name, path, dirty_files: [{path,status,age_hours,class,suggested_action}]}],
       totals: {wip, stale, repos_dirty},
       generated_at}
    """
    ws = workspace or harness_lib.WORKSPACE
    repos = harness_lib.discover_repos(ws)

    result = {
        "repos": [],
        "totals": {"wip": 0, "stale": 0, "repos_dirty": 0},
        "generated_at": harness_lib.iso_now(),
    }

    for repo in sorted(repos, key=lambda p: p.name.lower()):
        lines = _get_dirty_lines(repo)
        if not lines:
            continue

        dirty_files = [
            classify_dirty_path(repo, line, wip_threshold_hours)
            for line in lines
        ]

        result["repos"].append({
            "name": repo.name,
            "path": str(repo),
            "dirty_files": dirty_files,
        })
        result["totals"]["repos_dirty"] += 1
        result["totals"]["wip"]   += sum(1 for f in dirty_files if f["class"] == "wip")
        result["totals"]["stale"] += sum(1 for f in dirty_files if f["class"] == "stale")

    return result


def format_audit_compact(audit: dict) -> str:
    """One-line-per-repo summary."""
    t = audit["totals"]
    lines = [
        f"Git Audit — {audit['generated_at']}",
        f"Repos dirty: {t['repos_dirty']}  WIP: {t['wip']}  Stale: {t['stale']}\n",
    ]
    for repo in audit["repos"]:
        wip   = sum(1 for f in repo["dirty_files"] if f["class"] == "wip")
        stale = sum(1 for f in repo["dirty_files"] if f["class"] == "stale")
        lines.append(f"  {repo['name']:30s}  wip={wip}  stale={stale}")
    return "\n".join(lines)


def format_audit_md(audit: dict) -> str:
    """Full markdown report for vault."""
    t = audit["totals"]
    lines = [
        f"# Git Audit — {audit['generated_at']}\n",
        f"**Repos dirty:** {t['repos_dirty']}  "
        f"**WIP:** {t['wip']}  **Stale:** {t['stale']}\n",
        "---\n",
    ]
    for repo in audit["repos"]:
        lines.append(f"## {repo['name']}\n")
        lines.append(f"`{repo['path']}`\n")
        lines.append("| File | Status | Age (h) | Class | Action |")
        lines.append("|---|---|---|---|---|")
        for f in repo["dirty_files"]:
            lines.append(
                f"| `{f['path']}` | `{f['xy']}` | {f['age_hours']} "
                f"| {f['class']} | {f['suggested_action']} |"
            )
        lines.append("")
    return "\n".join(lines)


def write_report(audit: dict) -> Path:
    """Write markdown report to Vault/Audits/YYYY-MM-DD-git-audit.md."""
    AUDITS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = AUDITS_DIR / f"{date_str}-git-audit.md"
    atomic_io.vault_write(path, format_audit_md(audit))

    # Append wikilink to today's session log
    try:
        import daily_note
        rel = f"Audits/{date_str}-git-audit"
        daily_note.append_session_log(
            focus="git_audit: wrote workspace audit",
            files=[f"[[{rel}]]"],
            next_steps="Review stale paths; add to .gitignore or commit.",
        )
    except Exception:
        pass

    return path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Audit ~/Code dirty paths.")
    parser.add_argument("--threshold-hours", type=int, default=24,
                        help="Hours before a file is considered stale (default: 24).")
    parser.add_argument("--write-report", action="store_true",
                        help="Write full report to Vault/Audits/.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Output raw JSON.")
    args = parser.parse_args()

    audit = audit_workspace(wip_threshold_hours=args.threshold_hours)

    if args.write_report:
        path = write_report(audit)
        print(f"Report written: {path}")

    if args.as_json:
        print(json.dumps(audit, indent=2))
    else:
        print(format_audit_compact(audit))


if __name__ == "__main__":
    main()
