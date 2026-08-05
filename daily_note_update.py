#!/usr/bin/env python3
"""
daily_note_update.py: Roll the whole day's real changes into today's daily note.

Unlike log_changes.py (which logs only what you type into one invocation), this
scans every git repo you actually own and auto-discovers what changed *today*,
across every window, session, and codebase, because it reads the filesystem,
not the current chat. Run it from anywhere.

What it captures (see --help for scope flags):
  - COMMITTED: commits you authored today, per repo (subjects + files touched)
  - WIP:       repos with uncommitted changes touched today (not yet committed)
  - WORDS:     words written into the daily note and every file wired to it,
               plus an HTML dashboard (heatmap, trend, word cloud) that opens
               in the browser

Ownership filter (the important part):
  A repo only counts if you have EVER authored a commit in it. That single test
  drops every vendored clone (llama.cpp, firecrawl, claude-cookbooks, AI-Scientist,
  awesome-*, …) automatically, so the roll-up stays signal, not noise.

Usage:
  python3 daily_note_update.py                 # scan + write + open dashboard
  python3 daily_note_update.py --dry-run       # print the entry, write nothing
  python3 daily_note_update.py --no-wip        # committed work only
  python3 daily_note_update.py --no-open       # build the dashboard, don't open it
  python3 daily_note_update.py --no-words      # skip word counting entirely
  python3 daily_note_update.py --root ~/other  # scan a different root (repeatable)
  python3 daily_note_update.py --focus "..."   # override the headline

Exit codes:
  0 = written (or dry-run printed)
  1 = error (note missing/unwritable, bad args)
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import daily_note
import harness_lib

# Word counting is additive: if the modules are missing or the vault is
# unreachable, the git roll-up still runs. It is never allowed to be the
# reason a day's changes go unlogged.
try:
    import word_count
except Exception:
    word_count = None
try:
    import wordcount_dashboard
except Exception:
    wordcount_dashboard = None

# Roots scanned by default. macOS is case-insensitive, so ~/Code and ~/code
# resolve to the same inode; realpath-dedup below collapses them.
DEFAULT_ROOTS = ["~/Code", "~/code"]

# Author substring matched against git's "Name <email>". The email local-part
# is the most stable handle. Override with --author.
DEFAULT_AUTHOR = "ctavolazzi"

# Re-exported from harness_lib so there is one skip list, not two that drift.
# Kept as module attributes because external callers reference them by name.
SKIP_DIRS = harness_lib.SKIP_DIRS
MAX_DEPTH = harness_lib.MAX_DEPTH


def _git(repo, *args, timeout=15):
    """Run a git command in repo, return stripped stdout ('' on any failure)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def find_repos(roots):
    """Walk roots and return every git repo dir, pruning vendored subtrees.

    Delegates to harness_lib.walk_repos, which is the single implementation
    for the harness. This function used to carry its own copy; keeping three
    walkers in sync failed in the obvious way, so the other two now defer.

    Still returns realpath STRINGS (not Path) — callers pass these straight to
    `git -C`.
    """
    seen = set()   # (st_dev, st_ino) — collapses ~/Code vs ~/code on macOS
    repos = []
    for root in roots:
        base = os.path.realpath(os.path.expanduser(root))
        if not os.path.isdir(base):
            continue
        for repo in harness_lib.walk_repos(base):
            try:
                st = repo.stat()
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key not in seen:
                seen.add(key)
                repos.append(str(repo))
    return repos


def is_owned(repo, author):
    """True if `author` has ever authored a commit here (drops vendored clones)."""
    return bool(_git(repo, "log", "-1", "--author", author, "--format=%H"))


def commits_today(repo, author, since):
    """Return list of commit subjects authored by `author` since `since`."""
    raw = _git(repo, "log", f"--since={since}", "--author", author,
               "--no-merges", "--format=%s")
    return [line for line in raw.splitlines() if line.strip()]


def files_touched_today(repo, author, since):
    """Count of unique files touched by today's commits."""
    raw = _git(repo, "log", f"--since={since}", "--author", author,
               "--no-merges", "--name-only", "--format=")
    return len({line for line in raw.splitlines() if line.strip()})


def wip_status(repo, midnight_epoch):
    """(changed_file_count, touched_today) for uncommitted work.

    touched_today is True if any changed/untracked file was modified since
    midnight. That's what distinguishes 'worked on today but not committed'
    from a repo that's just been sitting dirty for weeks.
    """
    raw = _git(repo, "status", "--porcelain")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return 0, False
    touched_today = False
    for ln in lines:
        path = ln[3:] if len(ln) > 3 else ln
        if " -> " in path:  # rename: take the destination
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        try:
            if os.path.getmtime(os.path.join(repo, path)) >= midnight_epoch:
                touched_today = True
                break
        except OSError:
            continue
    return len(lines), touched_today


def scan(roots, author, since, midnight_epoch, include_wip):
    """Return (committed, wip) lists of per-repo dicts for owned repos."""
    committed, wip = [], []
    for repo in find_repos(roots):
        if not is_owned(repo, author):
            continue
        subjects = commits_today(repo, author, since)
        if subjects:
            committed.append({
                "name": os.path.basename(repo),
                "path": repo,
                "count": len(subjects),
                "subjects": subjects,
                "files": files_touched_today(repo, author, since),
            })
        elif include_wip:
            n, touched = wip_status(repo, midnight_epoch)
            if n and touched:
                wip.append({"name": os.path.basename(repo), "path": repo, "count": n})
    committed.sort(key=lambda r: r["count"], reverse=True)
    wip.sort(key=lambda r: r["count"], reverse=True)
    return committed, wip


def count_words(date=None, depth=2):
    """Word-count scan for the daily note's wagonwheel. None if unavailable."""
    if word_count is None:
        return None
    try:
        return word_count.scan_day(date, max_depth=depth)
    except Exception as e:
        print(f"  (word count skipped: {e})", file=sys.stderr)
        return None


def build_dashboard(args, words):
    """Build the HTML dashboard and open it. Never fatal — the note is written."""
    if args.no_words or args.no_dashboard or words is None:
        return None
    if wordcount_dashboard is None:
        print("  (dashboard skipped: wordcount_dashboard.py not importable)",
              file=sys.stderr)
        return None
    try:
        result = wordcount_dashboard.build(
            date=args.date, open_browser=not args.no_open, max_depth=args.depth)
    except Exception as e:
        print(f"  (dashboard failed: {e})", file=sys.stderr)
        return None
    verb = "opened" if not args.no_open else "written"
    print(f"dashboard {verb}: {result['path']}")
    return result


def render(committed, wip, focus_override=None, words=None):
    """Build (focus, changes, context) for daily_note.append_session_log()."""
    total_commits = sum(r["count"] for r in committed)
    n_c, n_w = len(committed), len(wip)

    if focus_override:
        focus = focus_override
    elif not committed and not wip and not (words and words["words_written"]):
        focus = "No committed or WIP changes detected today"
    else:
        parts = []
        if n_c:
            parts.append(f"{total_commits} commit(s) in {n_c} repo(s)")
        if n_w:
            parts.append(f"{n_w} repo(s) with WIP")
        if words and words["words_written"]:
            parts.append(f"{words['words_written']:,} words written")
        focus = "Daily changes: " + ", ".join(parts)

    changes = []
    if words:
        changes.append(
            f"**Words** - {words['words_written']:,} written "
            f"({words['prose_written']:,} prose + {words['code_written']:,} code) "
            f"across {words['files_written']} file(s): "
            f"{words['files_linked_fresh']} wired to the note, "
            f"{words['files_unlinked_fresh']} unlinked. "
            f"Daily note itself {words['daily_note_words']:,} words."
        )
    for r in committed:
        preview = "; ".join(r["subjects"][:3])
        if r["count"] > 3:
            preview += f"; +{r['count'] - 3} more"
        changes.append(f"**{r['name']}** - {r['count']} commit(s), "
                       f"{r['files']} file(s): {preview}")
    for r in wip:
        changes.append(f"**{r['name']}** - WIP: {r['count']} uncommitted file(s), "
                       f"not committed today")

    # Full per-repo commit subjects go in the foldable context block.
    ctx_lines = []
    for r in committed:
        ctx_lines.append(f"{r['name']} ({r['path']}):")
        ctx_lines.extend(f"  - {s}" for s in r["subjects"])
    if words and words["files"]:
        if ctx_lines:
            ctx_lines.append("")
        ctx_lines.append("Words by file (● wired · ○ unlinked · · earlier):")
        badge = {"linked_fresh": "●", "unlinked_fresh": "○", "linked_carried": "·"}
        for rec in words["files"][:15]:
            ctx_lines.append(f"  {badge[rec['bucket']]} {rec['total']:>6,}  {rec['rel']}")
    context = "\n".join(ctx_lines)

    return focus, changes, context


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Roll today's changes across all owned repos into the daily note")
    parser.add_argument("--root", action="append", dest="roots",
                        help="root dir to scan (repeatable; default ~/Code)")
    parser.add_argument("--author", default=DEFAULT_AUTHOR,
                        help=f"git author substring (default {DEFAULT_AUTHOR!r})")
    parser.add_argument("--since", default=None,
                        help="git --since value (default: today 00:00 local)")
    parser.add_argument("--no-wip", action="store_true",
                        help="committed work only; skip uncommitted WIP")
    parser.add_argument("--focus", default=None, help="override the headline")
    parser.add_argument("--date", default=None, help="target note date YYYY-MM-DD")
    parser.add_argument("--actor", default="claude", help="actor tag (default claude)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the rendered entry, write nothing")
    parser.add_argument("--no-words", action="store_true",
                        help="skip word counting and the dashboard entirely")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="count words, but don't build the HTML dashboard")
    parser.add_argument("--no-open", action="store_true",
                        help="build the dashboard but don't open the browser")
    parser.add_argument("--dashboard", action="store_true",
                        help="build (and open) the dashboard even on --dry-run")
    parser.add_argument("--depth", type=int, default=2,
                        help="wikilink hops to follow from the daily note (default 2)")
    args = parser.parse_args()

    roots = args.roots or DEFAULT_ROOTS
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = args.since or midnight.strftime("%Y-%m-%d 00:00:00")
    midnight_epoch = midnight.timestamp()

    committed, wip = scan(roots, args.author, since, midnight_epoch,
                          include_wip=not args.no_wip)
    words = None if args.no_words else count_words(args.date, args.depth)
    focus, changes, context = render(committed, wip, args.focus, words)

    if args.dry_run:
        print(f"focus:   {focus}\n")
        print("changes:")
        for c in changes:
            print(f"  - {c}")
        if context:
            print("\ncontext (foldable):")
            for line in context.splitlines():
                print(f"  {line}")
        print(f"\n[dry-run] {len(committed)} committed repo(s), "
              f"{len(wip)} WIP repo(s): nothing written")
        if args.dashboard:
            build_dashboard(args, words)
        elif words:
            print("[dry-run] dashboard skipped — pass --dashboard to build it")
        return 0

    try:
        # One roll-up per day, replaced in place. Without this key every
        # invocation appends its own copy, and because the word counter
        # counts the note itself, each copy inflates the next one's total.
        result = daily_note.append_session_log(
            focus=focus, changes=changes, next_steps="",
            actor=args.actor, date=args.date, files=[], context=context,
            dedupe_key=f"rollup:{args.date or datetime.now().strftime('%Y-%m-%d')}",
        )
    except PermissionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    tail = ""
    if words:
        tail = f", {words['words_written']:,} words"
    print(f"logged: {result['section']} ({result['timestamp']}) - "
          f"{len(committed)} committed, {len(wip)} WIP{tail}")

    build_dashboard(args, words)
    return 0


if __name__ == "__main__":
    sys.exit(main())
