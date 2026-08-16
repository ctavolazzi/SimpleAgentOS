#!/usr/bin/env python3
"""Keep the Claude Code harness architecture doc alive across days.

The problem this solves: an architecture document is true on the day it is
written and quietly wrong a week later. This module measures the harness on a
schedule, decides whether anything *material* changed, and only then bumps a
version and writes a changelog row. A day where nothing structural moved leaves
the version alone, so a version bump keeps meaning something.

What counts as material (fingerprinted, bumps the version):
    the content of the always-loaded instruction files, the set of local skills,
    slash commands and hook scripts, which hooks are wired at which events,
    which plugins are installed and which are enabled, the MCP server registry,
    the load-bearing settings keys, and the memory corpus by filename and type.

What does NOT count (deliberately excluded, would bump every single day):
    transcript counts and bytes, total directory size, git dirty-file count,
    hook log row counts, backup rotation count. These are session exhaust. A
    document that reprints them as news trains its reader to stop reading it.

Verbs:
    python3 harness_architecture.py check      # measure, diff, report, no writes
    python3 harness_architecture.py update     # check, then write doc + baseline
    python3 harness_architecture.py status     # last known state, no probing

Called by spin_up.py once per morning. Safe to run by hand any time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import atomic_io

CLAUDE_DIR = Path.home() / ".claude"
PROBE = CLAUDE_DIR / "tools" / "harness_probe.py"
BASELINE = CLAUDE_DIR / "tools" / "harness-fingerprint.json"
RENDERED_PAGE = CLAUDE_DIR / "docs" / "architecture.html"
SPEC = CLAUDE_DIR / "tools" / "harness-architecture.spec.json"

VAULT = Path.home() / "Documents" / "Personal-Remote-Vault"
DOC = VAULT / "_work_efforts" / "40-49_architecture" / "40.12_claude_code_harness.md"

INITIAL_VERSION = "0.0.1"
MAX_CHANGELOG_ROWS = 40


# ---------------------------------------------------------------- measurement

def probe() -> dict:
    """Run the read-only probe in ~/.claude/tools and return its JSON."""
    if not PROBE.exists():
        raise FileNotFoundError(
            f"{PROBE} is missing. It is the measurement source for this doc; "
            "without it nothing here can be trusted. Re-create it before running."
        )
    # Write to a real temp file rather than /dev/stdout: the probe writes with
    # write_text, and a caller that mixes summary text into the JSON stream is
    # a parse error waiting for the day someone drops --quiet.
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        out = subprocess.run(
            [sys.executable, str(PROBE), "--out", str(tmp_path), "--quiet"],
            capture_output=True, text=True, timeout=300,
        )
        if out.returncode != 0:
            raise RuntimeError(f"probe failed rc={out.returncode}: {out.stderr[:400]}")
        return json.loads(tmp_path.read_text(encoding="utf-8"))
    finally:
        tmp_path.unlink(missing_ok=True)


def _script_name(command: str) -> str:
    """Pull the hook script filename out of a shell command line.

    The wired command is `bash "$HOME/.claude/hooks/live-feed.sh" record`, so
    naive Path(...).name yields `live-feed.sh" record`. Match the extension
    instead of trusting the string to be a bare path.
    """
    m = re.search(r'([\w.-]+\.(?:sh|py|js|ts))', command)
    return m.group(1) if m else command.strip()[:40]


def _esc(s: str) -> str:
    """Escape a value going into a markdown table cell.

    A hook matcher like `Bash|Edit|Write` is a regex alternation, and an
    unescaped pipe silently splits the row into extra columns.
    """
    return str(s).replace("|", "\\|")


def _hash_file(p: Path) -> Optional[str]:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def fingerprint(d: dict) -> dict:
    """Reduce a probe to the facts whose change is worth a version bump.

    Every value here is either a hash, a sorted name list, or a small count that
    only moves when someone changes the harness on purpose. Nothing in here
    moves because a session happened.
    """
    s, m, sk, pl, mcp, hk = (d["settings"], d["memory"], d["skills"],
                             d["plugins"], d["mcp"], d["hooks"])

    return {
        # The three files that load into context with no action taken.
        "context_files": {
            "CLAUDE.md": _hash_file(CLAUDE_DIR / "CLAUDE.md"),
            "empirica-system-prompt.md": _hash_file(CLAUDE_DIR / "empirica-system-prompt.md"),
            "MEMORY.md": _hash_file(Path(m["path"]) / "MEMORY.md") if m.get("present") else None,
            "OPUS_OS.md": _hash_file(CLAUDE_DIR / "OPUS_OS.md"),
        },
        # Capability surface, by name. A renamed skill is a real change.
        "skills": sk["local"],
        "commands": d["commands"],
        "plugin_skill_total": sk["plugin_skill_total"],
        # Which plugins exist and which of them actually run.
        "plugins_installed": sorted(p["id"] for p in pl["plugins"]),
        "plugins_enabled": sorted(p["id"] for p in pl["plugins"] if p["enabled"]),
        # Out-of-process reach.
        "mcp_global": mcp["global_servers"],
        "mcp_orphaned": mcp["claude_dir_mcp_servers"],
        # The gate layer: scripts present, and where each is wired.
        "hook_scripts": hk["scripts_on_disk"],
        "hooks_wired": sorted(f"{w['event']}:{w['matcher']}:{_script_name(w['command'])}"
                              for w in hk["wired"]),
        "dormant_hook_count": len(hk["plugin_hooks_available"]),
        # Settings that change how every session behaves.
        "settings": {
            "effortLevel": s["effortLevel"],
            "autoCompactEnabled": s["autoCompactEnabled"],
            "verbose": s["verbose"],
            "remoteControlAtStartup": s["remoteControlAtStartup"],
        },
        # Memory by identity and type, never by byte count.
        # NOTE: a plain dict, deliberately. An earlier version used
        # sorted(...items()), which is a list of TUPLES in memory and a list of
        # LISTS after a JSON round-trip. The baseline is stored as JSON, so
        # every single run compared tuples against lists, reported a phantom
        # change, and bumped the version daily. Dict equality in Python is
        # order-insensitive, so this is both stable and JSON-safe.
        "memory_types": m.get("by_type", {}) if m.get("present") else {},
        "memory_count": m.get("fact_files", 0),
        "memory_index_rows": m.get("index_rows", 0),
    }


def assert_json_stable(fp: dict) -> None:
    """A fingerprint that changes shape through JSON bumps the version forever.

    The baseline lives on disk as JSON, so `fingerprint(probe())` must equal
    itself after a serialize/deserialize round-trip. Tuples, sets, and Paths all
    violate this silently. Called on every check so the failure is loud.
    """
    if json.loads(json.dumps(fp)) != fp:
        bad = [k for k in fp if json.loads(json.dumps({k: fp[k]}))[k] != fp[k]]
        raise TypeError(
            f"fingerprint is not JSON-stable, so it would report a phantom "
            f"change on every run. Offending key(s): {bad}"
        )


# ---------------------------------------------------------------------- diff

def _describe(key: str, old, new) -> list[str]:
    """Turn one fingerprint key's change into readable lines."""
    if isinstance(old, list) and isinstance(new, list) and all(
            isinstance(x, str) for x in old + new):
        added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
        out = []
        if added:
            out.append(f"{key}: added {', '.join(added)}")
        if removed:
            out.append(f"{key}: removed {', '.join(removed)}")
        return out
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in sorted(set(old) | set(new)):
            if old.get(k) != new.get(k):
                if key == "context_files":
                    out.append(f"{k} edited")
                else:
                    out.append(f"{key}.{k}: {old.get(k)} -> {new.get(k)}")
        return out
    return [f"{key}: {old} -> {new}"]


def diff(old_fp: dict, new_fp: dict) -> list[str]:
    changes: list[str] = []
    for key in sorted(set(old_fp) | set(new_fp)):
        if old_fp.get(key) != new_fp.get(key):
            changes.extend(_describe(key, old_fp.get(key), new_fp.get(key)))
    return changes


def _bump(version: str) -> str:
    """0.0.1 -> 0.0.2. House rule: versions start at 0.0.1, never 1.0."""
    try:
        parts = [int(x) for x in version.split(".")]
    except ValueError:
        return INITIAL_VERSION
    while len(parts) < 3:
        parts.append(0)
    parts[2] += 1
    return ".".join(str(p) for p in parts[:3])


def load_baseline() -> Optional[dict]:
    try:
        return json.loads(BASELINE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------- doc writing

def _fmt_bytes(n: int) -> str:
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def render_doc(d: dict, version: str, changed_at: str, checked_at: str,
               changelog: list[dict], page_stale: bool) -> str:
    m, sk, pl, mcp, hk, s = (d["memory"], d["skills"], d["plugins"],
                             d["mcp"], d["hooks"], d["settings"])
    g, t = d["git"], d["transcripts"]

    enabled = [p["id"] for p in pl["plugins"] if p["enabled"]]
    disabled = [p["id"] for p in pl["plugins"] if not p["enabled"]]
    types = m.get("by_type", {})

    stale_line = (
        "> [!warning] The rendered page is behind this doc.\n"
        f"> Material change was detected on {changed_at} and the narrative page at\n"
        f"> `~/.claude/docs/architecture.html` has not been regenerated since. The numbers\n"
        "> below are current; the prose on that page may not be. Run `/architecture` to redraw it,\n"
        "> then `python3 harness_architecture.py clear-stale` to drop this warning.\n"
        if page_stale else
        "> [!info] Numbers below are measured, not remembered.\n"
        f"> Refreshed {checked_at} by `harness_architecture.py`, called from `spin_up.py`.\n"
        "> Version bumps only when something structural moves, so a bump means something.\n"
    )

    rows = "\n".join(
        f"| {c['version']} | {c['date']} | {c['summary']} |" for c in reversed(changelog)
    ) or "| 0.0.1 | (initial) | Baseline captured |"

    plugin_rows = "\n".join(
        f"| {p['id']} | {p['version']} | {'**enabled**' if p['enabled'] else 'off'} |"
        for p in pl["plugins"]
    )

    hook_rows = "\n".join(
        f"| {w['event']} | `{_esc(w['matcher'])}` | `{_script_name(w['command'])}` |"
        for w in hk["wired"]
    )

    return f"""---
type: work_effort
id: "40.12"
title: Claude Code Harness Architecture
project: Daily Note OS
status: living
version: {version}
created: 2026-08-06
updated: {checked_at}
last_material_change: {changed_at}
parent: "[[Daily Notes/{datetime.now():%Y-%m-%d}]]"
hub: "[[Hubs/{datetime.now():%Y-%m-%d}_hub]]"
related:
  - "[[System/00-09_system_meta/00.13_daily_note_os]]"
  - "[[System/00-09_system_meta/00.14_daily_note_os_evolution]]"
  - "[[10.53_harness_silent_failure_audit]]"
code_refs:
  - _experiments/SimpleAgentOS/harness_architecture.py
  - _experiments/SimpleAgentOS/spin_up.py
tags:
  - architecture
  - daily-note-os
  - claude-code
  - living-document
---

# 40.12 Claude Code Harness Architecture

**Living document, v{version}.** Self-updating. Do not hand-edit the measured
sections below; `harness_architecture.py` overwrites them. Add commentary under
Notes, which is preserved.

{stale_line}
**Rendered map:** `~/.claude/docs/architecture.html` (open it for the system map,
the 3D stack, runtime flows, and the seams section)
**Spec:** `~/.claude/tools/harness-architecture.spec.json`
**Probe:** `~/.claude/tools/harness_probe.py`

---

## What The Harness Is

Six layers, top to bottom. What loads into context at turn zero, what is read on
demand, what can be invoked, what wraps every tool call, what runs out of
process, and what survives the session.

The one-line version: `CLAUDE.md` is a single `@import`, so effectively all
standing instruction lives in `empirica-system-prompt.md`. `MEMORY.md` is the
only part of the memory system always in context; the fact bodies open on demand.

---

## Measured State

*Refreshed {checked_at}.*

| Layer | Current |
|---|---|
| Context at turn zero | {s['claude_md_bytes']} B CLAUDE.md, {s['empirica_prompt_bytes']:,} B Empirica prompt, {m.get('index_rows', 0)} MEMORY.md rows |
| Memory corpus | {m.get('fact_files', 0)} facts ({', '.join(f"{v} {k}" for k, v in sorted(types.items(), key=lambda kv: -kv[1]))}) |
| Memory graph | {m.get('files_with_wikilinks', 0)} files wikilinked, {len(m.get('dangling_link_targets', []))} dangling targets |
| Capability | {sk['local_count']} local skills ({sk['local_with_executable_scripts']} ship scripts), {len(d['commands'])} slash commands, {sk['plugin_skill_total']} plugin skills |
| Plugins | {len(enabled)} enabled of {len(pl['plugins'])} installed |
| Gates | {hk['wired_count']} hooks wired, {len(hk['plugin_hooks_available'])} dormant |
| MCP | live: {', '.join(mcp['global_servers']) or 'none'} · orphaned in `~/.claude/mcp.json`: {', '.join(mcp['claude_dir_mcp_servers']) or 'none'} |
| Settings | effort `{s['effortLevel']}`, autoCompact `{s['autoCompactEnabled']}`, verbose `{s['verbose']}` |
| Version control | {g['tracked_files']} tracked, {g['dirty_files']} dirty, {g['commits']} commits, HEAD `{g['head']}` |
| On disk | {_fmt_bytes(d['total_bytes'])} total, {t['total_transcripts']} transcripts ({_fmt_bytes(t['total_bytes'])}) |

### Plugins

| Plugin | Version | State |
|---|---|---|
{plugin_rows}

### Hooks wired

| Event | Matcher | Script |
|---|---|---|
{hook_rows}

---

## Standing Seams

These are structural, so they persist until someone fixes them. The rendered
page carries the full reasoning for each.

1. **The memory corpus has no copy anywhere.** `.gitignore` opens `/*` and
   re-includes six paths; `projects/` is not one of them, and `backups/` holds
   only `.claude.json` rotations. Currently {m.get('fact_files', 0)} files.
2. **Injected protocol without enforcement.** The Empirica prompt loads every
   session; the {len(hk['plugin_hooks_available'])} hooks that would enforce it belong to a plugin that is
   {'enabled' if 'empirica@local' in enabled else 'not enabled'}.
3. **Memory wikilink naming is split.** Filenames are snake_case, frontmatter
   `name:` slugs are kebab-case. Dangling targets right now: {len(m.get('dangling_link_targets', []))}.
4. **`~/.claude/mcp.json` is at a path Claude Code does not read.** It reads
   `~/.claude.json`, project `.mcp.json`, and enabled plugins.

---

## Changelog

Rows are appended only when the fingerprint moves. A quiet day writes nothing.

| Version | Date | What moved |
|---|---|---|
{rows}

---

## Notes

*Hand-written. Preserved across updates.*

<!-- notes-start -->
<!-- notes-end -->

---

*Auto-maintained by [[_experiments/SimpleAgentOS/harness_architecture|harness_architecture.py]],
called from [[_experiments/SimpleAgentOS/spin_up|spin_up.py]] each morning.*
"""


def _preserve_notes(new_doc: str) -> str:
    """Carry the hand-written Notes block across a regeneration."""
    if not DOC.exists():
        return new_doc
    try:
        old = DOC.read_text(encoding="utf-8")
    except OSError:
        return new_doc
    start, end = old.find("<!-- notes-start -->"), old.find("<!-- notes-end -->")
    if start == -1 or end == -1 or end < start:
        return new_doc
    kept = old[start + len("<!-- notes-start -->"):end]
    if not kept.strip():
        return new_doc
    return new_doc.replace(
        "<!-- notes-start -->\n<!-- notes-end -->",
        f"<!-- notes-start -->{kept}<!-- notes-end -->",
    )


# ------------------------------------------------------------------- verbs

def check(update: bool = False) -> dict:
    """Measure, compare, and optionally persist. Returns a result dict."""
    now = datetime.now()
    checked_at = now.strftime("%Y-%m-%d %H:%M")
    today = now.strftime("%Y-%m-%d")

    data = probe()
    new_fp = fingerprint(data)
    assert_json_stable(new_fp)
    base = load_baseline()

    if base is None:
        version, changed_at, changes, first = INITIAL_VERSION, today, [], True
    else:
        first = False
        changes = diff(base.get("fingerprint", {}), new_fp)
        if changes:
            version = _bump(base.get("version", INITIAL_VERSION))
            changed_at = today
        else:
            version = base.get("version", INITIAL_VERSION)
            changed_at = base.get("changed_at", today)

    changelog = list(base.get("changelog", [])) if base else []
    page_stale = bool(base.get("page_stale", False)) if base else False

    if changes or first:
        summary = ("Baseline captured" if first
                   else "; ".join(changes[:4]) + (f" (+{len(changes) - 4} more)"
                                                  if len(changes) > 4 else ""))
        changelog.append({"version": version, "date": today, "summary": summary})
        changelog = changelog[-MAX_CHANGELOG_ROWS:]
        if not first:
            page_stale = True

    result = {
        "version": version,
        "changed": bool(changes),
        "first_run": first,
        "changes": changes,
        "checked_at": checked_at,
        "changed_at": changed_at,
        "page_stale": page_stale,
        "written": False,
    }

    if not update:
        return result

    doc = render_doc(data, version, changed_at, checked_at, changelog, page_stale)
    doc = _preserve_notes(doc)
    DOC.parent.mkdir(parents=True, exist_ok=True)
    with atomic_io.vault_lock():
        atomic_io.atomic_write(DOC, doc)

    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.atomic_write(BASELINE, json.dumps({
        "version": version,
        "changed_at": changed_at,
        "checked_at": checked_at,
        "page_stale": page_stale,
        "changelog": changelog,
        "fingerprint": new_fp,
    }, indent=2))
    result["written"] = True
    return result


def clear_stale() -> bool:
    """Call after the narrative page is regenerated, so the warning drops off."""
    base = load_baseline()
    if not base or not base.get("page_stale"):
        return False
    base["page_stale"] = False
    atomic_io.atomic_write(BASELINE, json.dumps(base, indent=2))
    return True


def spin_up_check() -> Tuple[bool, str]:
    """Entry point for spin_up.py. Never raises; a bad probe must not stop a morning."""
    try:
        r = check(update=True)
        if r["first_run"]:
            return True, f"baseline captured (v{r['version']})"
        if r["changed"]:
            n = len(r["changes"])
            return True, f"v{r['version']}: {n} change(s), {r['changes'][0][:60]}"
        return True, f"unchanged (v{r['version']}, last moved {r['changed_at']})"
    except Exception as e:
        return False, f"failed ({type(e).__name__}): {e}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("verb", nargs="?", default="check",
                    choices=["check", "update", "status", "clear-stale"])
    args = ap.parse_args()

    if args.verb == "status":
        base = load_baseline()
        if not base:
            print("no baseline yet. Run: harness_architecture.py update")
            return
        print(f"version      {base['version']}")
        print(f"last change  {base['changed_at']}")
        print(f"last check   {base.get('checked_at', '?')}")
        print(f"page stale   {base.get('page_stale')}")
        print(f"doc          {DOC}")
        for c in base.get("changelog", [])[-5:]:
            print(f"  {c['version']}  {c['date']}  {c['summary'][:80]}")
        return

    if args.verb == "clear-stale":
        print("stale flag cleared" if clear_stale() else "nothing to clear")
        return

    r = check(update=(args.verb == "update"))
    print(f"version {r['version']}  checked {r['checked_at']}")
    if r["first_run"]:
        print("first run: baseline captured, no version bump")
    elif r["changed"]:
        print(f"{len(r['changes'])} material change(s):")
        for c in r["changes"]:
            print(f"  · {c}")
    else:
        print(f"no material change since {r['changed_at']}")
    if r["written"]:
        print(f"wrote {DOC}")
    else:
        print("(check only, nothing written. Use `update` to persist)")


if __name__ == "__main__":
    main()
