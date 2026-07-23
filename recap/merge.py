"""CRUD merge strategies for daily-note sections.

Three strategies, each a pure function (existing_body, new_data) -> merged_body:

    upsert_commits   — union by SHA, preserve hand-authored prose
    fill_gaps_top3   — preserve user items, pad with <!-- auto --> markers
    stamped_recap    — newest block on top, fold prior to collapsed callouts

Parse helpers:

    extract_shas       — regex-find 7-char SHAs in bullet/table rows
    extract_checkbox   — list of (text, is_auto_marked) tuples
    fold_summary       — rewrite `> [!summary] ` to `> [!summary]- ` (collapsed)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


# ── Parse helpers ────────────────────────────────────────────────────────────

SHA_IN_ROW_RE = re.compile(r"(?:^|\s)`([a-f0-9]{7,40})`")
CHECKBOX_LINE_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s*(.+?)\s*$")
AUTO_MARKER = "<!-- auto -->"
SUMMARY_OPEN_RE = re.compile(r"^(>\s*\[!summary\])(?!-)", re.MULTILINE)


def extract_shas(body: str) -> set[str]:
    """Collect 7+ char hex SHAs appearing in backticks on bullet or table rows."""
    shas = set()
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("- ", "* ", "|")):
            for match in SHA_IN_ROW_RE.finditer(line):
                shas.add(match.group(1)[:7])
    return shas


def extract_checkbox(body: str) -> list[tuple[str, bool]]:
    """Return list of (text, is_auto) for each checkbox line in body."""
    out = []
    for line in body.splitlines():
        m = CHECKBOX_LINE_RE.match(line)
        if not m:
            continue
        text = m.group(1)
        is_auto = AUTO_MARKER in text
        clean = text.replace(AUTO_MARKER, "").strip()
        out.append((clean, is_auto))
    return out


def fold_summary(body: str) -> str:
    """Mark any non-collapsed `> [!summary]` callout as collapsed (`-` suffix)."""
    return SUMMARY_OPEN_RE.sub(r"\1-", body)


# ── Strategy 1: UPSERT-BY-SHA (commits_today) ────────────────────────────────

@dataclass
class CommitEntry:
    sha: str
    subject: str
    repo: str
    files: int = 0


def upsert_commits(
    existing: str,
    commits: Iterable[CommitEntry],
    *,
    force: bool = False,
) -> tuple[str, str]:
    """Merge git commits into existing `commits_today` body.

    Returns (merged_body, status) where status is one of:
        "skip_complete" — every commit SHA already in existing, no-op
        "upsert"        — new SHAs merged into body
        "fresh"         — existing was empty/template; wrote from scratch

    Preservation contract:
        - Lines between recognized commit rows that don't match the commit
          shape are passed through verbatim ("hand-authored prose")
        - Existing commit rows keep their exact text (subjects may have been
          edited); only missing SHAs get appended under their repo
    """
    known = extract_shas(existing or "")
    new_shas = {c.sha[:7] for c in commits}

    if not new_shas:
        return existing, "skip_complete"

    missing = new_shas - known
    if not missing and not force:
        return existing, "skip_complete"

    # Group new commits by repo, only those missing
    by_repo: dict[str, list[CommitEntry]] = {}
    for c in commits:
        if c.sha[:7] in missing or force:
            by_repo.setdefault(c.repo, []).append(c)

    # Build appended section — keep existing verbatim, add new repo-sections
    # after it. Order by repo name for determinism.
    suffix_parts = []
    for repo in sorted(by_repo):
        entries = by_repo[repo]
        suffix_parts.append(f"### {repo}")
        for c in entries:
            sha_short = c.sha[:7]
            if sha_short in known and not force:
                continue
            files_note = f" · {c.files} file(s)" if c.files else ""
            suffix_parts.append(f"- `{sha_short}` {c.subject}{files_note}")
        suffix_parts.append("")

    suffix = "\n".join(suffix_parts).rstrip() + "\n"

    if not existing.strip() or _is_template_only(existing):
        # Fresh write — render authoritative table from all commits
        header = f"**{len(commits) if isinstance(commits, list) else len(list(by_repo.values()))} commit(s)**\n\n"
        total = sum(len(v) for v in by_repo.values())
        header = f"**{total} commit(s)**\n\n"
        return header + suffix, "fresh"

    base = existing.rstrip() + "\n\n"
    return base + suffix, "upsert"


def _is_template_only(body: str) -> bool:
    """Heuristic: body has no real content (only whitespace, italics-instruction, or linked-doc placeholder)."""
    stripped = body.strip()
    if not stripped:
        return True
    # Common template artefacts
    lines = [l for l in stripped.splitlines() if l.strip()]
    if not lines:
        return True
    non_instruction = [
        l for l in lines
        if not (l.startswith(">") or l.startswith("*") and l.endswith("*"))
    ]
    return len(non_instruction) == 0


# ── Strategy 2: FILL-GAPS (tomorrows_top_3) ──────────────────────────────────

def fill_gaps_top3(
    existing: str,
    suggestions: list[str],
    *,
    target: int = 3,
    force: bool = False,
) -> tuple[str, str]:
    """Keep hand-authored checkbox items; pad up to `target` with auto-marked suggestions.

    Returns (merged_body, status):
        "skip_complete" — user items already >= target
        "fill_gaps"     — padded with N auto suggestions
        "refresh_auto"  — replaced prior auto-marked entries with new ones
        "fresh"         — existing empty; wrote all suggestions as auto
    """
    items = extract_checkbox(existing or "")
    user_items = [t for t, auto in items if not auto]
    prior_auto_count = sum(1 for _, auto in items if auto)

    if len(user_items) >= target and not force:
        return existing, "skip_complete"

    need = max(0, target - len(user_items))
    auto_items = suggestions[:need]

    lines = [f"- [ ] {t}" for t in user_items]
    lines += [f"- [ ] {t} {AUTO_MARKER}" for t in auto_items]

    body = "\n".join(lines)

    if not user_items and not prior_auto_count:
        return body, "fresh"
    if prior_auto_count and user_items:
        return body, "refresh_auto"
    return body, "fill_gaps"


# ── Strategy 3: STAMPED-APPEND (session_recap) ───────────────────────────────

def stamped_recap(
    existing: str,
    new_block_body: str,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Prepend a timestamped `> [!summary]-` block; fold any prior open summaries.

    `new_block_body` is the INNER content (without the callout header).
    Returns (merged_body, status):
        "fresh"  — first recap today
        "append" — prior recap(s) folded, new block on top
    """
    now = now or datetime.now()
    ts = now.strftime("%H:%M")
    weekday_date = now.strftime("%A, %B %-d")

    new_lines = [f"> [!summary]- EOD Recap — {weekday_date} {ts}"]
    for line in new_block_body.strip().splitlines():
        if line.startswith(">"):
            new_lines.append(line)
        else:
            new_lines.append(f"> {line}" if line.strip() else ">")
    new_block = "\n".join(new_lines) + "\n"

    if not existing.strip() or _is_template_only(existing):
        return new_block, "fresh"

    folded = fold_summary(existing.rstrip()) + "\n"
    return new_block + "\n" + folded, "append"
