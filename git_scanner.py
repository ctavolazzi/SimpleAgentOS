"""
git_scanner.py — Scan git repositories and report health.

Pure module. No LLM, no server dependency, no external packages.
Uses subprocess to call git CLI directly.

Usage:
    python git_scanner.py                      # scan default workspace
    python git_scanner.py /path/to/workspace   # scan specific directory
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


WORKSPACE_DIR = Path.home() / "Code"


def _git(repo_path: Path, *args: str, timeout: int = 10) -> Optional[str]:
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path)] + list(args),
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def scan_single_repo(repo_path: Path) -> dict:
    """Scan a single git repo and return structured health data."""
    info = {
        "name": repo_path.name,
        "path": str(repo_path),
        "branch": None,
        "dirty_files": 0,
        "unpushed": 0,
        "last_commit_date": None,
        "last_commit_msg": None,
        "remote_url": None,
        "health": "unknown",
    }

    # Branch
    info["branch"] = _git(repo_path, "branch", "--show-current") or "(detached)"

    # Dirty files
    status = _git(repo_path, "status", "--porcelain")
    if status is not None:
        info["dirty_files"] = len([l for l in status.splitlines() if l.strip()])

    # Last commit
    log_line = _git(repo_path, "log", "-1", "--format=%aI\t%s")
    if log_line and "\t" in log_line:
        date_str, msg = log_line.split("\t", 1)
        info["last_commit_date"] = date_str
        info["last_commit_msg"] = msg[:80]

    # Remote URL
    info["remote_url"] = _git(repo_path, "remote", "get-url", "origin")

    # Unpushed commits (only if remote tracking branch exists)
    if info["remote_url"]:
        count_str = _git(repo_path, "rev-list", "--count", "@{u}..HEAD")
        if count_str and count_str.isdigit():
            info["unpushed"] = int(count_str)

    # Health classification
    has_remote = info["remote_url"] is not None
    if not has_remote:
        info["health"] = "no-remote"
    elif info["dirty_files"] > 0:
        info["health"] = "dirty"
    elif info["unpushed"] > 0:
        info["health"] = "ahead"
    else:
        info["health"] = "clean"

    return info


def scan_workspace(workspace_dir: Optional[Path] = None) -> list[dict]:
    """
    Scan all git repos in the workspace.
    Checks top-level dirs and active/ subdirs.
    """
    workspace = workspace_dir or WORKSPACE_DIR
    results = []
    seen = set()

    # Top-level repos
    for child in sorted(workspace.iterdir()):
        if child.is_dir() and (child / ".git").exists():
            results.append(scan_single_repo(child))
            seen.add(child.name)

    # active/ subdirectory repos
    active_dir = workspace / "active"
    if active_dir.is_dir():
        for child in sorted(active_dir.iterdir()):
            if child.is_dir() and (child / ".git").exists():
                results.append(scan_single_repo(child))

    return results


def format_report_md(results: list[dict]) -> str:
    """Format scan results as a markdown table for daily note embedding."""
    if not results:
        return "*No repos found.*\n"

    # Health icons
    icons = {"clean": "✅", "dirty": "⚠️", "ahead": "🔼", "no-remote": "⚫", "unknown": "❓"}

    lines = [
        f"**Git Health Scan** — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "| Status | Repo | Branch | Dirty | Unpushed | Last Commit |",
        "|--------|------|--------|------:|--------:|-------------|",
    ]

    # Sort: dirty/ahead first, then clean
    priority = {"dirty": 0, "ahead": 1, "no-remote": 2, "unknown": 3, "clean": 4}
    sorted_results = sorted(results, key=lambda r: priority.get(r["health"], 5))

    for r in sorted_results:
        icon = icons.get(r["health"], "❓")
        msg = (r["last_commit_msg"] or "—")[:40]
        lines.append(
            f"| {icon} | {r['name']} | {r['branch']} | "
            f"{r['dirty_files']} | {r['unpushed']} | {msg} |"
        )

    # Summary
    dirty = sum(1 for r in results if r["health"] == "dirty")
    ahead = sum(1 for r in results if r["health"] == "ahead")
    clean = sum(1 for r in results if r["health"] == "clean")
    lines.append("")
    lines.append(f"**Summary:** {len(results)} repos — {clean} clean, {dirty} dirty, {ahead} ahead")

    return "\n".join(lines) + "\n"


def format_report_compact(results: list[dict]) -> str:
    """One-line-per-repo compact format for terminal output."""
    icons = {"clean": "●", "dirty": "▲", "ahead": "△", "no-remote": "○", "unknown": "?"}
    lines = []
    for r in results:
        icon = icons.get(r["health"], "?")
        extra = ""
        if r["dirty_files"] > 0:
            extra += f" [{r['dirty_files']} dirty]"
        if r["unpushed"] > 0:
            extra += f" [{r['unpushed']} unpushed]"
        lines.append(f"  {icon} {r['name']:30s} {r['branch'] or '?':15s}{extra}")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    workspace = Path(sys.argv[1]) if len(sys.argv) > 1 else WORKSPACE_DIR

    print(f"Scanning {workspace}...")
    results = scan_workspace(workspace)

    if "--json" in sys.argv:
        print(json.dumps(results, indent=2))
    elif "--md" in sys.argv:
        print(format_report_md(results))
    else:
        print(format_report_compact(results))
        print()
        dirty = sum(1 for r in results if r["health"] == "dirty")
        ahead = sum(1 for r in results if r["health"] == "ahead")
        clean = sum(1 for r in results if r["health"] == "clean")
        print(f"  {len(results)} repos: {clean} clean, {dirty} dirty, {ahead} ahead")
