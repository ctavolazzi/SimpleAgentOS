#!/usr/bin/env python3
"""
we_factory.py — Work Effort auto-creator for SimpleAgentOS harness.

Creates WE markdown files from tasks using the LLM pipeline, links into daily notes,
handles dedup via source_hash, manages number allocation, merges frontmatter.

Usage:
    from we_factory import create, create_for_quests, create_for_top3
    result = create("Task text", priority="high")
    print(result['wikilink'])
"""

import os
import re
import hashlib
from pathlib import Path
from typing import Optional, TypedDict
from datetime import datetime
import llm_pipeline
import daily_note
import atomic_io


# ── Configuration ──────────────────────────────────────────────────────────

WE_DIR = Path(
    os.environ.get("WE_FACTORY_DIR")
    or Path.home()
    / "Documents/Personal-Remote-Vault/_work_efforts_/10-19_development/10_core"
)
WE_PREFIX_RE = re.compile(r"^(\d{2})\.(\d{2})_\d{8}_.+\.md$")


class WEResult(TypedDict):
    status: str  # "created" | "exists" | "skipped_not_worthy" | "unlinked" | "overwritten" | "failed" | "dry_run"
    number: str
    slug: str
    filename: str
    wikilink: str
    source_hash: str
    path: Optional[Path]
    pipeline_source: str
    worthy: bool
    significance_reason: str
    suggested_parent: Optional[str]
    reason: Optional[str]


# ── Helpers ────────────────────────────────────────────────────────────────


def _normalize(task: str) -> str:
    """Normalize task for hashing: lowercase, strip punct, drop checkboxes, collapse ws."""
    s = task.lower()
    s = re.sub(r"^[-\[\]xX\s]*", "", s)  # drop checkbox markers
    s = re.sub(r"\*\(carried\)\*", "", s)  # drop carry-forward marker
    s = re.sub(r"[^\w\s]", " ", s)  # strip punct
    s = re.sub(r"\s+", " ", s).strip()  # collapse ws
    return s


def _normalize_date(date_str: str) -> tuple[str, str]:
    """Parse date string (any format) → (YYYYMMDD, YYYY-MM-DD). Used for WE filenames vs daily note refs."""
    if not date_str:
        now = datetime.now()
        return now.strftime("%Y%m%d"), now.strftime("%Y-%m-%d")
    # If already YYYYMMDD, convert to hyphenated for daily note
    if len(date_str) == 8 and date_str.isdigit():
        yyyymmdd = date_str
        yyyy_mm_dd = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        return yyyymmdd, yyyy_mm_dd
    # If hyphenated YYYY-MM-DD, convert to compact for filename
    if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
        yyyy_mm_dd = date_str
        yyyymmdd = date_str.replace("-", "")
        return yyyymmdd, yyyy_mm_dd
    # Fallback
    return datetime.now().strftime("%Y%m%d"), datetime.now().strftime("%Y-%m-%d")


def _source_hash(task: str) -> str:
    """Generate 12-char source hash for dedup."""
    normalized = _normalize(task)
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def next_we_number(dir_: Path = WE_DIR) -> str:
    """Scan dir for highest NN.MM prefix, return next as '10.NN'."""
    if not dir_.exists():
        dir_.mkdir(parents=True, exist_ok=True)
        return "10.01"
    max_minor = 0
    for p in dir_.iterdir():
        if not p.is_file():
            continue
        m = WE_PREFIX_RE.match(p.name)
        if not m:
            continue
        major, minor = int(m.group(1)), int(m.group(2))
        if major == 10:
            max_minor = max(max_minor, minor)
    return f"10.{max_minor + 1:02d}"


def we_exists_for(task: str, dir_: Path = WE_DIR) -> Optional[Path]:
    """Check if WE with same source_hash exists. Return path or None."""
    hash_ = _source_hash(task)
    pattern = f'source_hash: "{hash_}"'
    if not dir_.exists():
        return None
    for p in dir_.glob("*.md"):
        if not p.is_file():
            continue
        try:
            content = p.read_text()
            if pattern in content:
                return p
        except Exception:
            pass
    return None


def gather_context(dir_: Path = WE_DIR) -> dict:
    """Gather recent WE titles + harness activity for Haiku context."""
    recent_titles = []
    if dir_.exists():
        files = sorted(dir_.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        for p in files:
            try:
                content = p.read_text()
                m = re.search(r'title:\s*"([^"]+)"', content)
                if m:
                    recent_titles.append(m.group(1))
            except Exception:
                pass
    recent_titles = recent_titles[:5]

    recent_activity = "(none)"
    try:
        daily_path = Path.home() / "Documents/Personal-Remote-Vault/Daily Notes"
        today_file = daily_path / f"{datetime.now().strftime('%Y-%m-%d')}.md"
        if today_file.exists():
            content = today_file.read_text()
            lines = content.split("\n")[50:100]
            recent_activity = "\n".join(lines[:20])
    except Exception:
        pass

    return {
        "recent_we_titles": recent_titles,
        "recent_activity": recent_activity,
    }


# Atomic write delegated to atomic_io.atomic_write; lock acquired by callers
# at the vault-root level so the WE file write and any concurrent daily-note
# mutation by a co-running MCP tool are queued cleanly.


def _build_frontmatter(
    title: str,
    priority: str,
    number: str,
    slug: str,
    tags: list,
    source_hash: str,
    date: str,
    parent: str,
    pipeline_source: str,
    worthy: bool,
    significance_reason: str,
    auto_source: str,
) -> str:
    """Build YAML frontmatter."""
    today = date or datetime.now().strftime("%Y-%m-%d")
    created = datetime.now().isoformat() + "Z"
    tags_yaml = "\n".join(f"  - {t}" for t in (tags or ["general"]))

    return f"""---
title: "{title}"
status: "active"
priority: "{priority}"
created: "{created}"
last_updated: "{created}"
phase: "Planning"
category: "10-19"
tags:
{tags_yaml}
daily_note: "[[{today}]]"
parent: "{parent or ''}"
source_hash: "{source_hash}"
auto_source: "{auto_source}"
draft_provider: "{pipeline_source}"
significance_reason: "{significance_reason}"
---
"""


def create(
    task: str,
    *,
    priority: str = "medium",
    parent: Optional[str] = None,
    date: Optional[str] = None,
    number: Optional[str] = None,
    auto_source: str = "manual",
    dry_run: bool = False,
    no_save: bool = False,
    overwrite: bool = False,
) -> WEResult:
    """Create a WE. Return WEResult with status, wikilink, etc."""

    hash_ = _source_hash(task)

    # Dedup check
    existing = we_exists_for(task)
    if existing and not overwrite:
        return WEResult(
            status="exists",
            number="",
            slug="",
            filename="",
            wikilink=f"[[{existing.stem}]]",
            source_hash=hash_,
            path=existing,
            pipeline_source="",
            worthy=True,
            significance_reason="duplicate",
            suggested_parent=None,
            reason="task already has WE",
        )

    # Pipeline: draft → sanitize → judge & patch
    context = gather_context()
    simulate = dry_run or os.environ.get("WE_FACTORY_SIMULATE") == "1"
    result = llm_pipeline.gate_and_draft(task, field_spec=llm_pipeline.WE_FIELD_SPEC, context=context, simulate=simulate)

    # Significance gate
    if not result["worthy"]:
        return WEResult(
            status="skipped_not_worthy",
            number="",
            slug=result["content"].get("slug", ""),
            filename="",
            wikilink="",
            source_hash=hash_,
            path=None,
            pipeline_source=result["source"],
            worthy=False,
            significance_reason=result["reason"],
            suggested_parent=result.get("suggested_parent"),
            reason=result["reason"],
        )

    # Allocate number
    if not number:
        number = next_we_number()

    # Determine parent
    final_parent = parent or result.get("suggested_parent") or ""

    # Build WE content
    slug = result["content"].get("slug", "untitled")
    title = result["content"].get("title", "Untitled")
    tags = result["content"].get("tags", ["general"])
    plan_body = result["content"].get("plan_body", "_To be filled in._")

    yyyymmdd, yyyy_mm_dd = _normalize_date(date)
    filename = f"{number}_{yyyymmdd}_{slug}.md"
    filepath = WE_DIR / filename

    fm = _build_frontmatter(
        title=title,
        priority=priority,
        number=number,
        slug=slug,
        tags=tags,
        source_hash=hash_,
        date=yyyy_mm_dd,
        parent=final_parent,
        pipeline_source=result["source"],
        worthy=True,
        significance_reason=result["reason"],
        auto_source=auto_source,
    )

    body = f"""# WE {number} — {title}

> Auto-created by `we_factory` from {auto_source} on {yyyy_mm_dd}.
> Pipeline: {result["source"]}. Sanitizer issues: {len(result["sanitized_issues"])}. Verify patches: {len(result["verify_patches"])}.

## Task

{task}

## Plan

{plan_body}

## Notes
"""

    content = fm + body

    if not no_save and not dry_run:
        WE_DIR.mkdir(parents=True, exist_ok=True)
        with atomic_io.vault_lock():
            atomic_io.atomic_write(filepath, content)
            if overwrite and existing:
                try:
                    existing.unlink()
                except OSError:
                    pass

    wikilink = f"[[{number}_{yyyymmdd}_{slug}]]"

    return WEResult(
        status="dry_run" if dry_run else "created",
        number=number,
        slug=slug,
        filename=filename,
        wikilink=wikilink,
        source_hash=hash_,
        path=filepath if not dry_run else None,
        pipeline_source=result["source"],
        worthy=True,
        significance_reason=result["reason"],
        suggested_parent=final_parent,
        reason=None,
    )


def link_into_daily(wikilinks: list[str], date: Optional[str] = None) -> dict:
    """Merge wikilinks into daily note frontmatter work_efforts list."""
    try:
        text = daily_note.read_full(date)

        # Extract current list via regex
        fm_match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
        if not fm_match:
            return {"status": "no_frontmatter", "linked": 0}

        fm_text = fm_match.group(1)

        # Find work_efforts field
        we_match = re.search(r'^work_efforts:\s*\n((?:[ \t]+-.*\n)*)', fm_text, re.MULTILINE)
        existing = []
        if we_match:
            for line in we_match.group(1).splitlines():
                item = line.strip().lstrip("-").strip().strip('"').strip("'")
                if item:
                    existing.append(item)

        # Merge, dedup on wikilink target
        seen = {e.strip("[]") for e in existing}
        merged = list(existing)
        for link in wikilinks:
            key = link.strip("[]")
            if key not in seen:
                merged.append(link)
                seen.add(key)

        # Update via daily_note module
        daily_note.update_frontmatter_fields({"work_efforts": merged}, date=date)
        return {"status": "linked", "linked": len([w for w in wikilinks if w.strip("[]") not in seen])}
    except Exception as e:
        return {"status": "failed", "error": str(e), "linked": 0}


def create_for_quests(quests: list[dict], *, dry_run: bool = False, no_save: bool = False) -> list[WEResult]:
    """Batch-create WEs for WAFT quests. Return list of WEResult."""
    start_num = int(next_we_number().split(".")[1])
    results = []

    # Extract tasks defensively
    tasks = []
    for q in quests:
        if isinstance(q, dict) and "task" in q:
            tasks.append(q["task"])

    for i, task in enumerate(tasks):
        number = f"10.{start_num + i:02d}"
        result = create(task, number=number, auto_source="waft_quest", dry_run=dry_run, no_save=no_save)
        results.append(result)

    # Link successful ones
    links = [r["wikilink"] for r in results if r["status"] == "created"]
    if links and not dry_run and not no_save:
        link_into_daily(links)

    return results


def create_for_top3(items: list[str], *, dry_run: bool = False, no_save: bool = False, date: Optional[str] = None) -> list[WEResult]:
    """Batch-create WEs for tomorrow's top-3 items. Return list of WEResult."""
    start_num = int(next_we_number().split(".")[1])
    results = []

    for i, task in enumerate(items[:3]):
        if task.startswith("- [ ]"):
            task = task[5:].strip()
        if not task or task.startswith("("):  # skip placeholders
            continue
        number = f"10.{start_num + i:02d}"
        result = create(task, number=number, auto_source="tomorrow_top3", dry_run=dry_run, no_save=no_save, date=date)
        results.append(result)

    # Link if tomorrow note exists
    if date and not dry_run and not no_save:
        tomorrow_note = Path.home() / "Documents/Personal-Remote-Vault/Daily Notes" / f"{date}.md"
        if tomorrow_note.exists():
            links = [r["wikilink"] for r in results if r["status"] == "created"]
            if links:
                link_into_daily(links, date=date)

    return results


if __name__ == "__main__":
    result = create("Test WE for verification", dry_run=True)
    print(f"Status: {result['status']}")
    print(f"Wikilink: {result['wikilink']}")
    print(f"Source: {result['pipeline_source']}")
    print(f"Worthy: {result['worthy']}")
