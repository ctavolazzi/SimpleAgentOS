"""
live_feed.py — Real-time "what the agent is doing right now" feed for the
daily note.

The Live Feed section sits directly under the hero image, above Daily Reading,
so the top of the note is a watchable window: put it on screen, work, and the
actions stream in.

Two halves, deliberately split for speed:

  RECORD  A shell hook (~/.claude/hooks/live-feed.sh) appends one raw JSON
          line per tool call to today's feed file. Pure bash + jq, no Python
          startup, so it costs milliseconds and never delays a tool call.

  RENDER  This module reads that file, formats it, and rebuilds the whole
          Live Feed section via daily_note.write_section(mode="replace").
          Full rebuild rather than append: double fires, races, and hand
          edits all converge on the same correct content, and there is no
          callout-nesting hazard (the bug that append mode used to hit).

Feed file: System/40-49_telemetry/live_feed/YYYY-MM/YYYY-MM-DD.jsonl
Raw line schema (written by the hook, read here):
    {"ts": "09:52:03", "kind": "Edit", "detail": "<raw tool input>"}
The hook captures raw values; all formatting decisions live here.

CLI:
    python3 live_feed.py --render            rebuild the section from the feed
    python3 live_feed.py --note "text"       append a manual entry, then render
    python3 live_feed.py --focus "text"      set the headline, then render
    python3 live_feed.py --record            read a hook payload on stdin
    python3 live_feed.py --show              print the rendered markdown only
Add --dry-run to any of the above to skip the note write.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import daily_note

# ── Paths ──────────────────────────────────────────────────────────────

FEED_ROOT = (daily_note.VAULT_DIR / "System" / "40-49_telemetry" / "live_feed")

# ── Render tuning ──────────────────────────────────────────────────────

MAX_ROWS = 14          # rows shown in the note; older entries collapse to a count
DETAIL_CHARS = 68      # per-row detail truncation

# Tools whose runs are pure reconnaissance. They still appear, but consecutive
# runs collapse into a single counted row so a long research stretch reads as
# one line instead of flooding the window.
QUIET_KINDS = {"Read", "Grep", "Glob", "NotebookRead", "TodoWrite"}

ICONS = {
    "Bash": "▶",
    "Edit": "✎",
    "MultiEdit": "✎",
    "Write": "✚",
    "NotebookEdit": "✎",
    "Read": "·",
    "Grep": "·",
    "Glob": "·",
    "TodoWrite": "☑",
    "Task": "⚙",
    "Agent": "⚙",
    "WebFetch": "⇣",
    "WebSearch": "⌕",
    "note": "◆",
}


# ── Feed file ──────────────────────────────────────────────────────────

def feed_path(date: Optional[str] = None) -> Path:
    """Path to the JSONL feed for a given date. Defaults to today."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    return FEED_ROOT / date[:7] / f"{date}.jsonl"


def state_path(date: Optional[str] = None) -> Path:
    """Path to the small sidecar holding the current focus headline."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    return FEED_ROOT / date[:7] / f"{date}.state.json"


def append(kind: str, detail: str = "", date: Optional[str] = None,
           sid: str = "") -> None:
    """Append one event to the feed file. Never raises on a full disk or a
    missing directory — a dropped feed row must not break the caller."""
    try:
        path = feed_path(date)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "kind": kind,
            "sid": sid,
            "detail": detail,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(date: Optional[str] = None) -> list:
    """Read the feed file. Malformed lines are skipped, not fatal — the feed
    is observability, so a truncated write loses one row and nothing else."""
    path = feed_path(date)
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("kind"):
            events.append(row)
    return events


def set_focus(text: str, date: Optional[str] = None) -> None:
    """Set the headline shown at the top of the feed."""
    try:
        path = state_path(date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "focus": _oneline(text),
            "set_at": datetime.now().strftime("%H:%M"),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def get_focus(date: Optional[str] = None) -> dict:
    path = state_path(date)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ── Formatting ─────────────────────────────────────────────────────────

def _oneline(value) -> str:
    """Collapse to a single line. A stray newline inside a callout row breaks
    the reader out of the block, the same failure mode daily_note._oneline
    guards against."""
    return " ".join(str(value).split())


def _shorten_path(raw: str) -> str:
    """Show the last two path components — enough to identify the file without
    burning the row on a home-directory prefix."""
    parts = [p for p in Path(raw).parts if p not in ("/", "")]
    return "/".join(parts[-2:]) if len(parts) > 1 else (parts[-1] if parts else raw)


def _format_detail(kind: str, detail: str) -> str:
    """Turn a raw tool input into one scannable row fragment."""
    detail = _oneline(detail)
    if not detail:
        return ""

    if kind in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
        detail = _shorten_path(detail)
    elif kind == "Bash":
        # Drop a leading `cd <path> &&`, which is preamble rather than action.
        detail = re.sub(r'^cd\s+\S+\s*&&\s*', '', detail)

    if len(detail) > DETAIL_CHARS:
        detail = detail[:DETAIL_CHARS - 1].rstrip() + "…"

    # Rows are wrapped in backticks; an inner backtick would end the span early.
    return detail.replace("`", "'")


def _collapse(events: list) -> list:
    """Collapse consecutive runs of the same quiet tool into one counted row.

    Twelve consecutive Reads are one act of research, not twelve events worth
    of screen space. Loud tools (Bash, Edit, Write) never collapse — those are
    the ones worth watching individually. Runs from different sessions never
    merge either: two agents reading in parallel is not one act of research.
    """
    rows = []
    for ev in events:
        kind = ev.get("kind", "?")
        sid = ev.get("sid", "")
        prev = rows[-1] if rows else None
        if (prev and kind in QUIET_KINDS
                and prev["kind"] == kind and prev["sid"] == sid):
            prev["count"] += 1
            prev["ts"] = ev.get("ts", prev["ts"])
            prev["detail"] = ev.get("detail", "")
        else:
            rows.append({
                "ts": ev.get("ts", ""),
                "kind": kind,
                "sid": sid,
                "detail": ev.get("detail", ""),
                "count": 1,
            })
    return rows


def render_markdown(date: Optional[str] = None) -> str:
    """Build the full Live Feed section body."""
    events = read_events(date)
    focus = get_focus(date)
    now = datetime.now().strftime("%H:%M")

    if not events and not focus:
        return ("> [!abstract]+ Live Feed\n"
                "> Idle. Rebuilt by `live_feed.py` on every agent tool call.\n")

    rows = _collapse(events)
    shown = rows[::-1][:MAX_ROWS]          # newest first
    hidden = len(rows) - len(shown)

    plural = "action" if len(events) == 1 else "actions"
    all_sessions = {e.get("sid") for e in events if e.get("sid")}
    tabs = f" · {len(all_sessions)} tabs" if len(all_sessions) > 1 else ""
    title = (f"> [!abstract]+ Live Feed · {len(events)} {plural}{tabs} "
             f"· updated {now}")
    lines = [title]

    if focus.get("focus"):
        lines.append(f"> **Now:** {focus['focus']} *(since {focus.get('set_at', '')})*")
        lines.append(">")

    # Several Claude Code tabs share one feed. Tag rows by session only when
    # more than one is actually in the window, so the common single-session
    # view stays clean.
    sessions = {r["sid"] for r in shown if r.get("sid")}
    tag_sessions = len(sessions) > 1

    for row in shown:
        icon = ICONS.get(row["kind"], "•")
        detail = _format_detail(row["kind"], row["detail"])
        count = f" ×{row['count']}" if row["count"] > 1 else ""
        body = f" `{detail}`" if detail else ""
        tag = f" ⟨{row['sid']}⟩" if tag_sessions and row.get("sid") else ""
        lines.append(f"> `{row['ts']}`{tag} {icon} **{row['kind']}{count}**{body}")

    if hidden > 0:
        lines.append(">")
        older = "row" if hidden == 1 else "rows"
        lines.append(f"> *{hidden} earlier {older} trimmed. "
                     f"Full history: `System/40-49_telemetry/live_feed/`*")

    return "\n".join(lines) + "\n"


# ── Note write ─────────────────────────────────────────────────────────

def _ensure_section(date: Optional[str] = None) -> bool:
    """Make sure `## Live Feed` exists in the note, in the right place.

    write_section self-heals a missing header by appending at the END of the
    note, which is the one position that defeats the point of this section.
    So place it explicitly: directly after the hero image block, which is where
    the template puts it.

    Returns True if the section is present (or was just created).
    """
    header = daily_note.SECTIONS["live_feed"]
    path = daily_note.daily_path(date)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if re.search(rf'^{re.escape(header)}\s*$', text, re.MULTILINE):
        return True

    block = f"{header}\n\n> [!abstract]+ Live Feed\n> Starting up.\n\n---\n\n"

    if "<!-- /hero_image -->" in text:
        new_text = text.replace("<!-- /hero_image -->",
                                f"<!-- /hero_image -->\n\n{block}".rstrip() + "\n", 1)
    else:
        # No hero image yet: sit above whichever work section comes first.
        anchors = ["## Work Efforts", "## In the Lab", "## Commits Today",
                   "## Daily Reading", "## Location"]
        for anchor in anchors:
            if re.search(rf'^{re.escape(anchor)}\s*$', text, re.MULTILINE):
                new_text = re.sub(rf'^{re.escape(anchor)}$', f"{block}{anchor}",
                                  text, count=1, flags=re.MULTILINE)
                break
        else:
            return False

    import atomic_io
    with atomic_io.vault_lock():
        atomic_io.atomic_write(path, new_text)
    return True


def render(date: Optional[str] = None, dry_run: bool = False) -> dict:
    """Rebuild the Live Feed section from the feed file."""
    if not daily_note.exists(date):
        return {"status": "skipped", "reason": "no daily note"}

    md = render_markdown(date)
    if dry_run:
        return {"status": "dry-run", "markdown": md}

    if not _ensure_section(date):
        return {"status": "skipped", "reason": "could not place Live Feed section"}

    daily_note.write_section("live_feed", md, actor="claude",
                             mode="replace", date=date)
    return {"status": "written", "rows": len(read_events(date))}


# ── Hook payload ───────────────────────────────────────────────────────

def record_payload(payload: dict) -> Optional[dict]:
    """Extract one feed event from a Claude Code PostToolUse payload.

    Kept here as well as in the shell hook so the schema has a Python-side
    reference implementation and the hook stays testable.
    """
    kind = payload.get("tool_name") or "?"
    sid = str(payload.get("session_id") or "")[:4]
    ti = payload.get("tool_input") or {}
    detail = (ti.get("command")
              or ti.get("file_path")
              or ti.get("notebook_path")
              or ti.get("pattern")
              or ti.get("description")
              or ti.get("query")
              or ti.get("url")
              or ti.get("prompt")
              or "")
    append(kind, str(detail), sid=sid)
    return {"kind": kind, "detail": str(detail), "sid": sid}


# ── CLI ────────────────────────────────────────────────────────────────

def main(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Live Feed for the daily note")
    ap.add_argument("--render", action="store_true", help="rebuild the section")
    ap.add_argument("--record", action="store_true",
                    help="read a hook payload on stdin and append it")
    ap.add_argument("--note", help="append a manual entry, then render")
    ap.add_argument("--focus", help="set the headline, then render")
    ap.add_argument("--show", action="store_true",
                    help="print the rendered markdown without writing")
    ap.add_argument("--date", help="YYYY-MM-DD (defaults to today)")
    ap.add_argument("--dry-run", action="store_true", help="never write the note")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args(argv)

    if args.record:
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            return 0
        record_payload(payload if isinstance(payload, dict) else {})

    if args.focus:
        set_focus(args.focus, args.date)

    if args.note:
        append("note", args.note, args.date)

    if args.show:
        print(render_markdown(args.date), end="")
        return 0

    if args.render or args.record or args.note or args.focus:
        result = render(args.date, dry_run=args.dry_run)
        print(json.dumps(result) if args.json
              else f"live_feed: {result.get('status')}"
                   f"{' — ' + result['reason'] if result.get('reason') else ''}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
