#!/usr/bin/env python3
"""
preflight.py — /spin-up preflight checks for Daily Note Harness.

Runs ~30 checks across 7 categories. Outputs pretty terminal report
or structured JSON. Surfaces blockers, warnings, readiness score,
and recommended next action.

Categories:
  A. Harness Integrity   — can we use the tools?
  B. Vault & Daily Note  — vault + today's note state
  C. Data Sources        — fetcher module health
  D. Epistemic           — empirica transactions, memory
  E. Environment         — disk, git, MCP
  F. Work Context        — devlog, commits, work efforts
  G. Readiness Gate      — synthesize, recommend next

Usage:
  python3 preflight.py              # pretty terminal report
  python3 preflight.py --json       # JSON stdout
  python3 preflight.py --category A # filter category (A-F)
  python3 preflight.py --deep       # include network probes
  python3 preflight.py --fail-fast  # stop on first HALT
"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Config ─────────────────────────────────────────────────────────────

HERE = Path(__file__).parent
CODE_ROOT = Path.home() / "Code"
VAULT = Path.home() / "Documents" / "Personal-Remote-Vault"
DAILY_NOTES = VAULT / "Daily Notes"
CACHE_DIR = Path.home() / ".cache" / "daily-harness"
RUNS_DIR = Path.home() / ".spin_up" / "runs"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "-Users-ctavolazzi-Code" / "memory"
MCP_DIAGNOSTIC = CODE_ROOT / ".mcp-servers" / "mcp_diagnostic.py"
DEVLOG = CODE_ROOT / "_work_efforts" / "devlog.md"

POCKETBASE_URL = "http://localhost:8090/api/health"
DAILY_API_URL = "http://localhost:1010/api/daily/status"
VAULT_REPO = "ctavolazzi/Personal-Remote-Vault"

TODAY = datetime.now().strftime("%Y-%m-%d")

# ── Result primitives ──────────────────────────────────────────────────

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"
HALT = "halt"

STATUS_EMOJI = {
    PASS: "✅",
    WARN: "⚠️ ",
    FAIL: "❌",
    SKIP: "○ ",
    HALT: "🛑",
}


@dataclass
class Check:
    id: str
    category: str
    name: str
    status: str
    message: str
    action: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


# ── Utilities ──────────────────────────────────────────────────────────

def run(cmd, timeout=10, cwd=None):
    """Shell out, return (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def module_importable(name: str) -> bool:
    rc, _, _ = run([sys.executable, "-c", f"import {name}"], timeout=5, cwd=str(HERE))
    return rc == 0


def url_alive(url: str, timeout=2) -> bool:
    rc, _, _ = run(["curl", "-sf", "--max-time", str(timeout), url], timeout=timeout + 1)
    return rc == 0


def file_age_seconds(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    return datetime.now().timestamp() - path.stat().st_mtime


# ── Category A: Harness Integrity ──────────────────────────────────────

def check_harness_integrity(deep: bool = False) -> List[Check]:
    out: List[Check] = []

    # Ensure SimpleAgentOS dir on path for imports
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))

    # A1: daily_note importable
    ok = module_importable("daily_note")
    out.append(Check(
        id="A1", category="harness", name="daily_note.py import",
        status=PASS if ok else HALT,
        message="importable" if ok else "ImportError — harness unusable",
        action=None if ok else "fix daily_note.py or PYTHONPATH",
    ))

    # A2: daily_note.py status callable
    rc, sout, serr = run([sys.executable, str(HERE / "daily_note.py"), "status"], timeout=10)
    marker_lines = [l for l in sout.split("\n") if any(m in l for m in ("●", "○", "◐", "·"))]
    if rc != 0:
        a2_status, a2_msg = FAIL, f"failed: {serr[:80]}"
    elif marker_lines:
        a2_status, a2_msg = PASS, f"{len(marker_lines)} sections enumerated"
    else:
        # status now falls back to the most recent note; zero markers means
        # the vault has no daily notes at all — a fresh vault, not a broken CLI
        a2_status, a2_msg = WARN, "no daily notes to enumerate (fresh vault?)"
    out.append(Check(
        id="A2", category="harness", name="daily_note status command",
        status=a2_status, message=a2_msg,
        data={"section_count": len(marker_lines)},
    ))

    # A3: fetcher deps importable
    deps = ["weather", "local_news", "music_pick", "git_scanner", "arxiv", "commit_summary"]
    missing = [d for d in deps if not module_importable(d)]
    out.append(Check(
        id="A3", category="harness", name="spin_up fetcher deps",
        status=PASS if not missing else WARN,
        message="all importable" if not missing else f"missing: {', '.join(missing)}",
        data={"missing": missing, "total": len(deps)},
    ))

    # A4: cache dir writable
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_ok = os.access(CACHE_DIR, os.W_OK)
    except Exception:
        cache_ok = False
    out.append(Check(
        id="A4", category="harness", name="cache dir writable",
        status=PASS if cache_ok else FAIL,
        message=str(CACHE_DIR),
    ))

    # A5: runs dir writable
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        runs_ok = os.access(RUNS_DIR, os.W_OK)
    except Exception:
        runs_ok = False
    out.append(Check(
        id="A5", category="harness", name="runs dir writable",
        status=PASS if runs_ok else FAIL,
        message=str(RUNS_DIR),
    ))

    # A6 + A7: network-dependent — only if --deep
    if deep:
        alive = url_alive(POCKETBASE_URL)
        out.append(Check(
            id="A6", category="harness", name="PocketBase canonical",
            status=PASS if alive else WARN,
            message="reachable" if alive else "offline",
            action=None if alive else "cd core_engine && ./pocketbase serve",
            data={"url": POCKETBASE_URL},
        ))
        alive = url_alive(DAILY_API_URL)
        out.append(Check(
            id="A7", category="harness", name="Daily Note API",
            status=PASS if alive else WARN,
            message="reachable" if alive else "offline",
            data={"url": DAILY_API_URL},
        ))
    else:
        out.append(Check(id="A6", category="harness", name="PocketBase canonical",
                         status=SKIP, message="use --deep to probe"))
        out.append(Check(id="A7", category="harness", name="Daily Note API",
                         status=SKIP, message="use --deep to probe"))

    return out


# ── Category B: Vault & Daily Note State ───────────────────────────────

def check_vault_and_note() -> List[Check]:
    out: List[Check] = []

    # B1: vault exists
    vault_ok = VAULT.exists() and VAULT.is_dir()
    out.append(Check(
        id="B1", category="vault", name="Vault path exists",
        status=PASS if vault_ok else HALT,
        message=str(VAULT),
        action=None if vault_ok else "verify vault path; cannot proceed",
    ))

    # B2: GitHub repo privacy — enforcement
    rc, sout, serr = run(["gh", "api", f"repos/{VAULT_REPO}", "--jq", ".private"], timeout=5)
    if rc == 0:
        priv = sout.strip() == "true"
        out.append(Check(
            id="B2", category="vault", name="GitHub repo privacy",
            status=PASS if priv else HALT,
            message="repo private" if priv else "REPO NOT PRIVATE — blocks backup",
            action=None if priv else "make repo private before any push",
            data={"private": priv},
        ))
    else:
        out.append(Check(
            id="B2", category="vault", name="GitHub repo privacy",
            status=WARN, message=f"gh api failed: {serr[:80]}",
        ))

    # B3: today's note exists
    today_note = DAILY_NOTES / f"{TODAY}.md"
    note_ok = today_note.exists()
    out.append(Check(
        id="B3", category="vault", name="Today's daily note",
        status=PASS if note_ok else WARN,
        message=today_note.name + (" exists" if note_ok else " missing"),
        action=None if note_ok else "create via daily_note.py or /spin-up fill",
    ))

    # B4: section fill ratio
    filled = empty = template = total = 0
    by_section: Dict[str, str] = {}
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import daily_note
        absent = 0
        if note_ok:
            by_section = daily_note.section_status(TODAY) or {}
            filled = sum(1 for v in by_section.values() if v == "filled")
            empty = sum(1 for v in by_section.values() if v == "empty")
            template = sum(1 for v in by_section.values() if v == "template")
            absent = sum(1 for v in by_section.values() if v == "absent")
            total = len(by_section) - absent
        b4_status = PASS if filled >= 4 else WARN
        msg = (f"{filled}/{total} filled, {empty} empty, {template} template"
               + (f", {absent} absent" if absent else "")) if total else "no note to scan"
        out.append(Check(
            id="B4", category="vault", name="Section fill ratio",
            status=b4_status if total else SKIP,
            message=msg,
            data={"filled": filled, "empty": empty, "template": template,
                  "absent": absent, "total": total, "by_section": by_section},
        ))
    except Exception as e:
        out.append(Check(
            id="B4", category="vault", name="Section fill ratio",
            status=FAIL, message=f"section_status failed: {e}",
        ))

    # B5: last session log entry age
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import daily_note
        import re
        if note_ok:
            log = daily_note.read_section("claude_session_log", TODAY) or ""
            full_stamps = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", log)
            # Match `[actor @ HH:MM]` format
            short_stamps = re.findall(r"\[[\w-]+ @ (\d{2}:\d{2})\]", log)
            # Match Obsidian callout format `> [!note]- HH:MM`
            callout_stamps = re.findall(r">\s*\[![\w-]+\]-?\s*(\d{2}:\d{2})", log)
            stamps = full_stamps + [f"{TODAY} {t}" for t in short_stamps + callout_stamps]
            if stamps:
                last = sorted(stamps)[-1]
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M")
                age_min = int((datetime.now() - last_dt).total_seconds() / 60)
                st = PASS if 0 <= age_min < 60 * 24 else WARN
                age_label = f"{age_min}m ago" if age_min >= 0 else f"{abs(age_min)}m in future"
                out.append(Check(
                    id="B5", category="vault", name="Last session log entry",
                    status=st, message=f"{last} ({age_label})",
                    data={"last_entry": last, "age_minutes": age_min},
                ))
            else:
                out.append(Check(
                    id="B5", category="vault", name="Last session log entry",
                    status=WARN, message="no timestamped entries today yet",
                ))
        else:
            out.append(Check(
                id="B5", category="vault", name="Last session log entry",
                status=SKIP, message="no note",
            ))
    except Exception as e:
        out.append(Check(
            id="B5", category="vault", name="Last session log entry",
            status=FAIL, message=str(e),
        ))

    # B6: most recent handoff (gap-tolerant — walks back up to 14 days)
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import re as _re
        import daily_note
        handoff = daily_note.last_handoff()
        top3_text = (handoff or {}).get("tomorrows_top_3") or ""
        top3_items = _re.findall(r'^-\s*\[.\]\s*\S.*$', top3_text, _re.MULTILINE)
        found = bool((handoff or {}).get("found"))
        gap_days = (handoff or {}).get("gap_days")
        if found and top3_items:
            gap_note = f", {gap_days}d gap" if gap_days and gap_days > 1 else ""
            msg = f"{len(top3_items)} items from {handoff['date']}{gap_note}"
            status = PASS
        elif found:
            gap_note = f" ({gap_days}d gap)" if gap_days and gap_days > 1 else ""
            msg = f"seed from {handoff['date']} has no top-3 items{gap_note}"
            status = WARN
        else:
            msg = "no prior note within 14 days — cold start"
            status = WARN
        out.append(Check(
            id="B6", category="vault", name="Yesterday's handoff",
            status=status, message=msg,
            data={"top3": top3_items, "handoff_keys": list((handoff or {}).keys()),
                 "gap_days": gap_days},
        ))
    except Exception as e:
        out.append(Check(
            id="B6", category="vault", name="Yesterday's handoff",
            status=FAIL, message=str(e),
        ))

    # B7: vault dirty status
    rc, sout, _ = run(["git", "-C", str(VAULT), "status", "-s"])
    if rc == 0:
        dirty = len([l for l in sout.split("\n") if l.strip()])
        st = PASS if dirty == 0 else (WARN if dirty < 50 else FAIL)
        out.append(Check(
            id="B7", category="vault", name="Vault dirty status",
            status=st, message=f"{dirty} changed paths",
            data={"dirty_count": dirty},
        ))
    else:
        out.append(Check(id="B7", category="vault", name="Vault dirty status",
                         status=SKIP, message="not a git repo or git failed"))

    # B8: last vault commit age
    rc, sout, _ = run(["git", "-C", str(VAULT), "log", "-1", "--format=%ct"])
    if rc == 0 and sout:
        try:
            ts = int(sout.strip())
            age_days = int((datetime.now().timestamp() - ts) / 86400)
            st = PASS if age_days < 7 else (WARN if age_days < 30 else FAIL)
            out.append(Check(
                id="B8", category="vault", name="Last vault commit age",
                status=st, message=f"{age_days}d ago",
                data={"age_days": age_days},
            ))
        except Exception:
            out.append(Check(id="B8", category="vault", name="Last vault commit age",
                             status=WARN, message="parse failed"))
    else:
        out.append(Check(id="B8", category="vault", name="Last vault commit age",
                         status=SKIP, message="no commits or git unavailable"))

    # B9: blank sections in today's note (low priority — never blocks spin-up)
    try:
        import blank_files as _bf
        today_str = datetime.now().strftime("%Y-%m-%d")
        empties = _bf.find_daily_empties(VAULT)
        today_e = [e for e in empties if today_str in e["path"]]
        if today_e:
            secs = today_e[0]["empty_sections"]
            out.append(Check(
                id="B9", category="vault", name="Blank sections today",
                status=WARN,
                message=f"{len(secs)} unfilled: {', '.join(secs)}",
                data={"empty_sections": secs},
            ))
        else:
            out.append(Check(id="B9", category="vault", name="Blank sections today",
                             status=PASS, message="no blank sections"))
    except Exception as e:
        out.append(Check(id="B9", category="vault", name="Blank sections today",
                         status=WARN, message=f"check skipped: {e}"))

    return out


# ── Category C: Data Sources ───────────────────────────────────────────

def check_data_sources(deep: bool = False) -> List[Check]:
    out: List[Check] = []
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))

    sources = [
        ("weather", "C1"),
        ("local_news", "C2"),
        ("music_pick", "C3"),
        ("arxiv", "C4"),
        ("git_scanner", "C5"),
        ("commit_summary", "C6"),
    ]
    for name, cid in sources:
        ok = module_importable(name)
        out.append(Check(
            id=cid, category="data", name=f"{name} module",
            status=PASS if ok else FAIL,
            message="importable" if ok else "ImportError",
        ))

    # C7: cache freshness
    ttl = {"weather": 3600, "news": 10800, "arxiv": 86400, "git": 600, "music": 86400}
    stale: List[str] = []
    missing: List[str] = []
    if CACHE_DIR.exists():
        for name, max_age in ttl.items():
            cached = list(CACHE_DIR.glob(f"{name}-{TODAY}.json"))
            if not cached:
                missing.append(name)
                continue
            age = file_age_seconds(cached[0])
            if age and age > max_age:
                stale.append(f"{name}({int(age)}s)")
    out.append(Check(
        id="C7", category="data", name="Cache freshness",
        status=PASS if not stale else WARN,
        message=(f"all fresh ({len(ttl) - len(missing)}/{len(ttl)} cached)"
                 if not stale else f"stale: {', '.join(stale)}"),
        data={"stale": stale, "missing": missing},
    ))

    return out


# ── Category D: Epistemic Context ──────────────────────────────────────

def check_epistemic() -> List[Check]:
    out: List[Check] = []

    # D1: open empirica transactions
    rc, sout, serr = run(["empirica", "transaction-list", "--open", "--output", "json"], timeout=5)
    if rc == 0 and sout:
        try:
            data = json.loads(sout)
            if isinstance(data, list):
                count = len(data)
            elif isinstance(data, dict):
                count = data.get("count", len(data.get("transactions", [])))
            else:
                count = 0
            st = PASS if count <= 1 else WARN
            out.append(Check(
                id="D1", category="epistemic", name="Open empirica transactions",
                status=st, message=f"{count} open",
                action=None if count <= 1 else "close stale via empirica postflight-submit",
                data={"count": count},
            ))
        except Exception:
            out.append(Check(id="D1", category="epistemic", name="Open empirica transactions",
                             status=SKIP, message="JSON parse failed"))
    else:
        out.append(Check(id="D1", category="epistemic", name="Open empirica transactions",
                         status=SKIP, message="empirica cmd unavailable"))

    # D2: memory file freshness
    if MEMORY_DIR.exists():
        mems = list(MEMORY_DIR.glob("*.md"))
        stale = []
        for m in mems:
            age = file_age_seconds(m)
            if age and age > 14 * 86400:
                stale.append(f"{m.name}({int(age / 86400)}d)")
        out.append(Check(
            id="D2", category="epistemic", name="Memory file freshness",
            status=PASS if not stale else WARN,
            message=f"{len(mems)} files, {len(stale)} stale (>14d)",
            data={"total": len(mems), "stale": stale},
        ))
    else:
        out.append(Check(id="D2", category="epistemic", name="Memory file freshness",
                         status=SKIP, message="no memory dir"))

    # D3: latest reflex checkpoint
    cp_dir = CODE_ROOT / ".empirica_reflex_logs" / "checkpoints"
    if cp_dir.exists():
        cps = [p for p in cp_dir.iterdir() if p.is_dir()]
        cps.sort(key=lambda p: p.stat().st_mtime)
        if cps:
            latest = cps[-1]
            age_min = int(file_age_seconds(latest) / 60)
            out.append(Check(
                id="D3", category="epistemic", name="Latest reflex checkpoint",
                status=PASS, message=f"{latest.name[:8]}… ({age_min}m ago)",
                data={"checkpoint": latest.name, "age_minutes": age_min,
                      "total_checkpoints": len(cps)},
            ))
        else:
            out.append(Check(id="D3", category="epistemic", name="Latest reflex checkpoint",
                             status=SKIP, message="no checkpoints"))
    else:
        out.append(Check(id="D3", category="epistemic", name="Latest reflex checkpoint",
                         status=SKIP, message="no dir"))

    return out


# ── Category E: Environment ────────────────────────────────────────────

def check_environment() -> List[Check]:
    out: List[Check] = []

    # E1: ~/Code disk usage
    rc, sout, _ = run(["du", "-sh", str(CODE_ROOT)], timeout=30)
    size = sout.split()[0] if rc == 0 and sout else "?"
    gb = 0.0
    try:
        unit = size[-1]
        val = float(size[:-1])
        if unit == "G":
            gb = val
        elif unit == "M":
            gb = val / 1000
        elif unit == "T":
            gb = val * 1000
    except Exception:
        pass
    st = PASS if 0 < gb < 20 else (WARN if gb < 25 else FAIL)
    out.append(Check(
        id="E1", category="env", name="~/Code disk usage",
        status=st, message=size,
        action=None if gb < 20 else "delete node_modules/build artifacts",
        data={"size_gb": gb, "raw": size},
    ))

    # E2/E6/E7 share ONE sweep of every discovered repo. `git status
    # --porcelain=v2 --branch` answers dirty AND ahead-of-upstream in a single
    # process per repo; 93 repos costs ~1s at 8 workers.
    #
    # E2 used to run `git status -s` against the ~/Code ROOT only and report
    # its line count as "~/Code git dirty" — a number that described one
    # directory while reading like it described the workspace. On 2026-08-05 it
    # said "16 changed paths" while the workspace actually held 361 across 38
    # repos.
    statuses = []
    sweep_err = None
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import harness_lib
        statuses = harness_lib.scan_repo_statuses(workspace=CODE_ROOT)
    except Exception as e:
        sweep_err = f"{type(e).__name__}: {e}"

    if sweep_err or not statuses:
        for cid, nm in (("E2", "~/Code working trees"),
                        ("E6", "Unpushed commits"),
                        ("E7", "Repos with no remote")):
            out.append(Check(id=cid, category="env", name=nm, status=SKIP,
                             message=sweep_err or "no repos discovered"))
    else:
        scanned = len(statuses)
        broken = [s for s in statuses if s["error"]]
        dirty_repos = [s for s in statuses if s["dirty"]]
        dirty_paths = sum(s["dirty"] for s in dirty_repos)
        worst = sorted(dirty_repos, key=lambda s: s["dirty"], reverse=True)[:10]

        # E2 is informational on purpose. An active workspace is dirty as its
        # resting state (38/93 repos, median 4 paths, on a normal day), so any
        # aggregate threshold here would fire every single morning — and a
        # check that always warns stops being read. The actionable signal is
        # E6: work that exists nowhere but this disk.
        out.append(Check(
            id="E2", category="env", name="~/Code working trees",
            status=PASS,
            message=f"{dirty_paths} uncommitted paths across "
                    f"{len(dirty_repos)}/{scanned} repos",
            data={
                "repos_scanned": scanned,
                "dirty_repos": len(dirty_repos),
                "dirty_paths": dirty_paths,
                "worst": [{"name": s["name"], "dirty": s["dirty"]} for s in worst],
                "unreadable": [s["name"] for s in broken],
            },
        ))

        # E6: commits that exist only on this machine. This is the check that
        # was missing entirely. On 2026-08-05 four commits sat unpushed in the
        # harness's own repo — including the fixes for two items still showing
        # as open on that day's plan — and a full 30-check preflight said
        # nothing about it.
        ahead = harness_lib.unpushed(statuses)
        total_ahead = sum(s["ahead"] for s in ahead)
        out.append(Check(
            id="E6", category="env", name="Unpushed commits",
            status=WARN if ahead else PASS,
            message=(f"{total_ahead} commit(s) unpushed in {len(ahead)} repo(s): "
                     + ", ".join(f"{s['name']}+{s['ahead']}" for s in ahead[:5])
                     if ahead else "all tracked branches pushed"),
            action="git push" if ahead else None,
            data={"total": total_ahead,
                  "repos": [{"name": s["name"], "ahead": s["ahead"],
                             "branch": s["branch"], "upstream": s["upstream"]}
                            for s in ahead]},
        ))

        # E7: committed work with nowhere to go. Not a WARN — having no remote
        # is a deliberate state for a scratch project, and nagging daily would
        # bury E6. Listed so it stays visible. Empty `git init` scaffolds are
        # excluded; they hold no work.
        orphan = harness_lib.no_upstream(statuses)
        out.append(Check(
            id="E7", category="env", name="Repos with no remote",
            status=PASS,
            message=(f"{len(orphan)} repo(s) with commits but no upstream: "
                     + ", ".join(s["name"] for s in orphan[:5])
                     if orphan else "every repo with commits has an upstream"),
            data={"repos": [{"name": s["name"], "dirty": s["dirty"]}
                            for s in orphan]},
        ))

    # E3: branch
    rc, sout, _ = run(["git", "-C", str(CODE_ROOT), "rev-parse", "--abbrev-ref", "HEAD"])
    branch = sout.strip() if rc == 0 else "?"
    out.append(Check(
        id="E3", category="env", name="~/Code branch",
        status=PASS if rc == 0 else SKIP,
        message=branch,
        data={"branch": branch},
    ))

    # E4: MCP diagnostic — splits local vs remote failures
    if MCP_DIAGNOSTIC.exists():
        rc, sout, _ = run([sys.executable, str(MCP_DIAGNOSTIC)], timeout=30)
        server_lines = [l.strip() for l in sout.splitlines()
                        if l.strip().startswith(("✅", "❌", "⚠️"))
                        and not any(k in l for k in ("OK:", "Warning:", "Error:", "Some servers"))]
        green       = sum(1 for l in server_lines if l.startswith("✅"))
        warn        = sum(1 for l in server_lines if l.startswith("⚠️"))
        red_local   = sum(1 for l in server_lines if l.startswith("❌") and "[REMOTE]" not in l)
        red_remote  = sum(1 for l in server_lines if l.startswith("❌") and "[REMOTE]" in l)
        # Only local failures block — remote failures are outside our control
        st = PASS if (red_local == 0 and warn == 0) else (WARN if red_local == 0 else FAIL)
        msg = f"{green} ok · {warn} warn · {red_local} local_fail · {red_remote} remote_fail"
        out.append(Check(
            id="E4", category="env", name="MCP servers",
            status=st, message=msg,
            data={"green": green, "warn": warn,
                  "local_fail": red_local, "remote_fail": red_remote},
        ))
    else:
        out.append(Check(id="E4", category="env", name="MCP servers",
                         status=SKIP, message="diagnostic script missing"))

    # E5: python interpreter
    in_venv = sys.prefix != sys.base_prefix or os.environ.get("VIRTUAL_ENV")
    out.append(Check(
        id="E5", category="env", name="Python interpreter",
        status=PASS, message=f"{sys.executable} ({'venv' if in_venv else 'system'})",
        data={"executable": sys.executable, "venv": bool(in_venv)},
    ))

    return out


# ── Category F: Work Context ───────────────────────────────────────────

def check_work_context() -> List[Check]:
    out: List[Check] = []

    # F1: recent devlog
    if DEVLOG.exists():
        rc, sout, _ = run(["tail", "-30", str(DEVLOG)])
        lines = [l for l in sout.split("\n") if l.strip()]
        out.append(Check(
            id="F1", category="work", name="Recent devlog (tail -30)",
            status=PASS, message=f"{len(lines)} non-empty lines",
            data={"preview": sout[-500:] if sout else ""},
        ))
    else:
        out.append(Check(id="F1", category="work", name="Recent devlog",
                         status=FAIL, message="devlog missing"))

    # F2: active work efforts
    we_dir = CODE_ROOT / "_work_efforts" / "10-19_development"
    if we_dir.exists():
        files = [f for f in we_dir.iterdir() if f.is_file() and f.suffix == ".md"]
        out.append(Check(
            id="F2", category="work", name="Active work efforts (10-19)",
            status=PASS, message=f"{len(files)} docs",
            data={"count": len(files)},
        ))
    else:
        out.append(Check(id="F2", category="work", name="Active work efforts",
                         status=SKIP, message="dir missing"))

    # F3: commits today across repos
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import commit_summary
        import harness_lib
        # Use the canonical discovery so this matches git_scanner's repo set
        # (root + top-level dirs + active/*), not just root + active/*.
        repo_paths = harness_lib.discover_repos(CODE_ROOT)
        result = commit_summary.summarize_today(repo_paths)
        # summarize_today returns {"repos": {name: {...}}, "total_commits": N, ...}
        # Read those keys directly; the top-level values are never lists.
        total = result.get("total_commits", 0)
        active_repos = sorted(result.get("repos", {}))
        out.append(Check(
            id="F3", category="work", name="Commits today across repos",
            status=PASS, message=f"{total} commits in {len(active_repos)} repos (scanned {len(repo_paths)})",
            data={"total": total, "active_repos": active_repos[:10],
                  "repos_scanned": len(repo_paths)},
        ))
    except Exception as e:
        out.append(Check(id="F3", category="work", name="Commits today across repos",
                         status=WARN, message=f"scan failed: {e}"))

    return out


# ── Category G: Readiness Gate ─────────────────────────────────────────

def compute_readiness(all_checks: List[Check]) -> Check:
    halts = [c for c in all_checks if c.status == HALT]
    fails = [c for c in all_checks if c.status == FAIL]
    warns = [c for c in all_checks if c.status == WARN]
    passes = [c for c in all_checks if c.status == PASS]
    total = len(all_checks)

    if halts:
        return Check(
            id="G1", category="readiness", name="Readiness",
            status=HALT, message=f"{len(halts)} blocker(s) — cannot proceed",
            action="resolve blockers before any write",
            data={"halts": [c.id for c in halts], "fails": [c.id for c in fails]},
        )

    score = len(passes) / total if total else 0
    if score >= 0.8:
        st, msg = PASS, f"{int(score * 100)}% pass — ready"
    elif score >= 0.5:
        st, msg = WARN, f"{int(score * 100)}% pass — warnings present"
    else:
        st, msg = FAIL, f"{int(score * 100)}% pass — degraded"

    return Check(
        id="G1", category="readiness", name="Readiness score",
        status=st, message=msg,
        data={"pass": len(passes), "warn": len(warns), "fail": len(fails),
              "halt": len(halts), "score": round(score, 2),
              "warnings": [c.id for c in warns]},
    )


def recommend_next(all_checks: List[Check]) -> str:
    by_id = {c.id: c for c in all_checks}

    if by_id.get("A1") and by_id["A1"].status == HALT:
        return "Fix daily_note.py import before anything else"
    if by_id.get("B1") and by_id["B1"].status == HALT:
        return "Vault missing — verify path"
    if by_id.get("B2") and by_id["B2"].status == HALT:
        return "URGENT: make GitHub repo private before any backup"
    if by_id.get("B3") and by_id["B3"].status == WARN:
        return "python3 spin_up.py  # create + fill today's note"

    b4 = by_id.get("B4")
    if b4 and b4.data.get("filled", 0) < 4:
        return "python3 spin_up.py --force  # fill empty sections"

    d1 = by_id.get("D1")
    if d1 and d1.status == WARN and d1.data.get("count", 0) > 1:
        return "close stale empirica transactions via postflight-submit"

    return "/hello  # harness ready, start work"


# ── Orchestrator ───────────────────────────────────────────────────────

def run_all(category_filter: Optional[str] = None,
            deep: bool = False,
            fail_fast: bool = False) -> Dict:
    checks: List[Check] = []
    categories = [
        ("A", lambda: check_harness_integrity(deep)),
        ("B", check_vault_and_note),
        ("C", lambda: check_data_sources(deep)),
        ("D", check_epistemic),
        ("E", check_environment),
        ("F", check_work_context),
    ]

    for cat, fn in categories:
        if category_filter and cat != category_filter:
            continue
        try:
            checks.extend(fn())
        except Exception as e:
            checks.append(Check(
                id=f"{cat}_ERR", category=cat, name=f"Category {cat} runner",
                status=FAIL, message=f"category crashed: {e}",
            ))
        if fail_fast and any(c.status == HALT for c in checks):
            break

    readiness = compute_readiness(checks)
    checks.append(readiness)
    recommendation = recommend_next(checks)

    return {
        "generated_at": datetime.now().isoformat(),
        "today": TODAY,
        "session_id": os.environ.get("EMPIRICA_SESSION_ID"),
        "readiness": asdict(readiness),
        "recommendation": recommendation,
        "checks": [asdict(c) for c in checks],
    }


# ── Pretty printer ─────────────────────────────────────────────────────

CAT_NAMES = {
    "harness": "A. Harness Integrity",
    "vault": "B. Vault & Daily Note",
    "data": "C. Data Sources",
    "epistemic": "D. Epistemic Context",
    "env": "E. Environment",
    "work": "F. Work Context",
    "readiness": "G. Readiness Gate",
}


def print_report(result: Dict):
    by_cat: Dict[str, List[Dict]] = {}
    for c in result["checks"]:
        by_cat.setdefault(c["category"], []).append(c)

    print()
    header = f" Spin-Up Preflight · {result['today']} "
    print("─" * 3 + header + "─" * max(3, 78 - len(header) - 3))
    print()

    for key in ["harness", "vault", "data", "epistemic", "env", "work", "readiness"]:
        if key not in by_cat:
            continue
        print(f"  {CAT_NAMES[key]}")
        for c in by_cat[key]:
            emoji = STATUS_EMOJI.get(c["status"], "·")
            name = c["name"]
            if len(name) > 36:
                name = name[:33] + "..."
            print(f"    {emoji} {c['id']:<4} {name:<38} {c['message']}")
        print()

    print("─" * 80)
    r = result["readiness"]
    print(f"  READINESS: {STATUS_EMOJI.get(r['status'], '·')} {r['message']}")
    print(f"  NEXT:      {result['recommendation']}")
    print()

    halts = [c for c in result["checks"] if c["status"] == HALT]
    fails = [c for c in result["checks"] if c["status"] == FAIL]
    if halts:
        print("  BLOCKERS:")
        for c in halts:
            print(f"    🛑 {c['id']} — {c['name']}: {c['message']}")
            if c.get("action"):
                print(f"       → {c['action']}")
        print()
    if fails:
        print("  FAILURES:")
        for c in fails:
            print(f"    ❌ {c['id']} — {c['name']}: {c['message']}")
            if c.get("action"):
                print(f"       → {c['action']}")
        print()


# ── CLI ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Daily Note Harness preflight checks")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--category", choices=list("ABCDEF"), help="Filter to one category")
    p.add_argument("--deep", action="store_true", help="Include network probes")
    p.add_argument("--fail-fast", action="store_true", help="Stop on first HALT")
    args = p.parse_args()

    result = run_all(args.category, args.deep, args.fail_fast)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_report(result)

    halts = [c for c in result["checks"] if c["status"] == HALT]
    sys.exit(1 if halts else 0)


if __name__ == "__main__":
    main()
