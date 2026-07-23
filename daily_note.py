"""
daily_note.py — Harness for reading and writing Obsidian daily notes.

Treats the daily note as a structured document with named sections.
Any AI system (Claude Code, Gemma, cron jobs) can read/write specific
slots without knowing the full markdown structure.

Sections are identified by their markdown ## headers. Content between
one header and the next belongs to that section.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import atomic_io
import yaml_io

# Optional observability — logs to trail_log if harness_log is available.
# daily_note.py remains zero-dependency; this import is best-effort.
try:
    from harness_log import log_op as _log_op
except ImportError:
    _log_op = None


# ── Paths ──────────────────────────────────────────────────────────────

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
DAILY_NOTES_DIR = VAULT_DIR / "Daily Notes"
TEMPLATE_PATH = (VAULT_DIR / "System" / "00-09_system_meta" / "02_templates"
                 / "Daily_Note_Template.md")


# ── Section registry ───────────────────────────────────────────────────
# Maps slot names to the exact markdown header that starts each section.
# Order matters: sections appear in this order in the template.

SECTIONS = {
    "frontmatter":          None,               # special: YAML block at top
    "daily_reading":        "## Daily Reading",
    "location":             "## Location",
    "sitrep":               "## Sitrep",
    "research_feed":        "## Research Feed",
    "in_the_lab":           "## In the Lab",
    "work_efforts":         "## Work Efforts",
    "idea_dump":            "## Idea Dump",
    "claude_session_log":   "## Claude Code Session Log",
    "commits_today":        "## Commits Today",
    "tomorrows_top_3":      "## Tomorrow's Top 3",
    "waft_workspace":       "## WAFT Workspace",
    # Legacy sections — kept for backward compat with older notes
    "whats_happening":      "## What's Happening",
    "focus_snapshot":       "### Current Focus Snapshot",
    "ambiance":             "## Today's Ambiance",
    "session_log":          "## Session Log",
    "mcp_tools":            "## MCP Tools Used Today",
    "what_resonated":       "## What Resonated",
    "session_recap":        "## Session Recap (Timestamped)",
    "scraped_articles":     "## Scraped Articles",
}

# Sections that AI systems should write to (vs. user-only sections)
AI_WRITABLE = {
    "daily_reading", "location", "sitrep", "research_feed", "in_the_lab", "work_efforts",
    "claude_session_log", "commits_today", "tomorrows_top_3", "waft_workspace",
    # Legacy (still writable for older notes)
    "whats_happening", "focus_snapshot", "session_log", "session_recap",
}

# Actors allowed to use AI_WRITABLE permissions
AI_ACTORS = {"claude", "gemma", "waft-daemon", "cron"}

# Sections Gemma can write to (subset — local model gets fewer permissions)
GEMMA_WRITABLE = {
    "in_the_lab", "session_log", "session_recap",
}


# ── Core functions ─────────────────────────────────────────────────────

def daily_path(date: Optional[str] = None) -> Path:
    """Return path to a daily note. Defaults to today."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    return DAILY_NOTES_DIR / f"{date}.md"


def exists(date: Optional[str] = None) -> bool:
    """Check if a daily note exists for the given date."""
    return daily_path(date).is_file()


def create_from_template(date: Optional[str] = None,
                         template_path: Optional[Path] = None) -> Path:
    """
    Create a daily note from the vault's Daily Note Template, rendering
    basic Templater syntax headlessly (no Obsidian GUI required).

    Supports `<% tp.date.now("FMT") %>` and `<% tp.date.now("FMT", offset) %>`
    where offset is a day count (e.g. -1, 1). Format tokens handled:
    YYYY, MM, DD, dddd, MMMM, Do — the set used by the vault template.

    Returns the path to the note. No-op if the note already exists.
    Raises FileNotFoundError if the template is missing.
    """
    from datetime import timedelta

    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    path = daily_path(date)
    if path.is_file():
        return path

    tmpl = template_path or TEMPLATE_PATH
    if not tmpl.is_file():
        raise FileNotFoundError(f"Daily note template not found: {tmpl}")

    base = datetime.strptime(date, "%Y-%m-%d")

    def _ordinal(n: int) -> str:
        if 11 <= n % 100 <= 13:
            return f"{n}th"
        return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"

    def _render_format(fmt: str, dt: datetime) -> str:
        # Longest-token-first so MMMM wins over MM, dddd over DD, etc.
        tokens = [
            ("dddd", dt.strftime("%A")),
            ("MMMM", dt.strftime("%B")),
            ("YYYY", f"{dt.year:04d}"),
            ("Do",   _ordinal(dt.day)),
            ("MM",   f"{dt.month:02d}"),
            ("DD",   f"{dt.day:02d}"),
        ]
        out = fmt
        for token, value in tokens:
            out = out.replace(token, value)
        return out

    def _replace(match: re.Match) -> str:
        fmt = match.group(1)
        offset = int(match.group(2)) if match.group(2) else 0
        return _render_format(fmt, base + timedelta(days=offset))

    pattern = re.compile(
        r'<%\s*tp\.date\.now\(\s*"([^"]+)"\s*(?:,\s*(-?\d+)\s*)?\)\s*%>'
    )
    text = pattern.sub(_replace, tmpl.read_text(encoding="utf-8"))

    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_io.vault_lock():
        if path.is_file():  # re-check under lock
            return path
        atomic_io.atomic_write(path, text)

    if _log_op:
        try:
            _log_op(op="create_from_template", section="(whole note)",
                    actor="harness", date=date, path=str(path))
        except Exception:
            pass
    return path


def read_full(date: Optional[str] = None) -> str:
    """Read the entire daily note as a string."""
    path = daily_path(date)
    if not path.is_file():
        raise FileNotFoundError(f"No daily note at {path}")
    return path.read_text(encoding="utf-8")


def read_section(section: str, date: Optional[str] = None) -> str:
    """
    Read a single section from the daily note.
    Returns the content between this section's header and the next header.
    """
    if section not in SECTIONS:
        raise ValueError(f"Unknown section: {section}. Valid: {list(SECTIONS.keys())}")

    text = read_full(date)

    if section == "frontmatter":
        return _extract_frontmatter(text)

    header = SECTIONS[section]
    return _extract_section(text, header)


def read_all_sections(date: Optional[str] = None) -> dict:
    """Read all sections into a dict. Empty sections return empty strings."""
    text = read_full(date)
    result = {}
    for name, header in SECTIONS.items():
        if name == "frontmatter":
            result[name] = _extract_frontmatter(text)
        elif header:
            result[name] = _extract_section(text, header)
    return result


def section_status(date: Optional[str] = None) -> dict:
    """
    Return a dict of section_name -> status for each section.
    Status is 'filled', 'empty', 'template' (placeholder only), or 'absent'
    (header not in note — schema richer than template).
    """
    text = read_full(date)
    sections = read_all_sections(date)
    result = {}
    for name, content in sections.items():
        header = SECTIONS.get(name)
        if header and not re.search(rf'^{re.escape(header)}\s*$', text, re.MULTILINE):
            result[name] = "absent"
            continue
        stripped = _strip_boilerplate(content, name)
        if not stripped.strip():
            result[name] = "empty"
        elif _is_template_only(stripped):
            result[name] = "template"
        else:
            result[name] = "filled"
    return result


# ── Cross-day retrieval ───────────────────────────────────────────────

def most_recent_note(max_back: Optional[int] = None) -> Optional[str]:
    """
    Return the date (YYYY-MM-DD) of the most recent daily note strictly
    before today. Scans the Daily Notes directory directly rather than
    probing one day at a time, so continuity survives ANY gap — a long
    weekend, a month off, a year away. There is no lookback horizon by
    default: whatever the last note is, it's found.

    Pass max_back (days) only if you actually want a bounded search —
    e.g. "was there a note in the last week specifically." Left unset,
    a year-old note is found exactly as reliably as a two-day-old one.
    """
    if not DAILY_NOTES_DIR.is_dir():
        return None
    today_str = datetime.now().strftime("%Y-%m-%d")
    date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')
    candidates = [
        p.stem for p in DAILY_NOTES_DIR.glob("*.md")
        if date_pattern.match(p.stem) and p.stem < today_str
    ]
    if not candidates:
        return None
    best = max(candidates)  # ISO date strings sort chronologically
    if max_back is not None:
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=max_back)).strftime("%Y-%m-%d")
        if best < cutoff:
            return None
    return best


def read_yesterday(section: str) -> str:
    """Read a section from the most recent prior daily note (gap-tolerant)."""
    prior = most_recent_note()
    if prior is None:
        return ""
    return read_section(section, date=prior)


def last_handoff() -> dict:
    """
    Return the most recent handoff data for cross-session continuity.
    Finds the last note regardless of how long ago it was — a 4-day gap,
    a month away, a year off all resolve the same way: pick up from
    whatever the last note actually says, not a synthetic cold start.
    Reads the three sections that carry context between sessions:
    - tomorrows_top_3: what was planned for the next day
    - claude_session_log: what the last session did
    - in_the_lab: architectural decisions made
    """
    from datetime import timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    prior = most_recent_note()
    if prior is None:
        return {"date": yesterday, "found": False}
    return {
        "date": prior,
        "found": True,
        "gap_days": (datetime.strptime(
            datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d")
            - datetime.strptime(prior, "%Y-%m-%d")).days,
        "tomorrows_top_3": read_section("tomorrows_top_3", prior),
        "claude_session_log": read_section("claude_session_log", prior),
        "in_the_lab": read_section("in_the_lab", prior),
    }


def write_section(section: str, content: str, actor: str = "claude",
                  mode: str = "replace", date: Optional[str] = None) -> dict:
    """
    Write to a specific section of the daily note.

    Args:
        section: Section name from SECTIONS registry
        content: Markdown content to write
        actor: Who's writing — "claude", "gemma", "user", "cron"
        mode: "replace" overwrites section body, "append" adds to end
        date: Date string (YYYY-MM-DD), defaults to today

    Returns:
        dict with status, section, actor, timestamp
    """
    if section not in SECTIONS:
        raise ValueError(f"Unknown section: {section}")

    # Permission check
    if actor == "gemma" and section not in GEMMA_WRITABLE:
        raise PermissionError(f"Gemma cannot write to '{section}'. Allowed: {GEMMA_WRITABLE}")
    if actor in AI_ACTORS and section not in AI_WRITABLE:
        raise PermissionError(f"AI actors cannot write to '{section}'. Allowed: {AI_WRITABLE}")

    if section == "frontmatter":
        return _write_frontmatter(content, date)

    path = daily_path(date)
    text = read_full(date)
    header = SECTIONS[section]
    ts = datetime.now().strftime("%H:%M")

    # Self-heal: if the header is absent (legacy note, trimmed template),
    # append the section at the end instead of silently dropping the write.
    header_present = re.search(rf'^{re.escape(header)}\s*$', text, re.MULTILINE)
    if not header_present:
        body = f"\n**[{actor} @ {ts}]**\n{content}\n" if mode == "append" else content
        if not body.endswith("\n"):
            body += "\n"
        new_text = text.rstrip("\n") + f"\n\n---\n\n{header}\n\n{body}"
        if _log_op:
            _log_op("write_section", actor, section, "section_created")
    elif mode == "append":
        # Add content after existing section body, before next header
        old_body = _extract_section(text, header)
        # Add attribution line
        attributed = f"\n**[{actor} @ {ts}]**\n{content}\n"
        new_body = old_body.rstrip("\n") + attributed
        new_text = _replace_section(text, header, new_body)
    else:
        # Replace entire section body
        new_text = _replace_section(text, header, content)

    if new_text == text and content.strip() \
            and _extract_section(text, header).strip() != content.strip():
        raise RuntimeError(
            f"write_section produced no change for '{section}' — refusing to "
            f"report success on a silent no-op"
        )

    try:
        with atomic_io.vault_lock():
            atomic_io.atomic_write(path, new_text)
    except OSError as e:
        if _log_op:
            _log_op("write_section", actor, section, "fs_error", error=str(e))
        raise

    if _log_op:
        _log_op("write_section", actor, section, "ok", content=content)

    return {
        "status": "written",
        "section": section,
        "actor": actor,
        "mode": mode,
        "timestamp": datetime.now().isoformat(),
    }


def append_session_log(focus: str, changes: list = None, next_steps: str = "",
                       actor: str = "claude", date: Optional[str] = None,
                       files: list = None, context: str = "") -> dict:
    """
    Append a compact journal entry to the session log.

    Uses nested Obsidian callouts for foldability:
      > [!note]- 18:27 — focus text
      > files · context
      >> [!info]- Changes
      >> - item

    Args:
        focus: One-line summary (the headline)
        changes: List of changed items (shown in nested callout)
        next_steps: Brief next action (omitted if empty)
        files: List of file paths to link (renders as [[file]])
        context: Extra context string (nested callout if provided)
    """
    ts = datetime.now().strftime("%H:%M")
    changes = changes or []
    files = files or []

    # File links as compact Obsidian wikilinks
    file_links = " · ".join(f"[[{f}]]" for f in files) if files else ""

    # Build compact callout
    lines = [f"> [!note]- {ts} — {focus}"]
    if file_links:
        lines.append(f"> {file_links}")

    if changes:
        lines.append(f">> [!info]- {len(changes)} change(s)")
        for c in changes:
            lines.append(f">> - {c}")

    if context:
        lines.append(f">> [!tip]- Context")
        for ctx_line in context.strip().splitlines():
            lines.append(f">> {ctx_line}")

    if next_steps:
        lines.append(f"> **→** {next_steps}")

    entry = "\n".join(lines) + "\n"

    target = "claude_session_log" if actor == "claude" else "session_log"
    return write_section(target, entry, actor=actor, mode="append", date=date)


def update_frontmatter_fields(fields: dict, date: Optional[str] = None) -> dict:
    """
    Update specific YAML frontmatter fields without touching others.
    Example: update_frontmatter_fields({"project": "SimpleAgentOS", "energy": "high"})

    Uses ruamel.yaml RoundTrip mode via yaml_io — preserves comments,
    anchors, quote styles, and key order. All quoting/escaping is handled
    by ruamel; we just hand it the value.
    """
    path = daily_path(date)
    text = read_full(date)

    fm, body = yaml_io.parse(text)
    if fm is None:
        raise ValueError("No frontmatter found in daily note")

    new_text = yaml_io.update_fields(text, fields)

    with atomic_io.vault_lock():
        atomic_io.atomic_write(path, new_text)

    if _log_op:
        _log_op("update_frontmatter", "system", "frontmatter", "ok",
                content=",".join(fields.keys()))

    return {"status": "updated", "fields": list(fields.keys())}


# ── Internal helpers ───────────────────────────────────────────────────

def _extract_frontmatter(text: str) -> str:
    """Extract YAML frontmatter block."""
    match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
    return match.group(1) if match else ""


def _extract_section(text: str, header: str) -> str:
    """Extract content between a header and the next same-or-higher-level header."""
    try:
        level = len(header) - len(header.lstrip("#"))
        escaped = re.escape(header)
        pattern = rf'^{escaped}\s*\n(.*?)(?=^#{{{1},{level}}} |\Z)'
        match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
        if not match:
            return ""
        return match.group(1)
    except re.error:
        if _log_op:
            _log_op("extract_section", "system", header, "regex_error")
        return ""


def _replace_section(text: str, header: str, new_body: str) -> str:
    """Replace the body of a section (between its header and the next header)."""
    try:
        level = len(header) - len(header.lstrip("#"))
        escaped = re.escape(header)
        pattern = rf'(^{escaped}\s*\n)(.*?)(?=^#{{{1},{level}}} |\Z)'
        if not new_body.endswith("\n"):
            new_body += "\n"
        # Function replacement so new_body is inserted literally — a raw
        # replacement string chokes on backslashes (e.g. LaTeX in arXiv titles).
        result = re.sub(pattern, lambda m: m.group(1) + new_body, text, count=1,
                        flags=re.MULTILINE | re.DOTALL)
        return result
    except re.error:
        # Fallback: return text unchanged rather than corrupting the note
        if _log_op:
            _log_op("replace_section", "system", header, "regex_error")
        return text


def _strip_boilerplate(content: str, section_name: str) -> str:
    """Remove known template boilerplate to check if section has real content.

    Only strips instruction-style boilerplate, not content-bearing lines.
    A filled checkbox-list or a user-authored callout counts as real content.
    """
    stripped = content
    # Italic instruction blockquotes (> *Claude fills…*) — not ![!note] callouts
    stripped = re.sub(r'^>\s*\*[^*]+\*\s*$', '', stripped, flags=re.MULTILINE)
    # Empty checkbox lines (no text after `[ ]`)
    stripped = re.sub(r'^- \[ \]\s*$', '', stripped, flags=re.MULTILINE)
    # **Label:** with no value
    stripped = re.sub(r'^\*\*\w[\w\s]*:\*\*\s*$', '', stripped, flags=re.MULTILINE)
    # Horizontal rules
    stripped = re.sub(r'^---\s*$', '', stripped, flags=re.MULTILINE)
    # Empty list items
    stripped = re.sub(r'^-\s*$', '', stripped, flags=re.MULTILINE)
    return stripped


def _is_template_only(content: str) -> bool:
    """Return True only if every non-empty line is a known template placeholder."""
    stripped = content.strip()
    if not stripped:
        return True
    template_markers = [
        "Linked Document:", "Quick capture space",
        "Music is embedded", "No entries yet",
        "Tip: If you also write",
        # Sitrep template ships "**Blockers:** None" — value'd label, but stock.
        # Counting it as content meant a fresh note's sitrep read "filled" and
        # spin-up skipped it every morning (the section stayed template-blank).
        "**Blockers:** None",
        # WAFT Workspace / Session Log / Session Recap template callouts
        "WAFT Being ·",
        "Auto-generated by spin_up.py",
        "Auto-generated by /wrap-up",
        "Claude Code writes compact journal entries",
        "EOD summary of decisions",
        "[!info] Auto-generated",
        "[!summary] Auto-generated",
    ]
    # A bare bold label with no value ( **Foo**  or  **Foo:**  ) is a stub.
    bare_label = re.compile(r"^\*\*[^*]+\*\*:?$")
    # An empty checkbox ( - [ ] ) with nothing after it is a stub.
    empty_checkbox = re.compile(r"^-\s*\[\s*\]\s*$")
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "---":
            continue  # section divider = not real content
        if bare_label.match(line):
            continue  # empty field label = stub
        if empty_checkbox.match(line):
            continue  # unfilled checkbox = stub
        if any(m in line for m in template_markers):
            continue
        return False  # real content found → not template-only
    return True


def _write_frontmatter(content: str, date: Optional[str] = None) -> dict:
    """Replace frontmatter entirely. Use update_frontmatter_fields instead."""
    path = daily_path(date)
    text = read_full(date)
    _, body = yaml_io.parse(text)
    new_text = f"---\n{content.rstrip(chr(10))}\n---\n{body}"
    with atomic_io.vault_lock():
        atomic_io.atomic_write(path, new_text)
    return {"status": "written", "section": "frontmatter"}


# ── CLI for quick testing ──────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python daily_note.py status [date]   — show section fill status")
        print("  python daily_note.py read <section>   — read a section")
        print("  python daily_note.py sections         — list all sections")
        print("  python daily_note.py handoff          — yesterday's handoff data (JSON)")
        print("  python daily_note.py create [date]    — create note from template (headless)")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        # Optional explicit date; otherwise today, falling back to the most
        # recent note so pre-dawn preflight (before the note exists) can still
        # exercise the section parser instead of reporting zero sections.
        status_date = sys.argv[2] if len(sys.argv) > 2 else None
        if status_date is None and not exists():
            status_date = most_recent_note()
            if status_date is None:
                print("no daily notes found — run: python3 daily_note.py create")
                sys.exit(0)
            print(f"(no note for today — showing most recent: {status_date})")
        if not exists(status_date):
            print(f"no daily note for {status_date}")
            sys.exit(1)
        status = section_status(status_date)
        for name, state in status.items():
            icon = {"filled": "●", "template": "◐", "empty": "○", "absent": "·"}[state]
            print(f"  {icon} {name:25s} {state}")
        sys.exit(0)

    if cmd == "read" and not exists():
        print(f"no daily note for today — run: python3 daily_note.py create")
        sys.exit(0)

    if cmd == "read" and len(sys.argv) > 2:
        print(read_section(sys.argv[2]))
    elif cmd == "sections":
        for name, header in SECTIONS.items():
            writable = "✎" if name in AI_WRITABLE else " "
            gemma = "G" if name in GEMMA_WRITABLE else " "
            print(f"  {writable}{gemma} {name:25s} {header or '(YAML)'}")
    elif cmd == "create":
        target = sys.argv[2] if len(sys.argv) > 2 else None
        already = exists(target)
        path = create_from_template(target)
        print(f"{'exists' if already else 'created'}: {path}")
    elif cmd == "handoff":
        data = last_handoff()
        print(json.dumps(data, indent=2))
    else:
        print(f"Unknown command: {cmd}")
