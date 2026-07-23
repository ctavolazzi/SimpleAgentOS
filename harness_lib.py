"""
harness_lib.py — Shared helpers for vault_commit, git_audit, fill_sections.

Three functions only. Lifted from wrap_up._discover_repos to avoid importing
wrap_up (and all its dependencies) just for repo discovery.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path.home() / "Code"


def discover_repos(workspace: Path = WORKSPACE) -> list[Path]:
    """Return git repo roots under workspace: root + top-level dirs + active/*."""
    repos: list[Path] = []
    if (workspace / ".git").exists():
        repos.append(workspace)
    for p in workspace.iterdir():
        if p.is_dir() and (p / ".git").exists() and p.name != "active":
            repos.append(p)
    active = workspace / "active"
    if active.exists():
        for p in active.iterdir():
            if p.is_dir() and (p / ".git").exists():
                repos.append(p)
    return repos


def classify_mtime(path: Path, threshold_hours: int = 24) -> str:
    """Return 'wip' if path touched within threshold_hours, else 'stale'."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "stale"
    age_hours = (datetime.now(timezone.utc).timestamp() - mtime) / 3600
    return "wip" if age_hours <= threshold_hours else "stale"


def iso_now() -> str:
    """ISO 8601 timestamp, second precision, local time."""
    return datetime.now().isoformat(timespec="seconds")
