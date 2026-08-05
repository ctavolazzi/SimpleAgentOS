#!/usr/bin/env python3
"""
commit_summary.py — Aggregate today's git commits per repo.

Usage:
  python3 commit_summary.py --test          # scan ~/Code workspace
  python3 commit_summary.py --test --json   # raw JSON output
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def summarize_today(repo_paths: list, date: str = None) -> dict:
    """
    Scan each repo for commits by ctavolazzi on a given day.

    date: YYYY-MM-DD. Defaults to the current calendar day ("since midnight").
    Pass an explicit date when wrap-up runs after midnight so the tally covers
    the day being closed out, not the few minutes of the new one.

    Returns:
    {
        "repos": {
            "CivicOS": {
                "path": "/...",
                "commits": [{"sha": "abc1234", "subject": "...", "files": N}],
                "stat": "3 files changed, +12 -4"
            },
            ...
        },
        "total_commits": int,
        "total_repos_touched": int,
        "generated_at": "ISO string"
    }
    """
    result = {
        "repos": {},
        "total_commits": 0,
        "total_repos_touched": 0,
        "generated_at": datetime.now().isoformat(),
        "date": date or datetime.now().strftime("%Y-%m-%d"),
    }

    if date:
        since_args = [f"--since={date} 00:00", f"--until={date} 23:59:59"]
    else:
        since_args = ["--since=midnight"]

    for repo_path in repo_paths:
        p = Path(repo_path)
        if not (p / ".git").exists():
            continue

        try:
            log_out = subprocess.check_output(
                ["git", "log", *since_args, "--author=Christopher",
                 "--oneline", "--no-merges"],
                cwd=p, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except subprocess.CalledProcessError:
            continue

        if not log_out:
            continue

        commits = []
        for line in log_out.splitlines():
            line = line.strip()
            if not line:
                continue
            sha, _, subject = line.partition(" ")
            try:
                stat_out = subprocess.check_output(
                    ["git", "show", "--stat", "--format=", sha],
                    cwd=p, text=True, stderr=subprocess.DEVNULL
                ).strip()
                files_changed = 0
                for stat_line in stat_out.splitlines():
                    if "file" in stat_line and "changed" in stat_line:
                        try:
                            files_changed = int(stat_line.strip().split()[0])
                        except (ValueError, IndexError):
                            pass
            except subprocess.CalledProcessError:
                files_changed = 0

            commits.append({
                "sha": sha[:7],
                "subject": subject.strip(),
                "files": files_changed,
            })

        if not commits:
            continue

        # Overall diff stat for the day
        try:
            base_ref = f"HEAD@{{{date} 00:00}}" if date else "HEAD@{midnight}"
            diff_lines = subprocess.check_output(
                ["git", "diff", "--stat", base_ref, "HEAD"],
                cwd=p, text=True, stderr=subprocess.DEVNULL
            ).strip().splitlines()
            stat_summary = diff_lines[-1].strip() if diff_lines else ""
        except subprocess.CalledProcessError:
            stat_summary = ""

        result["repos"][p.name] = {
            "path": str(p),
            "commits": commits,
            "stat": stat_summary,
        }
        result["total_commits"] += len(commits)
        result["total_repos_touched"] += 1

    return result


def format_markdown(summary: dict) -> str:
    """Render commit summary as markdown for the daily note."""
    if not summary.get("total_commits"):
        return "*No commits today.*"

    lines = [
        f"**{summary['total_commits']} commit(s) across "
        f"{summary['total_repos_touched']} repo(s)**\n"
    ]

    for repo_name, data in summary["repos"].items():
        lines.append(f"### {repo_name}")
        for c in data["commits"]:
            files_note = f" · {c['files']} file(s)" if c["files"] else ""
            lines.append(f"- `{c['sha']}` {c['subject']}{files_note}")
        if data.get("stat"):
            lines.append(f"\n*{data['stat']}*")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Summarize today's commits.")
    parser.add_argument("--test", action="store_true",
                        help="Scan ~/Code workspace")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Output raw JSON instead of markdown")
    args = parser.parse_args()

    if args.test:
        import harness_lib
        summary = summarize_today(harness_lib.discover_repos())
        if args.as_json:
            print(json.dumps(summary, indent=2))
        else:
            print(format_markdown(summary))
    else:
        parser.print_help()
        sys.exit(1)
