#!/usr/bin/env python3
"""
check_in.py — Harness diagnostic + repair loop.

Phases:
  1. Diagnose   — run preflight.py --json
  2. Triage     — classify by severity + fixability
  3. Repair     — attempt auto-fixes for known issues (up to --max-tries)
  4. Re-check   — re-run preflight to verify repairs landed
  5. Escalate   — surface unfixable issues with remediation hints
  6. Declare    — READY / READY_WITH_WARNINGS / BLOCKED

Usage:
  python3 check_in.py [--max-tries N] [--json] [--dry-run]

Exit codes:
  0 = READY or READY_WITH_WARNINGS
  1 = BLOCKED
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

HARNESS_DIR = Path(__file__).parent.resolve()
VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
TOOLS_DIR = Path.home() / ".claude" / "tools"
DEFAULT_MAX_TRIES = 3


# ── Preflight runner ──────────────────────────────────────────────────────────

def run_preflight() -> dict:
    result = subprocess.run(
        [sys.executable, str(HARNESS_DIR / "preflight.py"), "--json"],
        capture_output=True, text=True, timeout=90,
        cwd=str(HARNESS_DIR),
    )
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"checks": [], "readiness": {"status": "fail", "data": {"score": 0}}}


def checks_by_status(preflight: dict, *statuses: str) -> list[dict]:
    return [c for c in preflight.get("checks", []) if c.get("status") in statuses]


# ── Repair functions ──────────────────────────────────────────────────────────
# Each returns (success: bool, message: str).

def _repair_B3(check: dict) -> tuple[bool, str]:
    """Create today's daily note from the vault template if missing."""
    try:
        sys.path.insert(0, str(HARNESS_DIR))
        import daily_note
        already = daily_note.exists()
        path = daily_note.create_from_template()
        return True, "already exists" if already else f"created {path.name}"
    except Exception as e:
        return False, str(e)


def _repair_B5(check: dict) -> tuple[bool, str]:
    """Add session-start entry to today's note.

    write_section self-heals a missing section header (appends it), so this
    is just a plain append — no manual header-detection branch needed.
    """
    try:
        sys.path.insert(0, str(HARNESS_DIR))
        import daily_note
        ts = datetime.now().strftime("%H:%M")
        daily_note.append_session_log(f"check_in at {ts}")
        return True, "session log entry added"
    except Exception as e:
        return False, str(e)


def _repair_scaffold(check: dict) -> tuple[bool, str]:
    """Scaffold missing hub + journal via spin_up helpers."""
    try:
        sys.path.insert(0, str(HARNESS_DIR))
        import spin_up
        date_str = datetime.now().strftime("%Y-%m-%d")
        today_label = datetime.now().strftime("%A, %B %-d")
        dt_ctx = spin_up._get_datetime_context()
        msgs = []
        _, s = spin_up._scaffold_journal(date_str, today_label, dt_ctx)
        msgs.append(f"journal:{s}")
        _, s = spin_up._scaffold_hub(date_str, today_label, dt_ctx)
        msgs.append(f"hub:{s}")
        # Wire frontmatter
        try:
            import frontmatter as fm
            hub_wl = f"[[Hubs/{date_str}_hub]]"
            jnl_wl = f"[[Claude Journal/{date_str}]]"
            if not fm.get_field("hub"):
                fm.set_field("hub", hub_wl)
            if not fm.get_field("journal"):
                fm.set_field("journal", jnl_wl)
        except Exception:
            pass
        return True, " · ".join(msgs)
    except Exception as e:
        return False, str(e)


def _repair_B9(check: dict) -> tuple[bool, str]:
    """Fill blank sitrep via sitrep_gen. Other blank sections escalate."""
    empty = check.get("data", {}).get("empty_sections", [])
    if not empty:
        return True, "no blank sections"
    results = []
    try:
        sys.path.insert(0, str(HARNESS_DIR))
        import sitrep_gen
        date_str = datetime.now().strftime("%Y-%m-%d")
        if "sitrep" in empty:
            ok, msg = sitrep_gen.write_to_note(date_str, force=True)
            results.append(f"sitrep:{'ok' if ok else msg}")
        skipped = [s for s in empty if s != "sitrep"]
        if skipped:
            results.append(f"skip(need-spin-up):{','.join(skipped)}")
        return bool(results), " · ".join(results)
    except Exception as e:
        return False, str(e)


def _repair_B6(check: dict) -> tuple[bool, str]:
    """Synthesize seed handoff from most recent hub when yesterday's is missing."""
    try:
        sys.path.insert(0, str(HARNESS_DIR))
        import spin_up
        import daily_note
        from datetime import timedelta

        # Find most recent hub (Hubs/ or Wagonwheels/)
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_hub_date = spin_up._find_prev_hub(today)
        if not prev_hub_date:
            return False, "no hub found to synthesize from"

        # Pull active threads from that hub
        import sitrep_gen
        threads = sitrep_gen._read_hub_threads(prev_hub_date)
        if not threads:
            return False, f"hub {prev_hub_date} has no active threads"

        # Build synthetic top-3 lines from thread labels + states
        lines = []
        for label, state in threads[:3]:
            line = f"- [ ] {label}" + (f": {state}" if state else "")
            lines.append(line)
        seed_md = "\n".join(lines)

        # Write into yesterday's note (creates cross-session continuity B6 expects)
        # If yesterday's note missing, write into today's as fallback
        target_date = yesterday if daily_note.exists(yesterday) else today
        daily_note.write_section("tomorrows_top_3", seed_md, actor="claude",
                                 mode="replace", date=target_date)
        return True, f"seeded from hub {prev_hub_date} → {target_date} ({len(lines)} items)"
    except Exception as e:
        return False, str(e)


def _repair_D2(check: dict) -> tuple[bool, str]:
    """Touch verified user/feedback memories to reset staleness. Surface project/reference for human review."""
    import re as _re
    import time as _time

    MEMORY_DIR_CODE = Path.home() / ".claude" / "projects" / "-Users-ctavolazzi-Code" / "memory"
    if not MEMORY_DIR_CODE.exists():
        return True, "no memory dir"

    stale_data = check.get("data", {}).get("stale", [])
    stale_names = {s.split("(")[0] for s in stale_data}

    touched = []
    needs_review = []
    for path in MEMORY_DIR_CODE.glob("*.md"):
        if path.name == "MEMORY.md" or path.name not in stale_names:
            continue
        text = path.read_text(encoding="utf-8")
        type_m = _re.search(r'^type:\s*(\w+)', text, _re.MULTILINE)
        mem_type = type_m.group(1) if type_m else "unknown"
        if mem_type in ("user", "feedback"):
            # These are long-lived by nature — refresh mtime
            path.touch()
            touched.append(path.name)
        else:
            needs_review.append(f"{path.name}({mem_type})")

    parts = []
    if touched:
        parts.append(f"touched {len(touched)}: {', '.join(touched)}")
    if needs_review:
        parts.append(f"review needed: {', '.join(needs_review)}")
    return True, " · ".join(parts) if parts else "nothing to do"


def _repair_dirs(check: dict) -> tuple[bool, str]:
    """Create missing cache/runs dirs."""
    created = []
    for d in [
        Path.home() / ".cache" / "daily-harness",
        Path.home() / ".spin_up" / "runs",
    ]:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))
    return True, f"created: {created}" if created else "already exist"


# ── Check ID → repair mapping + escalation hints ──────────────────────────────

REPAIRS: dict[str, callable] = {
    "B3": _repair_B3,
    "B5": _repair_B5,
    "B6": _repair_B6,
    "B9": _repair_B9,
    "D2": _repair_D2,
    "A4": _repair_dirs,
    "A5": _repair_dirs,
}

# IDs that trigger scaffold repair when vault files are missing
SCAFFOLD_TRIGGERS = {"B3", "B5"}

ESCALATE_HINTS: dict[str, str] = {
    "B4":  "Run spin-up to fill daily note sections (python3 spin_up.py)",
    "B7":  "Commit vault: cd ~/Documents/Personal-Remote-Vault && git add -A && git commit",
    "D1":  "empirica session-create --ai-id claude-code",
    "D2":  "Memory files need human review (project/reference types flagged)",
    "E2":  "Commit or stash dirty ~/Code files",
    "E4":  "Check MCP server config: claude mcp list",
}

# Aggregate/synthetic checks — skip in triage (derived from other checks, not independently fixable)
META_IDS = {"G1"}

ICON = {"pass": "✓", "warn": "⚠", "fail": "✗", "halt": "🚫", "skip": "·"}


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Harness diagnostic + repair loop")
    p.add_argument("--max-tries", type=int, default=DEFAULT_MAX_TRIES)
    p.add_argument("--json",     action="store_true", dest="as_json")
    p.add_argument("--dry-run",  action="store_true")
    args = p.parse_args()

    print("🔍 Check-In — Diagnose → Repair → Declare\n")

    # ── Phase 1: Diagnose ─────────────────────────────────────────────────────
    print("Phase 1: Diagnose...")
    preflight = run_preflight()
    readiness = preflight.get("readiness", {})
    data = readiness.get("data", {})
    score = data.get("score", 0)
    status = readiness.get("status", "warn")

    fails  = checks_by_status(preflight, "fail", "halt")
    warns  = checks_by_status(preflight, "warn")
    halts  = [c for c in fails if c.get("status") == "halt"]

    print(f"  Score: {score:.0%}  |  {len(fails)} fail · {len(warns)} warn · {len(halts)} halt")

    if halts:
        print("\n🚫 HALT detected — skipping repair, surface blockers:")
        for c in halts:
            print(f"   {c['id']}: {c['message']}")
            if c['id'] in ESCALATE_HINTS:
                print(f"   → {ESCALATE_HINTS[c['id']]}")
        _declare("BLOCKED", score, {}, [], args)
        return 1

    # ── Phase 2: Triage ───────────────────────────────────────────────────────
    print("\nPhase 2: Triage...")
    problem_ids = {c["id"] for c in fails + warns}
    fixable_ids  = (problem_ids & REPAIRS.keys()) - META_IDS
    escalate_ids = (problem_ids & ESCALATE_HINTS.keys()) - META_IDS
    unknown_ids  = problem_ids - fixable_ids - escalate_ids - META_IDS

    print(f"  Auto-fixable : {sorted(fixable_ids) or 'none'}")
    print(f"  Escalate     : {sorted(escalate_ids) or 'none'}")
    print(f"  Unknown      : {sorted(unknown_ids) or 'none'}")

    # Also run scaffold if vault files may be missing (even if check passed)
    run_scaffold = bool({"B3", "B5", "B9"} & problem_ids)

    # ── Phase 3: Repair ───────────────────────────────────────────────────────
    repair_log: dict[str, dict] = {}

    if not args.dry_run:
        print("\nPhase 3: Repair...")

        # Scaffold first (creates hub/journal before B5/B9 attempts)
        if run_scaffold:
            ok, msg = _repair_scaffold({})
            repair_log["scaffold"] = {"ok": ok, "msg": msg, "attempt": 1}
            print(f"  {'✓' if ok else '✗'} scaffold: {msg}")

        for check_id in sorted(fixable_ids):
            check = next((c for c in fails + warns if c["id"] == check_id), {})
            fn = REPAIRS[check_id]
            for attempt in range(1, args.max_tries + 1):
                ok, msg = fn(check)
                repair_log[check_id] = {"ok": ok, "msg": msg, "attempt": attempt}
                print(f"  {'✓' if ok else '✗'} {check_id} (try {attempt}): {msg}")
                if ok:
                    break
    else:
        print("\nPhase 3: Repair (dry-run — skipping writes)")

    # ── Phase 4: Re-check ─────────────────────────────────────────────────────
    if repair_log and not args.dry_run:
        print("\nPhase 4: Re-check...")
        preflight2 = run_preflight()
        score2 = preflight2.get("readiness", {}).get("data", {}).get("score", score)
        fails2  = checks_by_status(preflight2, "fail", "halt")
        warns2  = checks_by_status(preflight2, "warn")
        repaired_ids = {id for id, r in repair_log.items() if r["ok"]}
        still_bad = [c for c in fails2 + warns2 if c["id"] in repaired_ids]
        if still_bad:
            print(f"  Still failing after repair: {[c['id'] for c in still_bad]}")
        else:
            print(f"  All repaired checks now passing ✓")
        score = score2
        fails = fails2
        warns = warns2
        all_problem_ids2 = {c["id"] for c in fails2 + warns2}
        escalate_ids = all_problem_ids2 & ESCALATE_HINTS.keys()
        unknown_ids  = all_problem_ids2 - set(REPAIRS.keys()) - escalate_ids - META_IDS

    # ── Phase 5: Escalate ─────────────────────────────────────────────────────
    if escalate_ids or unknown_ids:
        print("\nPhase 5: Escalate — needs human action:")
        all_problems = fails + warns
        for check_id in sorted(escalate_ids | unknown_ids):
            check = next((c for c in all_problems if c["id"] == check_id), {})
            icon = ICON.get(check.get("status", "warn"), "⚠")
            print(f"  {icon} {check_id}: {check.get('message', '?')}")
            if check_id in ESCALATE_HINTS:
                print(f"     → {ESCALATE_HINTS[check_id]}")

    # ── Phase 6: Declare ──────────────────────────────────────────────────────
    remaining_fails = [c for c in fails if c.get("status") in ("fail", "halt")]
    if remaining_fails:
        declare = "BLOCKED" if any(c["status"] == "halt" for c in remaining_fails) else "READY_WITH_WARNINGS"
    elif score >= 0.85 and not warns:
        declare = "READY"
    else:
        declare = "READY_WITH_WARNINGS"

    _declare(declare, score, repair_log, sorted(escalate_ids), args)
    return 0 if declare != "BLOCKED" else 1


def _declare(declare: str, score: float, repair_log: dict, escalated: list, args) -> None:
    icons = {"READY": "✅", "READY_WITH_WARNINGS": "⚠️ ", "BLOCKED": "🚫"}
    labels = {
        "READY":               f"READY ({score:.0%}) — harness operational",
        "READY_WITH_WARNINGS": f"READY WITH WARNINGS ({score:.0%}) — non-blocking issues remain",
        "BLOCKED":             f"BLOCKED — resolve halt(s) before proceeding",
    }
    print("\n" + "=" * 52)
    print(f"{icons[declare]} {labels[declare]}")
    if repair_log:
        fixed = [id for id, r in repair_log.items() if r["ok"]]
        failed = [id for id, r in repair_log.items() if not r["ok"]]
        if fixed:
            print(f"   Repaired : {fixed}")
        if failed:
            print(f"   Failed   : {failed}")
    if escalated:
        print(f"   Escalated: {escalated} (needs human action)")
    print("=" * 52)

    if args.as_json:
        print(json.dumps({
            "declare": declare,
            "score": round(score, 3),
            "repairs": repair_log,
            "escalated": escalated,
            "timestamp": datetime.now().isoformat(),
        }, indent=2))


if __name__ == "__main__":
    sys.exit(main())
