#!/usr/bin/env python3
"""
wrap_up.py — Evening orchestrator. Mirror of spin_up.py.

Phase 1 (gather):  commits since midnight, note section state, dirty repos
Phase 2 (write):   commits_today + tomorrows_top_3 + session log entries
Phase 3 (backup):  vault-backup.sh if available (build Phase 0 first)

Usage:
  python3 wrap_up.py              # full run
  python3 wrap_up.py --dry-run    # gather + print, no writes
  python3 wrap_up.py --no-backup  # skip vault push
  python3 wrap_up.py --force      # overwrite already-filled sections
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Tuple

import daily_note
import commit_summary

# ── Config ──────────────────────────────────────────────────────────────────

WORKSPACE    = Path.home() / "Code"
VAULT_BACKUP = WORKSPACE / "tools" / "vault-backup.sh"
RUNS_DIR     = Path.home() / ".wrap_up" / "runs"


# ── Logging to daily note ────────────────────────────────────────────────────

def _log(phase: str, msg: str, changes: list = None, files: list = None,
         next_steps: str = "", context: str = ""):
    """Append compact journal entry to claude_session_log. Never raises."""
    try:
        daily_note.append_session_log(
            focus=f"[{phase}] {msg}",
            changes=changes or [],
            files=files or [],
            next_steps=next_steps,
            context=context,
        )
    except Exception:
        pass


# ── Repo discovery ───────────────────────────────────────────────────────────

def _discover_repos() -> list:
    """Find git repos in ~/Code (root), ~/Code/*, and ~/Code/active/*."""
    repos = []
    # Include workspace root if it's a git repo
    if (WORKSPACE / ".git").exists():
        repos.append(WORKSPACE)
    # Scan top-level subdirs
    for p in WORKSPACE.iterdir():
        if p.is_dir() and (p / ".git").exists() and p != (WORKSPACE / "active"):
            repos.append(p)
    # Scan active/ subdirs
    active = WORKSPACE / "active"
    if active.exists():
        for p in active.iterdir():
            if p.is_dir() and (p / ".git").exists():
                repos.append(p)
    return repos


# ── Phase 1: Gather ──────────────────────────────────────────────────────────

def _gather_commits(repos: list) -> Tuple[dict, str]:
    print("  • commits...", end=" ", flush=True)
    try:
        summary = commit_summary.summarize_today(repos)
        total = summary["total_commits"]
        touched = summary["total_repos_touched"]
        print(f"{total} commit(s) in {touched} repo(s)")
        return summary, "ok"
    except Exception as e:
        print(f"failed: {e}")
        return {}, f"failed: {e}"


def _gather_note_state() -> Tuple[dict, str]:
    print("  • note state...", end=" ", flush=True)
    try:
        status = daily_note.section_status()
        filled = [k for k, v in status.items() if v == "filled"]
        empty  = [k for k, v in status.items() if v == "empty"]
        print(f"{len(filled)} filled, {len(empty)} empty")
        return {"filled": filled, "empty": empty}, "ok"
    except Exception as e:
        print(f"failed: {e}")
        return {}, f"failed: {e}"


def _gather_dirty(repos: list) -> Tuple[list, str]:
    print("  • dirty repos...", end=" ", flush=True)
    dirty = []
    for p in repos:
        try:
            out = subprocess.check_output(
                ["git", "status", "-s"], cwd=p,
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            if out:
                dirty.append({"name": p.name, "files": len(out.splitlines())})
        except subprocess.CalledProcessError:
            pass
    print(f"{len(dirty)} dirty")
    return dirty, "ok"


# ── Phase 2: Write ───────────────────────────────────────────────────────────

def _write_commits(summary: dict, force: bool) -> Tuple[bool, str]:
    print("  • commits_today...", end=" ", flush=True)
    if daily_note.section_status().get("commits_today") == "filled" and not force:
        print("skipped (already filled)")
        return True, "skipped"
    try:
        md = commit_summary.format_markdown(summary)
        daily_note.write_section("commits_today", md, actor="claude")
        print("written")
        return True, "written"
    except Exception as e:
        print(f"failed: {e}")
        return False, f"failed: {e}"


def _write_tomorrows_top3(commits: dict, dirty: list, force: bool) -> Tuple[bool, str]:
    print("  • tomorrows_top_3...", end=" ", flush=True)
    if daily_note.section_status().get("tomorrows_top_3") == "filled" and not force:
        print("skipped (already filled)")
        return True, "skipped"
    try:
        items = []

        # Dirty repos = unfinished → surface first
        for d in dirty[:2]:
            items.append(
                f"- [ ] Commit + clean `{d['name']}` ({d['files']} dirty file(s))"
            )

        # Most-active repo today → continue momentum
        if commits.get("repos"):
            by_count = sorted(
                commits["repos"].items(),
                key=lambda x: len(x[1]["commits"]),
                reverse=True,
            )
            for name, data in by_count[:1]:
                if not any(name in i for i in items):
                    latest = data["commits"][0]["subject"] if data["commits"] else "?"
                    items.append(f"- [ ] Continue `{name}` — last: {latest}")

        # Pad to 3
        while len(items) < 3:
            items.append("- [ ] (review open work efforts)")

        daily_note.write_section("tomorrows_top_3", "\n".join(items[:3]), actor="claude")
        print("written")
        return True, "written"
    except Exception as e:
        print(f"failed: {e}")
        return False, f"failed: {e}"


# ── EOD Summary ─────────────────────────────────────────────────────────────

def _write_eod_summary(commits: dict, dirty: list, note_state: dict) -> Tuple[bool, str]:
    """Generate and append an EOD summary to the daily note's Session Recap section."""
    print("  • eod_summary...", end=" ", flush=True)
    try:
        from pathlib import Path as _Path
        note_path = daily_note.daily_path()
        text = _Path(note_path).read_text()

        # Build in-the-lab decisions snippet from current note
        lab_text = daily_note.read_section("in_the_lab") or ""
        decisions = []
        for line in lab_text.splitlines():
            if line.startswith("**Decision:**"):
                decisions.append("- " + line.replace("**Decision:**", "").strip())
        decisions_block = "\n".join(decisions[:3]) if decisions else "- (none recorded today)"

        # Commits block
        total_commits = commits.get("total_commits", 0)
        total_repos   = commits.get("total_repos_touched", 0)
        commits_line  = (
            f"{total_commits} commit(s) across {total_repos} repo(s)"
            if total_commits else
            "0 commits captured (root repo scan pending fix)"
        )

        ts = datetime.now().strftime("%H:%M")
        weekday = datetime.now().strftime("%A, %B %-d")

        summary = f"""
## Session Recap (Timestamped)

**{weekday} — EOD Report**
*Generated by /wrap-up · {ts}*

---

### Key Decisions
{decisions_block}

---

### Workspace State

- **Commits today:** {commits_line}
- **Dirty repos:** {len(dirty)} — uncommitted work remaining
- **Note sections filled:** {len(note_state.get('filled', []))} / {len(note_state.get('filled', [])) + len(note_state.get('empty', []))}
- **Vault backup:** {"pending vault-backup.sh build (Phase 0)" if not VAULT_BACKUP.exists() else "pushed"}

---

### Tomorrow's First Move
"""
        # Surface top dirty repo or top 3 item
        if dirty:
            top = dirty[0]
            summary += f"Clean + commit `{top['name']}` ({top['files']} dirty file(s)).\n"
        elif commits.get("repos"):
            top_repo = max(commits["repos"].items(), key=lambda x: len(x[1]["commits"]))
            summary += f"Continue momentum in `{top_repo[0]}`.\n"
        else:
            summary += "Review open work efforts — `_work_efforts/devlog/index.md`.\n"

        if "## Session Recap" not in text:
            _Path(note_path).write_text(text.rstrip() + "\n" + summary)
        else:
            # Overwrite existing recap
            daily_note.write_section("session_recap", summary.split("## Session Recap (Timestamped)\n", 1)[-1], actor="claude")

        print("written")
        return True, "written"
    except Exception as e:
        print(f"failed: {e}")
        return False, f"failed: {e}"


# ── Phase 3: Vault backup ────────────────────────────────────────────────────

def _run_vault_backup() -> Tuple[bool, str]:
    print("  • vault backup...", end=" ", flush=True)
    if not VAULT_BACKUP.exists():
        print("skipped (vault-backup.sh not built yet — Phase 0)")
        return True, "skipped: script not found"
    try:
        result = subprocess.run(
            ["bash", str(VAULT_BACKUP)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            out = result.stdout.strip() or "ok"
            print(out)
            return True, out
        else:
            err = result.stderr.strip()
            print(f"failed: {err}")
            return False, err
    except subprocess.TimeoutExpired:
        print("timeout")
        return False, "timeout after 60s"
    except Exception as e:
        print(f"failed: {e}")
        return False, str(e)


# ── Run transcript ───────────────────────────────────────────────────────────

def _write_transcript(results: dict) -> str:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{ts}.json"
    path.write_text(json.dumps(results, indent=2))
    return str(path)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Evening wrap-up orchestrator.")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Gather only, no writes")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip vault backup")
    parser.add_argument("--force",     action="store_true",
                        help="Overwrite already-filled note sections")
    args = parser.parse_args()

    results = {
        "timestamp": datetime.now().isoformat(),
        "args": vars(args),
        "phases": {},
    }

    # Phase 1 ── Gather
    print("📥 Phase 1: Gathering state...")
    repos  = _discover_repos()
    commits, cs = _gather_commits(repos)
    note_state, ns = _gather_note_state()
    dirty,   ds = _gather_dirty(repos)

    results["phases"]["gather"] = {
        "commits":    {"status": cs, "total": commits.get("total_commits", 0)},
        "note_state": {"status": ns, "filled": len(note_state.get("filled", []))},
        "dirty":      {"status": ds, "count": len(dirty)},
    }

    # Phase 2 ── Write
    print("\n✍️  Phase 2: Writing daily note...")
    phase2 = {}
    if not args.dry_run:
        ok, s = _write_commits(commits, args.force)
        phase2["commits_today"] = {"ok": ok, "status": s}

        ok, s = _write_tomorrows_top3(commits, dirty, args.force)
        phase2["tomorrows_top_3"] = {"ok": ok, "status": s}

        ok, s = _write_eod_summary(commits, dirty, note_state)
        phase2["eod_summary"] = {"ok": ok, "status": s}
    else:
        print("  (dry-run: skipping writes)")

    results["phases"]["write"] = phase2

    # Phase 3 ── Backup
    phase3 = {}
    vault_status = "skipped"
    if not args.no_backup and not args.dry_run:
        print("\n🔐 Phase 3: Vault backup...")
        ok, s = _run_vault_backup()
        phase3["vault_backup"] = {"ok": ok, "status": s}
        vault_status = s

    results["phases"]["backup"] = phase3

    # Single consolidated journal entry
    total_c = commits.get("total_commits", 0)
    touched = commits.get("total_repos_touched", 0)
    written_sections = [k for k, v in phase2.items() if v.get("status") == "written"]
    skipped_sections = [k for k, v in phase2.items() if v.get("status") == "skipped"]
    dirty_names = [d["name"] for d in dirty[:5]]

    _log("wrap-up",
         f"{total_c} commits · {len(dirty)} dirty · vault: {vault_status}",
         changes=written_sections if written_sections else None,
         files=[f"_experiments/SimpleAgentOS/wrap_up.py"],
         next_steps="Phase 0: build tools/vault-backup.sh" if not VAULT_BACKUP.exists() else "",
         context=f"Sections skipped (filled): {', '.join(skipped_sections)}\nDirty repos: {', '.join(dirty_names)}" if dirty_names else ""
    )

    # Summary
    print("\n" + "=" * 60)
    print(f"✅  Wrap-up complete at {datetime.now().strftime('%H:%M:%S')}")
    print(f"📊  Commits today : {commits.get('total_commits', 0)} "
          f"across {commits.get('total_repos_touched', 0)} repo(s)")
    print(f"🧹  Dirty repos   : {len(dirty)}")
    print(f"📋  Transcript    : {_write_transcript(results)}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
