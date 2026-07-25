"""
migrate_note_layout.py — Reorder an existing daily note to match the current
Daily Note Template.

The template is the spine. When section order changes there (2026-07-25: the
work block moved above Daily Reading so the top of the note is a watchable
window), notes already on disk keep the old order. This tool rewrites them in
place, moving whole sections without touching a byte of their content.

Guarantees:
  - Content preserving. Section bodies are moved, never edited. The tool
    verifies that the set of (header, body) pairs is identical before and
    after, and refuses to write if anything changed.
  - Idempotent. Running it twice is a no-op the second time.
  - Dry run by default. Pass --write to actually save.

Ordering rule:
  1. Sections the template lists appear in template order.
  2. Sections the note has that the template does not (ad-hoc sections like
     "## External Drives Survey") keep their relative order and land after the
     template body sections.
  3. Session Recap and Quick Links stay pinned at the bottom.

Usage:
    python3 migrate_note_layout.py                    # dry run, today
    python3 migrate_note_layout.py --write            # apply to today
    python3 migrate_note_layout.py --date 2026-07-24 --write
    python3 migrate_note_layout.py --all --write      # every note in the vault
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import atomic_io
import daily_note

# Headers that belong at the bottom regardless of what else moves.
TAIL_HEADERS = ["## Session Recap (Timestamped)", "## Quick Links"]

# Placeholder used when the note predates a section the template now has.
NEW_SECTION_BODIES = {
    "## Live Feed": ("> [!abstract]+ Live Feed\n"
                     "> Idle. Rebuilt by `live_feed.py` on every agent tool call.\n"),
}

SECTION_RE = re.compile(r'^##\s+\S')
FENCE_RE = re.compile(r'^\s*(```|~~~)')


# ── Parsing ────────────────────────────────────────────────────────────

def split_sections(text: str):
    """Split a note into (preamble, [(header, body), ...]).

    Fence aware: a `## ` line inside a code block is content, not a heading.
    """
    lines = text.splitlines(keepends=True)
    preamble = []
    blocks = []
    current_header = None
    current_body = []
    in_fence = False
    fence_marker = None
    in_frontmatter = text.startswith("---\n")
    frontmatter_closed = not in_frontmatter

    for i, line in enumerate(lines):
        # Frontmatter delimiters are not content and not fences.
        if not frontmatter_closed:
            preamble.append(line)
            if i > 0 and line.rstrip() == "---":
                frontmatter_closed = True
            continue

        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, None

        if not in_fence and SECTION_RE.match(line):
            if current_header is None:
                pass
            else:
                blocks.append((current_header, "".join(current_body)))
            current_header = line.rstrip("\n")
            current_body = []
            continue

        if current_header is None:
            preamble.append(line)
        else:
            current_body.append(line)

    if current_header is not None:
        blocks.append((current_header, "".join(current_body)))

    return "".join(preamble), blocks


def strip_leading_rule(text: str) -> str:
    """Drop blank lines and any standalone `---` rules from the front."""
    lines = text.splitlines()
    while lines:
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and lines[0].strip() == "---":
            lines.pop(0)
            continue
        break
    return "\n".join(lines)


def strip_trailing_rule(text: str) -> str:
    """Drop blank lines and any standalone `---` rules from the end.

    Used on the preamble too: whether the hero image block is followed by a
    separator varies by note, and reassembly adds exactly one. Without this,
    every pass appended another rule and the tool was never idempotent.

    Strips ALL trailing rules, not just one. A single-rule strip cannot heal a
    note that already has `---\\n\\n---` at a boundary: it removes one, the
    rejoin adds one back, and the doubled rule survives every pass looking
    like a stable state.
    """
    lines = text.splitlines()
    while lines:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == "---":
            lines.pop()
            continue
        break
    return "\n".join(lines)


def normalize_body(body: str) -> str:
    """Strip the `---` separators bracketing a section body.

    Separators are structural punctuation between sections, not section
    content, so they are removed here and reinserted uniformly on reassembly.
    Notes on disk are inconsistent about which side of a header the rule sits
    on (the template puts it above the next header; spin_up-written sections
    ended up with it below their own), so both ends are handled. A horizontal
    rule in the MIDDLE of a section body is content and is left alone.
    """
    return strip_trailing_rule(strip_leading_rule(body))


def template_headers(template_path: Optional[Path] = None) -> list:
    """Header order as the template declares it."""
    path = template_path or daily_note.TEMPLATE_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Template not found: {path}")
    _, blocks = split_sections(path.read_text(encoding="utf-8"))
    return [h for h, _ in blocks]


# ── Ordering ───────────────────────────────────────────────────────────

def target_order(note_headers: list, tmpl_headers: list) -> list:
    """Apply the ordering rule. Returns the desired header sequence."""
    body_template = [h for h in tmpl_headers if h not in TAIL_HEADERS]

    ordered = [h for h in body_template if h in note_headers]
    extras = [h for h in note_headers
              if h not in tmpl_headers and h not in TAIL_HEADERS]
    tail = [h for h in TAIL_HEADERS if h in note_headers]
    return ordered + extras + tail


def reassemble(preamble: str, blocks: list, order: list) -> str:
    """Rebuild the note text with sections in the given order."""
    by_header = {h: normalize_body(b) for h, b in blocks}
    parts = [strip_trailing_rule(preamble)]
    for header in order:
        body = by_header.get(header, "")
        parts.append(f"{header}\n\n{body}" if body else header)
    return "\n\n---\n\n".join(parts).rstrip("\n") + "\n"


# ── Migration ──────────────────────────────────────────────────────────

def migrate(date: Optional[str] = None, write: bool = False,
            add_missing: bool = True) -> dict:
    """Reorder one note. Returns a result dict; writes only if write=True."""
    path = daily_note.daily_path(date)
    if not path.is_file():
        return {"status": "skipped", "reason": "no note", "path": str(path)}

    text = path.read_text(encoding="utf-8")
    preamble, blocks = split_sections(text)
    if not blocks:
        return {"status": "skipped", "reason": "no sections", "path": str(path)}

    tmpl = template_headers()
    note_headers = [h for h, _ in blocks]

    # Add sections the template gained since this note was created.
    added = []
    if add_missing:
        for header, body in NEW_SECTION_BODIES.items():
            if header in tmpl and header not in note_headers:
                blocks.append((header, body))
                note_headers.append(header)
                added.append(header)

    order = target_order(note_headers, tmpl)
    new_text = reassemble(preamble, blocks, order)

    # Compare rendered output, not just header order: a note can already be in
    # the right order and still carry uneven separator spacing from an earlier
    # pass. Byte equality is the only honest "nothing to do" signal.
    if new_text == text:
        return {"status": "unchanged", "path": str(path), "sections": len(blocks)}

    # Content-preservation check: every body must survive the move intact.
    _, new_blocks = split_sections(new_text)
    before = {h: normalize_body(b) for h, b in blocks}
    after = {h: normalize_body(b) for h, b in new_blocks}
    if before != after:
        lost = sorted(set(before) ^ set(after))
        changed = sorted(h for h in set(before) & set(after)
                         if before[h] != after[h])
        return {
            "status": "refused",
            "reason": "content would change",
            "path": str(path),
            "missing_or_extra": lost,
            "modified": changed,
        }

    verb = "reorder" if order != note_headers or added else "tidy"
    past = {"reorder": "reordered", "tidy": "tidied"}[verb]
    result = {
        "status": f"would-{verb}" if not write else past,
        "path": str(path),
        "sections": len(blocks),
        "added": added,
        "before": note_headers,
        "after": order,
    }

    if write:
        with atomic_io.vault_lock():
            atomic_io.atomic_write(path, new_text)

    return result


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--date", help="YYYY-MM-DD (defaults to today)")
    ap.add_argument("--all", action="store_true",
                    help="every daily note in the vault")
    ap.add_argument("--write", action="store_true",
                    help="apply changes (default is a dry run)")
    ap.add_argument("--no-add-missing", action="store_true",
                    help="do not insert sections the template gained")
    args = ap.parse_args(argv)

    if args.all:
        pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')
        dates = sorted(p.stem for p in daily_note.DAILY_NOTES_DIR.glob("*.md")
                       if pattern.match(p.stem))
    else:
        dates = [args.date]

    exit_code = 0
    for date in dates:
        result = migrate(date, write=args.write,
                         add_missing=not args.no_add_missing)
        label = date or "today"
        status = result["status"]
        if status == "refused":
            exit_code = 1
            print(f"  ✗ {label}: REFUSED — {result['reason']}")
            if result.get("missing_or_extra"):
                print(f"      missing/extra: {result['missing_or_extra']}")
            if result.get("modified"):
                print(f"      modified: {result['modified']}")
        elif status in ("would-reorder", "reordered", "would-tidy", "tidied"):
            print(f"  ● {label}: {status} ({result['sections']} sections"
                  f"{', added ' + ', '.join(result['added']) if result['added'] else ''})")
            if not args.write:
                print(f"      before: {' → '.join(h[3:] for h in result['before'])}")
                print(f"      after:  {' → '.join(h[3:] for h in result['after'])}")
        else:
            print(f"  ○ {label}: {status}"
                  f"{' (' + result['reason'] + ')' if result.get('reason') else ''}")

    if not args.write and exit_code == 0:
        print("\nDry run. Re-run with --write to apply.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
