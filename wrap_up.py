#!/usr/bin/env python3
"""
wrap_up.py — Evening orchestrator. Mirror of spin_up.py.

Phase 1 (gather):  commits since midnight, note section state, dirty repos,
                   words written across the daily note and everything wired to it
Phase 2 (write):   commits_today + tomorrows_top_3 + session log entries
Phase 3 (backup):  vault-backup.sh if available (build Phase 0 first)

Usage:
  python3 wrap_up.py                  # full run
  python3 wrap_up.py --dry-run        # gather + print, no writes
  python3 wrap_up.py --no-backup      # skip vault push
  python3 wrap_up.py --force          # overwrite already-filled sections
  python3 wrap_up.py --open-dashboard # open the word-count dashboard when done
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple

import daily_note
import commit_summary
import waft_workspace

# Additive: a word-count failure must never stop the day being closed out.
try:
    import word_count
except Exception:
    word_count = None
try:
    import wordcount_dashboard
except Exception:
    wordcount_dashboard = None

# ── Config ──────────────────────────────────────────────────────────────────

WORKSPACE    = Path.home() / "Code"
VAULT_BACKUP = WORKSPACE / "tools" / "vault-backup.sh"
RUNS_DIR     = Path.home() / ".wrap_up" / "runs"

# Text that spin-up leaves in the Hub/Journal scaffolds. If any of these
# survive to wrap-up, the sibling container file was never filled during the
# session — the failure mode that hid a full day's work on 2026-07-10.
PLACEHOLDER_MARKERS = (
    "To be populated",
    "(none yet)",
    "(pending)",
    "(current model)",
    "(check on startup)",
)


# ── Logical day ──────────────────────────────────────────────────────────────

def _resolve_date(cli_date: str = None) -> str:
    """
    Decide which day this wrap-up closes out.

    An evening ritual routinely runs after midnight. Closing out the NEW
    calendar day would tally zero commits and write into a note that doesn't
    exist yet (the 2026-07-19T00:xx problem). Resolution order:
      1. Explicit --date wins.
      2. Before 06:00, if yesterday's note exists and today's doesn't,
         wrap up yesterday — that's the day still being lived.
      3. Otherwise the current calendar day.
    """
    if cli_date:
        return cli_date
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour < 6:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if daily_note.exists(yesterday) and not daily_note.exists(today):
            print(f"🌙 After midnight — wrapping up {yesterday} (logical day)")
            return yesterday
    return today


# ── Logging to daily note ────────────────────────────────────────────────────

def _log(phase: str, msg: str, changes: list = None, files: list = None,
         next_steps: str = "", context: str = "", date: str = None):
    """Append compact journal entry to claude_session_log. Never raises.

    Keyed by phase and day: wrap-up is routinely run more than once (and
    sometimes in every open window at once), and each run should refresh its
    own entry rather than stack another copy beside it.
    """
    try:
        day = date or datetime.now().strftime("%Y-%m-%d")
        daily_note.append_session_log(
            focus=f"[{phase}] {msg}",
            changes=changes or [],
            files=files or [],
            next_steps=next_steps,
            context=context,
            date=date,
            dedupe_key=f"wrapup:{phase}:{day}",
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

def _gather_commits(repos: list, date: str = None) -> Tuple[dict, str]:
    print("  • commits...", end=" ", flush=True)
    try:
        summary = commit_summary.summarize_today(repos, date=date)
        total = summary["total_commits"]
        touched = summary["total_repos_touched"]
        print(f"{total} commit(s) in {touched} repo(s)")
        return summary, "ok"
    except Exception as e:
        print(f"failed: {e}")
        return {}, f"failed: {e}"


def _gather_note_state(date: str = None) -> Tuple[dict, str]:
    print("  • note state...", end=" ", flush=True)
    try:
        status = daily_note.section_status(date)
        filled = [k for k, v in status.items() if v == "filled"]
        empty  = [k for k, v in status.items() if v == "empty"]
        print(f"{len(filled)} filled, {len(empty)} empty")
        return {"filled": filled, "empty": empty}, "ok"
    except Exception as e:
        print(f"failed: {e}")
        return {}, f"failed: {e}"


def _gather_words(date: str = None) -> Tuple[dict, str]:
    """Words written into the daily note and every file wired to it."""
    print("  • words...", end=" ", flush=True)
    if word_count is None:
        print("skipped (word_count.py not importable)")
        return {}, "skipped: module unavailable"
    try:
        scan = word_count.scan_day(date)
        print(f"{scan['words_written']:,} word(s) across "
              f"{scan['files_written']} file(s)")
        return scan, "ok"
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


# ── Sibling-file completeness (Hub + Journal) ────────────────────────────────

def _check_sibling_files(date_str: str) -> list[str]:
    """Return human-readable warnings for Hub/Journal files that are still
    just spin-up placeholders at wrap-up time. Never raises."""
    warnings: list[str] = []
    vault = daily_note.VAULT_DIR
    targets = {
        "Hub":     vault / "Hubs" / f"{date_str}_hub.md",
        "Journal": vault / "Claude Journal" / f"{date_str}.md",
    }
    for label, path in targets.items():
        try:
            if not path.exists():
                warnings.append(f"{label} missing ({path.name})")
                continue
            text = path.read_text(encoding="utf-8")
            hits = sorted({m for m in PLACEHOLDER_MARKERS if m in text})
            # Journal: also flag an empty ## Notes section
            if label == "Journal":
                notes = daily_note._extract_section(text, "## Notes").strip()
                if not notes:
                    hits = sorted(set(hits) | {"empty ## Notes"})
            if hits:
                warnings.append(f"{label} still has placeholders: {', '.join(hits)}")
        except Exception as e:
            warnings.append(f"{label} check failed ({type(e).__name__})")
    return warnings


# ── Phase 2: Write ───────────────────────────────────────────────────────────

def _write_commits(summary: dict, force: bool, date: str = None) -> Tuple[bool, str]:
    print("  • commits_today...", end=" ", flush=True)
    # commits_today is an END-OF-DAY tally by definition. spin-up fills it in
    # the morning with a provisional "no commits yet" — which is stale (often
    # flat wrong) by evening. Wrap-up is the authority here, so it ALWAYS
    # refreshes rather than honoring the morning "filled" flag. (Historically
    # this skip left "0 commits today" on days that had commits — 2026-07-10.)
    try:
        md = commit_summary.format_markdown(summary)
        daily_note.write_section("commits_today", md, actor="claude", date=date)
        print("refreshed (EOD tally)")
        return True, "refreshed"
    except Exception as e:
        print(f"failed: {e}")
        return False, f"failed: {e}"


def _read_unchecked_top3(date: str = None) -> list[str]:
    """Return unchecked items from the prior day's ## Tomorrow's Top 3 section."""
    base = (datetime.strptime(date, "%Y-%m-%d") if date else datetime.now())
    yesterday = (base - timedelta(days=1)).strftime("%Y-%m-%d")
    note = daily_note.daily_path(yesterday)
    if not note.exists():
        return []
    try:
        text = note.read_text(encoding="utf-8")
        section = daily_note._extract_section(text, "## Tomorrow's Top 3")
        return [
            ln.strip()
            for ln in section.splitlines()
            if re.match(r"^-\s*\[ \]", ln.strip())
        ]
    except Exception:
        return []


def _write_tomorrows_top3(commits: dict, dirty: list, force: bool,
                          date: str = None) -> Tuple[bool, str]:
    print("  • tomorrows_top_3...", end=" ", flush=True)
    if daily_note.section_status(date).get("tomorrows_top_3") == "filled" and not force:
        print("skipped (already filled)")
        return True, "skipped"
    try:
        items: list[str] = []

        # Carry forward unchecked items from yesterday — quest continuity
        carried = _read_unchecked_top3(date)
        for item in carried[:2]:
            items.append(item + " *(carried)*")

        # Dirty repos = unfinished → surface after carries
        for d in dirty[:2]:
            candidate = f"- [ ] Commit + clean `{d['name']}` ({d['files']} dirty file(s))"
            if len(items) < 3:
                items.append(candidate)

        # Most-active repo today → continue momentum
        if commits.get("repos") and len(items) < 3:
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

        final_items = items[:3]

        # Auto-create WEs for tomorrow's top 3 — "tomorrow" relative to the
        # day being wrapped, not the wall clock (they differ after midnight)
        try:
            import we_factory
            base = (datetime.strptime(date, "%Y-%m-%d") if date else datetime.now())
            tomorrow = (base + timedelta(days=1)).strftime("%Y-%m-%d")
            we_factory.create_for_top3(final_items, date=tomorrow)
        except Exception as e:
            print(f"    (we_factory failed: {e})")

        daily_note.write_section("tomorrows_top_3", "\n".join(final_items),
                                 actor="claude", date=date)
        print("written")
        return True, "written"
    except Exception as e:
        print(f"failed: {e}")
        return False, f"failed: {e}"


# ── WAFT fitness ─────────────────────────────────────────────────────────────

def _award_quest_fitness(done_quests: list, date: str = None) -> None:
    """Award fitness delta for completed quests. Sentinel-file idempotent."""
    today = date or datetime.now().strftime("%Y-%m-%d")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = RUNS_DIR / f"fitness_{today}"

    if not done_quests:
        print("  • fitness: no completed quests")
        return
    if sentinel.exists():
        print("  • fitness: skipped (already awarded today)")
        return

    delta = min(len(done_quests) * 0.1, 1.0)
    try:
        sys.path.insert(0, str(waft_workspace.WAFT_PROJECT))
        from waft import BeingSystem
        bs = BeingSystem(waft_workspace.WAFT_PROJECT)
        being = bs.get_or_create_the_one()
        old = getattr(being, "fitness", 0.0)
        being.fitness = min(old + delta, 1.0)
        bs.save_being(being)
        sentinel.touch()
        print(f"  • fitness: +{delta:.1f} ({old:.2f} → {being.fitness:.2f}) · {len(done_quests)} quest(s)")
    except Exception as e:
        print(f"  • fitness: failed — {e}")


# ── EOD Summary ─────────────────────────────────────────────────────────────

def _build_dashboard(date: str, open_browser: bool) -> Tuple[bool, str]:
    """Regenerate the word-count dashboard so it is current after EOD."""
    print("  • wordcount dashboard...", end=" ", flush=True)
    if wordcount_dashboard is None:
        print("skipped (module unavailable)")
        return True, "skipped: module unavailable"
    try:
        result = wordcount_dashboard.build(date=date, open_browser=open_browser)
        print("opened" if open_browser else f"written → {result['path']}")
        return True, result["path"]
    except Exception as e:
        print(f"failed: {e}")
        return False, f"failed: {e}"


def _write_eod_summary(commits: dict, dirty: list, note_state: dict,
                       words: dict = None, date: str = None) -> Tuple[bool, str]:
    """Generate and append an EOD summary to the daily note's Session Recap section."""
    print("  • eod_summary...", end=" ", flush=True)
    try:
        # Build in-the-lab decisions snippet from current note
        lab_text = daily_note.read_section("in_the_lab", date=date) or ""
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

        # Words block — the daily note plus everything wired to it
        if words and word_count is not None:
            try:
                hist = word_count.history(30, end_date=date)
            except Exception:
                hist = None
            words_block = word_count.format_md(words, hist)
        else:
            words_block = "- (word count unavailable)"

        ts = datetime.now().strftime("%H:%M")
        label_dt = (datetime.strptime(date, "%Y-%m-%d") if date else datetime.now())
        weekday = label_dt.strftime("%A, %B %-d")

        summary = f"""
## Session Recap (Timestamped)

**{weekday} — EOD Report**
*Generated by /wrap-up · {ts}*

---

### Key Decisions
{decisions_block}

---

### Words Written

{words_block}

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

        # write_section self-heals if the header is missing from the note
        # (legacy schema, trimmed template) — no need to branch on that here.
        body = summary.split("## Session Recap (Timestamped)\n", 1)[-1]
        daily_note.write_section("session_recap", body, actor="claude", date=date)

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
    parser.add_argument("--date",      metavar="YYYY-MM-DD", default=None,
                        help="Day to close out (default: auto — before 06:00 "
                             "wraps the previous day if its note is the live one)")
    parser.add_argument("--open-dashboard", action="store_true",
                        help="Open the word-count dashboard in the browser "
                             "(it is regenerated either way)")
    args = parser.parse_args()

    date_str = _resolve_date(args.date)
    if not daily_note.exists(date_str):
        print(f"❌ No daily note for {date_str} — nothing to wrap up.")
        return 1

    results = {
        "timestamp": datetime.now().isoformat(),
        "date": date_str,
        "args": vars(args),
        "phases": {},
    }

    # Phase 1 ── Gather
    print(f"📥 Phase 1: Gathering state for {date_str}...")
    repos  = _discover_repos()
    commits, cs = _gather_commits(repos, date_str)
    note_state, ns = _gather_note_state(date_str)
    dirty,   ds = _gather_dirty(repos)
    words,   ws = _gather_words(date_str)

    results["phases"]["gather"] = {
        "commits":    {"status": cs, "total": commits.get("total_commits", 0)},
        "note_state": {"status": ns, "filled": len(note_state.get("filled", []))},
        "dirty":      {"status": ds, "count": len(dirty)},
        "words":      {"status": ws,
                       "written": words.get("words_written", 0),
                       "files": words.get("files_written", 0),
                       "in_scope": words.get("words_in_scope", 0)},
    }

    # Phase 2 ── Write
    print("\n✍️  Phase 2: Writing daily note...")
    phase2 = {}
    if not args.dry_run:
        ok, s = _write_commits(commits, args.force, date_str)
        phase2["commits_today"] = {"ok": ok, "status": s}

        ok, s = _write_tomorrows_top3(commits, dirty, args.force, date_str)
        phase2["tomorrows_top_3"] = {"ok": ok, "status": s}

        ok, s = _write_eod_summary(commits, dirty, note_state, words, date_str)
        phase2["eod_summary"] = {"ok": ok, "status": s}

        ok, s = _build_dashboard(date_str, args.open_dashboard)
        phase2["wordcount_dashboard"] = {"ok": ok, "status": s}

        # Write session-end entry to being journal
        total_c = commits.get("total_commits", 0)
        waft_data = waft_workspace.fetch()
        quests = waft_data.get("quests", [])
        done = [q for q in quests if q.get("complete")]
        incomplete = [q for q in quests if not q.get("complete")]
        details = (
            [f"DONE: {q['task'][:60]}" for q in done]
            + [f"OPEN: {q['task'][:60]}" for q in incomplete]
            + [f"commits today: {total_c}"]
        )
        waft_workspace.write_being_journal_entry(
            "session_end",
            f"wrap_up · {len(done)}/{len(quests)} quests complete · {total_c} commit(s)",
            details=details,
            being_state=waft_data.get("being"),
        )
        _award_quest_fitness(done, date_str)
    else:
        print("  (dry-run: skipping writes)")

    results["phases"]["write"] = phase2

    # Phase 3 ── Lock daily plan
    phase3 = {}
    if not args.dry_run:
        print("\n🔒 Phase 3: Lock daily plan...")
        try:
            import daily_plan as dp
            lock_result = dp.lock(date_str)
            if lock_result.get("ok"):
                rollover_count = lock_result.get("rollover_count", 0)
                print(f"  ✓ plan locked — {rollover_count} items to roll over tomorrow")
            else:
                err = lock_result.get("error", "unknown")
                print(f"  · plan lock skipped: {err}")
            phase3["plan_lock"] = lock_result
        except Exception as e:
            print(f"  · plan lock failed ({type(e).__name__}): {e}")
            phase3["plan_lock"] = {"ok": False, "error": str(e)}

    # Phase 4 ── Backup
    vault_status = "skipped"
    if not args.no_backup and not args.dry_run:
        print("\n🔐 Phase 4: Vault backup...")
        ok, s = _run_vault_backup()
        phase3["vault_backup"] = {"ok": ok, "status": s}
        vault_status = s

    results["phases"]["backup"] = phase3

    # Phase 5 ── Wheel integrity (fail-loud). Supersedes the old sibling-file
    # check: verifies frontmatter links resolve, containers are filled, spoke
    # reciprocity holds, the parent chain reaches the index, and nothing is
    # orphaned. This is the guard against 2026-07-10 — a day's work half-wired
    # into the vault. Non-fatal (writes already happened) but LOUD.
    today = date_str
    wheel_errors: list[str] = []
    wheel_warnings: list[str] = []
    try:
        import wheel_check
        wr = wheel_check.check(today)
        wheel_errors, wheel_warnings = wr.errors, wr.warnings
        results["phases"]["wheel_check"] = {
            "errors": wheel_errors, "warnings": wheel_warnings, "broken": wr.broken,
        }
        if wr.broken:
            print(f"\n❌ Phase 5: WHEEL BROKEN — {len(wheel_errors)} error(s)")
            for e in wheel_errors:
                print(f"  ✗ {e}")
            for w in wheel_warnings:
                print(f"  ⚠ {w}")
            print("  → fix the above or run /wagonwheel, then `python3 wheel_check.py`")
        elif wheel_warnings:
            print(f"\n⚠️  Phase 5: wheel intact, {len(wheel_warnings)} warning(s)")
            for w in wheel_warnings:
                print(f"  ⚠ {w}")
        else:
            print("\n✅ Phase 5: wheel intact — fully wired, no dangling links, no orphans")
    except Exception as e:
        print(f"\n· Phase 5: wheel check skipped ({type(e).__name__}): {e}")
        # Fall back to the narrow sibling-file guard so we never silently pass
        sib = _check_sibling_files(today)
        results["phases"]["sibling_files"] = {"warnings": sib}
        for w in sib:
            print(f"  ⚠ {w}")
        wheel_warnings = sib

    # Single consolidated journal entry
    total_c = commits.get("total_commits", 0)
    touched = commits.get("total_repos_touched", 0)
    written_sections = [k for k, v in phase2.items() if v.get("status") in ("written", "refreshed")]
    skipped_sections = [k for k, v in phase2.items() if v.get("status") == "skipped"]
    dirty_names = [d["name"] for d in dirty[:5]]

    context_lines = []
    if skipped_sections:
        context_lines.append(f"Sections skipped (filled): {', '.join(skipped_sections)}")
    if dirty_names:
        context_lines.append(f"Dirty repos: {', '.join(dirty_names)}")
    if wheel_errors:
        context_lines.append("❌ WHEEL BROKEN: " + "; ".join(wheel_errors))
    if wheel_warnings:
        context_lines.append("⚠️ Wheel warnings: " + "; ".join(wheel_warnings))

    words_frag = (f" · {words['words_written']:,} words" if words else "")
    _log("wrap-up",
         f"{total_c} commits · {len(dirty)} dirty{words_frag} · vault: {vault_status}",
         changes=written_sections if written_sections else None,
         files=[f"_experiments/SimpleAgentOS/wrap_up.py"],
         next_steps="Phase 0: build tools/vault-backup.sh" if not VAULT_BACKUP.exists() else "",
         context="\n".join(context_lines),
         date=date_str,
    )

    # Summary
    print("\n" + "=" * 60)
    print(f"✅  Wrap-up complete at {datetime.now().strftime('%H:%M:%S')}")
    print(f"📊  Commits today : {commits.get('total_commits', 0)} "
          f"across {commits.get('total_repos_touched', 0)} repo(s)")
    if words:
        print(f"📝  Words written : {words['words_written']:,} "
              f"({words['prose_written']:,} prose · {words['code_written']:,} code) "
              f"across {words['files_written']} file(s)")
        print(f"    In scope      : {words['words_in_scope']:,} words across "
              f"{words['files_in_scope']} associated file(s)")
    print(f"🧹  Dirty repos   : {len(dirty)}")
    print(f"📋  Transcript    : {_write_transcript(results)}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
