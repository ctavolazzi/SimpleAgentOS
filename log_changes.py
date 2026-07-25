#!/usr/bin/env python3
"""
log_changes.py — Ad-hoc changelog entry into today's daily note.

Thin CLI wrapper around daily_note.append_session_log() for mid-session
"log what just happened" moments — the gap between /spin-up (morning) and
/wrap-up (evening) that daily-note-os covers manually. This gives it a
one-shot command.

Usage:
  python3 log_changes.py --focus "headline" --change "did X" --change "did Y"
  python3 log_changes.py --focus "headline" --next "follow up on Z"
  echo '{"focus": "...", "changes": ["..."]}' | python3 log_changes.py -

Exit codes:
  0 = written
  1 = error (missing focus, note missing/unwritable section, bad JSON)
"""

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import daily_note


def _from_json(raw: str) -> dict:
    data = json.loads(raw)
    if "focus" not in data:
        raise ValueError("JSON payload missing required 'focus' field")
    return data


def _from_args(args: argparse.Namespace) -> dict:
    return {
        "focus": args.focus,
        "changes": args.change or [],
        "next_steps": args.next or "",
        "files": args.file or [],
        "context": args.context or "",
        "date": args.date,
        "actor": args.actor,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Append a changelog entry to today's daily note")
    parser.add_argument("stdin_json", nargs="?", help="pass '-' to read a JSON payload from stdin")
    parser.add_argument("--focus", help="one-line headline of what happened")
    parser.add_argument("--change", action="append", help="a changed item (repeatable)")
    parser.add_argument("--next", dest="next", help="next step / follow-up")
    parser.add_argument("--file", action="append", help="file or vault path to wikilink (repeatable)")
    parser.add_argument("--context", help="extra context block")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--actor", default="claude", help="actor tag, defaults to 'claude'")
    args = parser.parse_args()

    if args.stdin_json == "-":
        try:
            payload = _from_json(sys.stdin.read())
        except (json.JSONDecodeError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    else:
        if not args.focus:
            parser.error("--focus is required (or pipe JSON with '-')")
        payload = _from_args(args)

    try:
        result = daily_note.append_session_log(
            focus=payload["focus"],
            changes=payload.get("changes") or [],
            next_steps=payload.get("next_steps", ""),
            actor=payload.get("actor", "claude"),
            date=payload.get("date"),
            files=payload.get("files") or [],
            context=payload.get("context", ""),
        )
    except PermissionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"logged: {result['section']} ({result['timestamp']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
