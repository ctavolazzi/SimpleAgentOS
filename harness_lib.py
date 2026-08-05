"""
harness_lib.py — Shared helpers for vault_commit, git_audit, fill_sections.

Stdlib only, by design. This is the lowest layer: daily_note_update imports it
for repo discovery, so it must not import daily_note_update back.

Repo discovery is the canonical implementation for the whole harness. Three
copies of it used to exist (here, wrap_up._discover_repos, and
daily_note_update.find_repos) and they disagreed; the other two now delegate.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path.home() / "Code"

# Never descend into these while hunting for repos. Two groups:
#   - build/dependency output, which is huge and never interesting
#   - vendored subtrees, which DO contain real .git dirs (third-party clones,
#     LaTeX template packs, MCP sample repos) but are not the user's work.
#     Without these, a depth-limited walk of ~/Code surfaces ~100 vendored
#     clones and every consumer that reports "dirty repos" drowns in them.
SKIP_DIRS = {
    # build / dependency output
    "node_modules", ".venv", "venv", "env", ".env", "dist", "build",
    ".next", ".nuxt", ".cache", "__pycache__", ".mypy_cache", ".pytest_cache",
    "vendor", "target", ".gradle", ".svelte-kit", "coverage", "site-packages",
    # throwaway repo fixtures, not real work
    "test-projects", "test-fixtures", "mcp-jungle-gym",
    # vendored / archived subtrees full of third-party .git dirs
    "_external", "_integrations", "_realms", "_references", "_work_efforts",
    "templates", "templates_exploration", "standalone", "archived",
    "experiments", "lib",
}

# Directory-name prefixes pruned the same way (scratch + worktree conventions).
SKIP_PREFIXES = ("_temp_", "_worktree_")

# Directories that carry a .git of their own but are scan CONTAINERS, not work
# repos. ~/Code/active is an accidental `git init` with zero commits sitting
# above ~40 real projects: descend into it, but never report it as a repo.
# Including it would make git_audit read all of active/ as untracked.
CONTAINER_DIRS = {"active"}

MAX_DEPTH = 4  # relative to the scan base


def _prune(dirnames: list[str]) -> list[str]:
    """Filter a walk's dirnames in place-able form: drop skips and dotdirs."""
    return [
        d for d in dirnames
        if d not in SKIP_DIRS
        and not d.startswith(".")
        and not d.startswith(SKIP_PREFIXES)
    ]


def walk_repos(base: Path, max_depth: int = MAX_DEPTH) -> list[Path]:
    """Return every git repo root under `base`, pruning vendored subtrees.

    Descends PAST a found repo so genuinely nested independent projects are
    still caught (~/Code/Teleport-Massive-HQ/white-rabbit-debugger), which a
    stop-at-first-repo walk would miss. The SKIP_DIRS list is what keeps that
    from also dragging in vendored clones.

    `base` itself is returned if it is a repo; container dirs never are.
    """
    base = Path(os.path.realpath(os.path.expanduser(str(base))))
    if not base.is_dir():
        return []

    seen: set[tuple] = set()  # (st_dev, st_ino) — collapses symlinks/case dupes
    repos: list[Path] = []

    for dirpath, dirnames, _files in os.walk(base):
        depth = dirpath[len(str(base)):].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = _prune(dirnames)

        here = Path(dirpath)
        if not (here / ".git").exists():
            continue
        if here != base and here.name in CONTAINER_DIRS:
            continue  # descend into it, but do not report it
        try:
            st = here.stat()
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if key not in seen:
            seen.add(key)
            repos.append(here)

    return repos


def discover_repos(workspace: Path = WORKSPACE) -> list[Path]:
    """Return git repo roots under workspace.

    Was a two-level scan (root + top-level dirs + active/*), which could not
    see ~/Code/_experiments/SimpleAgentOS — the harness's own repo — so every
    consumer silently dropped the harness's commits from its tally.
    """
    return walk_repos(Path(workspace))


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
