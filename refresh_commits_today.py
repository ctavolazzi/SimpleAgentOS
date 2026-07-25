#!/usr/bin/env python3
"""
refresh_commits_today.py — Rebuild the daily note "Commits Today" section from git.

Fills the gap between spin_up (morning provisional tally) and wrap_up (EOD
tally). Anything committed in between used to leave the section flat wrong all
day.

Idempotent by construction: it regenerates the entire section from current git
state rather than appending the commit that just happened. Running it twice, or
after a commit that failed, produces the same correct content.

Two entry points:
  - manual:    python3 refresh_commits_today.py [--date YYYY-MM-DD] [--dry-run]
  - automatic: the Claude Code PostToolUse hook at
               ~/.claude/hooks/commits-today-refresh.sh, which fires after any
               `git commit` / `git push` run through the Bash tool.

Exit codes: 0 on success or a benign skip, 1 on real failure. The hook wrapper
converts a 1 into a user-visible warning and still exits 0 itself, so a broken
refresh never breaks the tool call that triggered it.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import commit_summary
import daily_note
import daily_note_update


def refresh(date: str | None = None, dry_run: bool = False) -> dict:
    """
    Rebuild commits_today for `date` (default today).

    Returns {status, commits, repos, [reason], [preview]}.
    status is one of: written, dry_run, skipped.
    """
    path = daily_note.daily_path(date)
    if not path.exists():
        # A commit before spin_up has run. Nothing to write into yet; spin_up
        # fills the section when it creates the note, and the next commit of
        # the day rebuilds it. Not an error.
        return {"status": "skipped", "reason": f"no daily note at {path}"}

    # daily_note_update.find_repos, not harness_lib.discover_repos: the latter
    # only looks at ~/Code, its top-level dirs, and active/*, so it cannot see
    # a repo like ~/Code/_experiments/SimpleAgentOS and silently drops commits
    # made there. find_repos walks to depth 4 and prunes vendored subtrees.
    # No ownership filter is needed here because summarize_today already
    # filters by author, so a vendored clone can only appear if the user
    # authored a commit in it today.
    repos = daily_note_update.find_repos(daily_note_update.DEFAULT_ROOTS)
    summary = commit_summary.summarize_today(repos, date=date)
    md = commit_summary.format_markdown(summary)
    ts = datetime.now().strftime("%H:%M")
    md += f"\n\n*Live tally, refreshed {ts} on commit. Wrap-up re-tallies at EOD.*"

    result = {
        "status": "dry_run" if dry_run else "written",
        "commits": summary.get("total_commits", 0),
        "repos": summary.get("total_repos_touched", 0),
    }
    if dry_run:
        result["preview"] = md
        return result

    daily_note.write_section("commits_today", md, actor="claude", date=date)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the daily note Commits Today section from git.")
    parser.add_argument("--date", help="Target note date (YYYY-MM-DD). Default: today.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the rendered section, write nothing.")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Machine-readable output.")
    args = parser.parse_args()

    try:
        result = refresh(date=args.date, dry_run=args.dry_run)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if args.as_json:
            print(json.dumps({"status": "error", "error": msg}))
        else:
            print(f"commits_today refresh failed. {msg}", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(result))
    elif result["status"] == "skipped":
        print(f"skipped: {result['reason']}")
    elif result["status"] == "dry_run":
        print(result["preview"])
    else:
        print(f"commits_today refreshed: {result['commits']} commit(s) "
              f"across {result['repos']} repo(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
