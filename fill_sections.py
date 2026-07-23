"""
fill_sections.py — Fill daily note sections that preflight flags as empty.

Targets (B9 warning set):
  commits_today     — git commits since midnight across ~/Code repos
  work_efforts      — active work efforts from vault _work_efforts_/
  tomorrows_top_3   — carry forward from yesterday, or blank checkboxes
  sitrep            — active threads + blockers stub

Usage:
  python3 fill_sections.py                           # fill all empty targets
  python3 fill_sections.py --section commits_today   # single section
  python3 fill_sections.py --force                   # overwrite filled too
  python3 fill_sections.py --dry-run                 # print, no writes
  python3 fill_sections.py --include in_the_lab      # opt-in epistemic section
  python3 fill_sections.py --json                    # machine-readable output
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# Harness path resolution
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import daily_note
import commit_summary
import harness_lib

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
WORK_EFFORTS_DIR = VAULT_DIR / "_work_efforts_"

# Default fillable targets. in_the_lab is opt-in only.
TARGETS = ("commits_today", "work_efforts", "tomorrows_top_3", "sitrep")


# ── Section builders ─────────────────────────────────────────────────────────

def _build_commits_today() -> str:
    repos = harness_lib.discover_repos()
    summary = commit_summary.summarize_today(repos)
    return commit_summary.format_markdown(summary)


def _build_work_efforts() -> str:
    """Scan _work_efforts_ for in_progress / exploring efforts, format as list."""
    lines = []
    if not WORK_EFFORTS_DIR.exists():
        return "*Work efforts directory not found.*"

    for md in sorted(WORK_EFFORTS_DIR.rglob("*.md")):
        if md.name.startswith("ARCHIVED") or md.name.startswith("00.00"):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        # Check status in frontmatter
        status_match = re.search(r"^status:\s*(\S+)", text, re.MULTILINE)
        if not status_match:
            continue
        status = status_match.group(1).strip('"').strip("'")
        if status not in ("in_progress", "exploring", "blocked"):
            continue
        # Get title from first H1 or filename
        title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else md.stem
        # Build vault-relative wikilink
        rel = md.relative_to(VAULT_DIR).with_suffix("")
        lines.append(f"- [[{rel}|{title}]] — `{status}`")

    if not lines:
        return "*No active work efforts found.*"
    return "\n".join(lines)


def _build_tomorrows_top_3() -> str:
    """Carry forward from yesterday's tomorrows_top_3, or return blank checkboxes."""
    yesterday_content = daily_note.read_yesterday("tomorrows_top_3")
    # Extract unchecked items
    items = re.findall(r"^- \[ \] (.+)$", yesterday_content, re.MULTILINE)
    if items:
        return "\n".join(f"- [ ] {item}" for item in items[:3])
    return "- [ ] \n- [ ] \n- [ ] "


def _build_sitrep() -> str:
    """Stub sitrep from active work efforts."""
    threads = []
    if WORK_EFFORTS_DIR.exists():
        for md in sorted(WORK_EFFORTS_DIR.rglob("*.md")):
            if md.name.startswith("ARCHIVED"):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            status_match = re.search(r"^status:\s*(\S+)", text, re.MULTILINE)
            if not status_match:
                continue
            status = status_match.group(1).strip('"').strip("'")
            if status not in ("in_progress", "exploring"):
                continue
            title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else md.stem
            threads.append(f"- {title}")

    thread_block = "\n".join(threads[:5]) if threads else "-"
    ts = datetime.now().strftime("%H:%M")
    return (
        f"**Status:** In progress — filled by fill_sections @ {ts}\n\n"
        f"**Active threads:**\n{thread_block}\n\n"
        f"**Blockers:** None\n\n"
        f"**Music:**"
    )


def _build_in_the_lab() -> str:
    """Opt-in only. Stub — user fills content."""
    ts = datetime.now().strftime("%H:%M")
    return f"<!-- fill_sections stub @ {ts} — replace with architectural decisions -->"


# ── Builder registry ─────────────────────────────────────────────────────────

_BUILDERS = {
    "commits_today":    _build_commits_today,
    "work_efforts":     _build_work_efforts,
    "tomorrows_top_3":  _build_tomorrows_top_3,
    "sitrep":           _build_sitrep,
    "in_the_lab":       _build_in_the_lab,
}


# ── Main API ─────────────────────────────────────────────────────────────────

def fill(
    *,
    sections: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    include_lab: bool = False,
) -> dict:
    """
    Fill empty daily note sections.

    Returns {filled: [...], skipped: [...], errors: [...], generated_at}
    """
    targets = list(sections) if sections else list(TARGETS)
    if include_lab and "in_the_lab" not in targets:
        targets.append("in_the_lab")

    result = {"filled": [], "skipped": [], "errors": [], "generated_at": harness_lib.iso_now()}

    try:
        statuses = daily_note.section_status()
    except Exception as e:
        result["errors"].append({"section": "ALL", "error": str(e)})
        return result

    for section in targets:
        if section not in _BUILDERS:
            result["errors"].append({"section": section, "error": "no builder"})
            continue

        status = statuses.get(section, "absent")

        if status == "absent":
            result["skipped"].append({"section": section, "reason": "absent from note"})
            continue

        if status == "filled" and not force:
            result["skipped"].append({"section": section, "reason": "already filled"})
            continue

        try:
            content = _BUILDERS[section]()
        except Exception as e:
            result["errors"].append({"section": section, "error": f"builder failed: {e}"})
            continue

        if dry_run:
            result["filled"].append({"section": section, "dry_run": True, "preview": content[:120]})
            continue

        try:
            daily_note.write_section(section, content, actor="claude")
            result["filled"].append({"section": section, "status": "ok"})
        except Exception as e:
            result["errors"].append({"section": section, "error": f"write failed: {e}"})

    return result


def format_result_md(result: dict) -> str:
    lines = [f"## fill_sections — {result['generated_at']}\n"]
    if result["filled"]:
        lines.append("**Filled:**")
        for item in result["filled"]:
            tag = " (dry-run)" if item.get("dry_run") else ""
            lines.append(f"  - `{item['section']}`{tag}")
    if result["skipped"]:
        lines.append("**Skipped:**")
        for item in result["skipped"]:
            lines.append(f"  - `{item['section']}` — {item['reason']}")
    if result["errors"]:
        lines.append("**Errors:**")
        for item in result["errors"]:
            lines.append(f"  - `{item['section']}`: {item['error']}")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fill daily note sections.")
    parser.add_argument("--section", action="append", dest="sections",
                        help="Section to fill (repeatable). Default: all targets.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite already-filled sections.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print content, no writes.")
    parser.add_argument("--include", action="append", dest="include",
                        help="Opt-in extras: in_the_lab")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Output JSON.")
    args = parser.parse_args()

    include_lab = "in_the_lab" in (args.include or [])
    result = fill(
        sections=args.sections,
        force=args.force,
        dry_run=args.dry_run,
        include_lab=include_lab,
    )

    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(format_result_md(result))


if __name__ == "__main__":
    main()
