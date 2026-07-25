"""
session_audit.py — Scan for loose ends a working session left behind.

Answers one question: what did this session change that it did not finish?

Not a linter and not a test runner. It checks the seams that only show up
after the fact — a template and a registry that drifted apart, notes still on
the old layout, a hook installed but not executable, work done but not logged
into the daily note, code changed but never committed.

Each check is independent and reports ok / warn / fail. Exit code is 1 if any
check fails, so it can gate a wrap-up.

Usage:
    python3 session_audit.py              # human-readable report
    python3 session_audit.py --json       # machine-readable
    python3 session_audit.py --since 08:00  # only flag activity after this time
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import daily_note
import live_feed
import migrate_note_layout as mnl

HOME = Path.home()
HARNESS = Path(__file__).resolve().parent
VAULT = daily_note.VAULT_DIR
SETTINGS = HOME / ".claude" / "settings.json"
HOOK = HOME / ".claude" / "hooks" / "live-feed.sh"
HOOK_LOG = HOME / ".claude" / "hooks" / "live-feed.log"
SKILL = HOME / ".claude" / "skills" / "daily-note-os" / "SKILL.md"

OK, WARN, FAIL = "ok", "warn", "fail"
CHECKS = []

# Wall-clock cutoff (datetime) separating this session's work from whatever was
# already dirty when it started. Several agents share these repos, so without a
# cutoff the report credits this session with other tabs' uncommitted files.
SINCE = None

# How far the session log may trail the last recorded action before it counts
# as unlogged work.
LOG_LAG_TOLERANCE_MIN = 15


def _minutes_between(earlier: str, later: str) -> int:
    """Minutes from one HH:MM to another. 0 if either is missing or malformed."""
    try:
        a = datetime.strptime(earlier[:5], "%H:%M")
        b = datetime.strptime(later[:5], "%H:%M")
    except (ValueError, TypeError):
        return 0
    return max(0, int((b - a).total_seconds() // 60))


def _touched_since(path) -> bool:
    """True if the file was modified at or after the --since cutoff."""
    if SINCE is None:
        return True
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime) >= SINCE
    except OSError:
        return True


def check(title):
    """Register a check. Each returns (status, [detail lines])."""
    def deco(fn):
        CHECKS.append((title, fn))
        return fn
    return deco


def sh(args, cwd=None):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                             timeout=30)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


# ── Layout coherence ───────────────────────────────────────────────────

@check("Template, registry, and note agree on section order")
def _check_order_agreement():
    details = []
    try:
        tmpl = mnl.template_headers()
    except FileNotFoundError as e:
        return FAIL, [str(e)]

    registry = [h for h in daily_note.SECTIONS.values() if h]
    # Compare only headers both sides know about; the registry keeps legacy
    # entries the template dropped on purpose.
    shared_t = [h for h in tmpl if h in registry]
    shared_r = [h for h in registry if h in tmpl]
    if shared_t != shared_r:
        details.append(f"template: {' → '.join(h[3:] for h in shared_t)}")
        details.append(f"registry: {' → '.join(h[3:] for h in shared_r)}")
        return FAIL, ["SECTIONS order does not match the template"] + details

    missing = [h for h in tmpl if h not in registry
               and h not in ("## Quick Links",)]
    if missing:
        return WARN, [f"template has sections the registry does not know: "
                      f"{', '.join(missing)}"]
    return OK, [f"{len(shared_t)} shared sections in the same order"]


@check("Today's note matches the template layout")
def _check_today_layout():
    result = mnl.migrate(write=False)
    status = result.get("status")
    if status == "unchanged":
        return OK, [f"{result.get('sections')} sections, already in order"]
    if status == "skipped":
        return WARN, [result.get("reason", "skipped")]
    if status == "refused":
        return FAIL, ["migration refuses to run: " + result.get("reason", "")]
    return FAIL, ["note is not on the current layout — run "
                  "`python3 migrate_note_layout.py --write`"]


@check("Other daily notes on the old layout")
def _check_other_notes():
    pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')
    notes = sorted(p.stem for p in daily_note.DAILY_NOTES_DIR.glob("*.md")
                   if pattern.match(p.stem))
    today = datetime.now().strftime("%Y-%m-%d")
    stale, refused = [], []
    for date in notes:
        if date == today:
            continue
        r = mnl.migrate(date, write=False)
        if r.get("status") == "refused":
            refused.append(date)
        elif r.get("status", "").startswith("would-"):
            stale.append(date)
    if refused:
        return FAIL, [f"{len(refused)} note(s) the migration refuses to touch: "
                      + ", ".join(refused[:5])]
    if stale:
        return WARN, [f"{len(stale)} of {len(notes)} notes still on the old "
                      f"layout (harmless, but `migrate_note_layout.py --all "
                      f"--write` would align them)",
                      "most recent: " + ", ".join(stale[-5:])]
    return OK, [f"all {len(notes)} notes on the current layout"]


@check("Today's note renders (no swallowed headings or doubled rules)")
def _check_note_structure():
    if not daily_note.exists():
        return WARN, ["no note for today"]
    text = daily_note.daily_path().read_text(encoding="utf-8")
    lines = text.splitlines()
    problems = []

    swallowed = [(i + 1, lines[i]) for i in range(1, len(lines))
                 if lines[i].startswith("## ")
                 and lines[i - 1].lstrip().startswith(">")]
    if swallowed:
        problems.append(f"{len(swallowed)} heading(s) absorbed into a callout: "
                        + ", ".join(f"L{n} {h}" for n, h in swallowed[:4]))

    nested = [i for i, (a, b) in enumerate(zip(lines, lines[1:]), 2)
              if b.startswith("**[") and a.lstrip().startswith(">")]
    if nested:
        problems.append(f"{len(nested)} session-log entry nesting violation(s) "
                        f"at line(s) {nested[:4]}")

    doubled = text.count("---\n\n---")
    if doubled:
        problems.append(f"{doubled} doubled separator rule(s)")

    dupes = [h for h in set(re.findall(r'^## .+$', text, re.MULTILINE))
             if len(re.findall(rf'^{re.escape(h)}$', text, re.MULTILINE)) > 1]
    if dupes:
        problems.append("duplicate section header(s): " + ", ".join(dupes))

    return (FAIL, problems) if problems else (OK, ["structure clean"])


@check("Every AI-writable section exists in today's note")
def _check_sections_present():
    if not daily_note.exists():
        return WARN, ["no note for today"]
    status = daily_note.section_status()
    tmpl = set(mnl.template_headers())
    absent = [name for name in daily_note.AI_WRITABLE
              if status.get(name) == "absent"
              and daily_note.SECTIONS.get(name) in tmpl]
    if absent:
        return WARN, ["writable sections the template has but the note lacks: "
                      + ", ".join(sorted(absent))]
    return OK, ["all template-backed writable sections present"]


# ── Hook install ───────────────────────────────────────────────────────

@check("Live Feed hook is installed and runnable")
def _check_hook_install():
    problems = []
    if not HOOK.is_file():
        return FAIL, [f"hook missing: {HOOK}"]
    if not os.access(HOOK, os.X_OK):
        problems.append(f"hook is not executable: chmod +x {HOOK}")
    if not shutil.which("jq"):
        problems.append("jq not on PATH — the record path silently no-ops")

    rc, _, err = sh(["bash", "-n", str(HOOK)])
    if rc != 0:
        problems.append(f"hook has a syntax error: {err}")

    if not SETTINGS.is_file():
        problems.append(f"settings.json missing: {SETTINGS}")
    else:
        try:
            cfg = json.loads(SETTINGS.read_text())
        except json.JSONDecodeError as e:
            return FAIL, [f"settings.json is not valid JSON: {e}"]
        blob = json.dumps(cfg.get("hooks", {}))
        if "live-feed.sh" not in blob:
            problems.append("live-feed.sh is not wired in settings.json hooks")
        else:
            if '"Stop"' not in json.dumps({"Stop": cfg["hooks"].get("Stop")}) \
                    or not cfg["hooks"].get("Stop"):
                problems.append("no Stop hook — the final render can be lost")

    feed_py = HARNESS / "live_feed.py"
    if not feed_py.is_file():
        problems.append(f"live_feed.py missing at {feed_py}")

    return (FAIL, problems) if problems else (OK, ["hook installed, wired, jq present"])


@check("Live Feed is current and error-free")
def _check_feed_health():
    problems = []
    events = live_feed.read_events()
    if not events:
        return WARN, ["no activity recorded today — hook may not be firing"]

    last_event = events[-1].get("ts", "")
    rendered = daily_note.read_section("live_feed") if daily_note.exists() else ""
    m = re.search(r'updated (\d{2}:\d{2})', rendered)
    if not m:
        problems.append("Live Feed section has no rendered timestamp")
    else:
        if last_event[:5] > m.group(1):
            problems.append(f"section is behind the feed "
                            f"(rendered {m.group(1)}, last event {last_event}) "
                            f"— run `python3 live_feed.py --render`")

    lock = live_feed.feed_path().parent / ".render.lock"
    if lock.is_dir():
        problems.append(f"stale render lock present: {lock}")

    if HOOK_LOG.is_file() and HOOK_LOG.stat().st_size > 0:
        tail = HOOK_LOG.read_text(errors="replace").strip().splitlines()[-3:]
        problems.append("hook error log is not empty: " + " | ".join(tail))

    counts = {}
    for e in events:
        counts[e.get("sid", "?")] = counts.get(e.get("sid", "?"), 0) + 1
    note = [f"{len(events)} events from {len(counts)} session(s): "
            + ", ".join(f"{k or 'untagged'}={v}" for k, v in counts.items())]

    return (WARN, problems + note) if problems else (OK, note)


# ── Code and doc drift ─────────────────────────────────────────────────

@check("No code still anchors content under the hero image")
def _check_hero_anchor_drift():
    hits = []
    for path in HARNESS.glob("*.py"):
        if path.name in ("live_feed.py", "session_audit.py"):
            continue  # both legitimately place the work block there
        text = path.read_text(errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if "/hero_image" in line and "insert" not in line.lower():
                # Flag only lines that build an insertion point.
                if any(k in line for k in ("replace(", "f\"", "+ ", "sub(")):
                    hits.append(f"{path.name}:{i}: {line.strip()[:90]}")
    if hits:
        return WARN, ["code inserting content directly under the hero image "
                      "(that space belongs to the work block):"] + hits
    return OK, ["no stale hero-image anchors"]


@check("Docs describe the current section order")
def _check_doc_drift():
    stale = []
    targets = [SKILL, HARNESS / "README.md", HARNESS / "PIPELINE.md",
               HARNESS / "CHANGELOG.md"]
    for path in targets:
        if not path.is_file():
            continue
        # A doc listing the writable sections must now include live_feed.
        # Match the whole line: live_feed sorts before daily_reading in the
        # list, so anchoring the span at daily_reading missed it and reported
        # a correct doc as stale.
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if "daily_reading" in line and "waft_workspace" in line \
                    and "live_feed" not in line:
                stale.append(f"{path.name}:{i}: section list omits live_feed")
    if stale:
        return WARN, stale
    return OK, ["no stale section lists found"]


@check("New modules have test coverage")
def _check_test_coverage():
    tests_dir = HARNESS / "tests"
    covered = ""
    if tests_dir.is_dir():
        for t in tests_dir.glob("test_*.py"):
            covered += t.read_text(errors="replace")
    new_modules = ["live_feed", "migrate_note_layout"]
    missing = [m for m in new_modules if f"import {m}" not in covered
               and f"{m} " not in covered]
    if missing:
        return WARN, [f"no test imports: {', '.join(missing)}"]
    return OK, [f"tested: {', '.join(new_modules)}"]


# ── Work not yet logged or committed ───────────────────────────────────

def _entry_path(top, entry):
    """Absolute path for one `git status --porcelain` line."""
    name = entry[3:].strip().strip('"').split(" -> ")[-1]
    return Path(top) / name, name


def _changed_repos(apply_since=True):
    """Repos with uncommitted changes among the paths this session touched.

    With --since, entries older than the cutoff are dropped: they belong to
    whoever was working here before, and reporting them as this session's
    loose ends is how a real finding gets lost in noise.
    """
    seen, repos = set(), []
    for start in (HARNESS, VAULT, HOME / ".claude"):
        rc, top, _ = sh(["git", "rev-parse", "--show-toplevel"], cwd=str(start))
        if rc != 0 or not top or top in seen:
            continue
        seen.add(top)
        rc, out, _ = sh(["git", "status", "--porcelain"], cwd=top)
        if rc != 0:
            continue
        lines = [l for l in out.splitlines() if l.strip()]
        if apply_since:
            lines = [l for l in lines if _touched_since(_entry_path(top, l)[0])]
        repos.append((top, lines))
    return repos


@check("Session work is committed")
def _check_uncommitted():
    repos = _changed_repos()
    if not repos:
        return WARN, ["no git repo found for the touched paths"]
    dirty = [(top, lines) for top, lines in repos if lines]
    if not dirty:
        return OK, ["working trees clean"
                    + (f" since {SINCE.strftime('%H:%M')}" if SINCE else "")]
    details = []
    if SINCE:
        details.append(f"(only files modified since "
                       f"{SINCE.strftime('%H:%M')} are counted)")
    for top, lines in dirty:
        details.append(f"{Path(top).name}: {len(lines)} uncommitted change(s)")
        details += [f"    {l}" for l in lines[:10]]
        if len(lines) > 10:
            details.append(f"    … {len(lines) - 10} more")
    return WARN, details


@check("Session log reflects the work done")
def _check_session_log():
    if not daily_note.exists():
        return WARN, ["no note for today"]
    body = daily_note.read_section("claude_session_log")
    stamps = re.findall(r'\[!note\]-\s*(\d{2}:\d{2})', body)
    events = live_feed.read_events()
    last_activity = events[-1].get("ts", "")[:5] if events else ""
    if not stamps:
        return WARN, ["session log has no entries today — "
                      "call daily_note.append_session_log() before closing"]

    # Tolerance, because running this audit is itself recorded activity: without
    # it the check can never pass, and a check that always warns gets ignored.
    lag = _minutes_between(max(stamps), last_activity)
    if lag > LOG_LAG_TOLERANCE_MIN:
        return WARN, [f"last session-log entry is {max(stamps)} but activity "
                      f"continued to {last_activity} ({lag} min) — the work "
                      f"since then is unlogged"]
    return OK, [f"{len(stamps)} entries, latest {max(stamps)}"
                + (f" ({lag} min behind activity)" if lag > 0 else "")]


# Runtime droppings, not authored work. Flagging these buries the real finding.
REF_NOISE = re.compile(
    r'(\.db(-shm|-wal)?$|\.sqlite\d*$|\.log$|\.jsonl$|\.pyc$'
    r'|(^|/)\.[^/]+/|__pycache__/)'
)


@check("Changed code outside the vault is referenced in the note")
def _check_code_refs():
    if not daily_note.exists():
        return WARN, ["no note for today"]
    text = daily_note.daily_path().read_text(errors="replace")
    unreferenced = []
    for top, lines in _changed_repos():
        if Path(top) == VAULT:
            continue
        for entry in lines:
            _, name = _entry_path(top, entry)
            if REF_NOISE.search(name):
                continue
            base = Path(name).name
            if base and base not in text:
                unreferenced.append(f"{Path(top).name}/{name}")
    if unreferenced:
        return WARN, ["changed files not mentioned anywhere in today's note "
                      "(frontmatter code_refs or body):"] + \
                     [f"    {u}" for u in unreferenced[:12]]
    return OK, ["all changed files referenced"]


@check("No stray backup or scratch files left in the vault or harness")
def _check_strays():
    patterns = ["*.bak", "*.orig", "*.rej", "*.tmp", "*~", "*.md.save"]
    strays = []
    for root in (VAULT, HARNESS):
        for pat in patterns:
            for p in root.rglob(pat):
                if ".git" in p.parts or "__pycache__" in p.parts:
                    continue
                strays.append(str(p.relative_to(root.parent)))
    if strays:
        return WARN, ["stray files:"] + [f"    {s}" for s in strays[:12]]
    return OK, ["none found"]


@check("Harness test suite passes")
def _check_tests():
    rc, out, err = sh([sys.executable, "-m", "pytest", "tests/", "-q",
                       "--no-header", "-x", "--tb=no"], cwd=str(HARNESS))
    tail = (out or err).strip().splitlines()
    summary = next((l for l in reversed(tail) if "passed" in l or "failed" in l),
                   "no summary")
    if rc == 0:
        return OK, [summary]
    failures = [l for l in tail if l.startswith("FAILED")]
    return WARN, [summary] + failures[:6]


# ── Runner ─────────────────────────────────────────────────────────────

def run():
    results = []
    for title, fn in CHECKS:
        try:
            status, details = fn()
        except Exception as e:                      # a broken check is a finding
            status, details = FAIL, [f"check raised {type(e).__name__}: {e}"]
        results.append({"check": title, "status": status, "details": details})
    return results


def main(argv):
    global SINCE
    ap = argparse.ArgumentParser(description="Scan for session loose ends")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--since", metavar="HH:MM",
                    help="only count files modified at or after this time "
                         "today, so another tab's dirty tree is not reported "
                         "as this session's loose ends")
    args = ap.parse_args(argv)

    if args.since:
        try:
            hh, mm = (int(x) for x in args.since.split(":"))
            SINCE = datetime.now().replace(hour=hh, minute=mm, second=0,
                                           microsecond=0)
        except ValueError:
            print(f"bad --since value: {args.since} (expected HH:MM)")
            return 2

    results = run()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        icons = {OK: "  ok  ", WARN: " WARN ", FAIL: " FAIL "}
        print(f"\nSession audit — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        for r in results:
            print(f"[{icons[r['status']]}] {r['check']}")
            if r["status"] != OK or True:
                for d in r["details"]:
                    print(f"           {d}")
        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(f"\n{counts.get(OK, 0)} ok · {counts.get(WARN, 0)} warn · "
              f"{counts.get(FAIL, 0)} fail\n")

    return 1 if any(r["status"] == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
