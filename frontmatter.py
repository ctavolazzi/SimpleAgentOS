#!/usr/bin/env python3
"""
frontmatter.py — Daily note frontmatter manager.

List-aware YAML read/write for daily note frontmatter.
Appends/removes single items without clobbering other fields.

Usage:
  python3 frontmatter.py status              # show all fields + validation
  python3 frontmatter.py get <field>         # print field value
  python3 frontmatter.py set <field> <val>   # set scalar field
  python3 frontmatter.py add <field> <val>   # append to list field
  python3 frontmatter.py remove <field> <val># remove from list
  python3 frontmatter.py link <note-name>    # add [[note]] to related
  python3 frontmatter.py ref <path>          # add path to code_refs
  python3 frontmatter.py validate            # check schema, exit 1 on fail
  python3 frontmatter.py sync               # fill missing defaults
  python3 frontmatter.py [--date YYYY-MM-DD] # target specific date
"""

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import daily_note

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = {
    "type":                {"kind": "scalar", "required": True,  "default": "daily"},
    "date":                {"kind": "scalar", "required": True},
    "parent":              {"kind": "scalar", "required": True,  "default": "[[00.00_vault_index]]", "link": True},
    "created":             {"kind": "scalar", "required": False},
    "project":             {"kind": "scalar", "required": False},
    "focus":               {"kind": "scalar", "required": False},
    # NOTE: field is work_efforts_TOUCHED — matches the template + real notes.
    # (Was mis-named "work_efforts" in the schema, so `sync` would have injected
    #  a bogus duplicate field. Corrected 2026-07-10.)
    "work_efforts_touched": {"kind": "list",  "required": False, "default": []},
    "tags":                {"kind": "list",   "required": False, "default": ["daily"]},
    "location":            {"kind": "scalar", "required": False},
    "related":             {"kind": "list",   "required": False, "default": []},
    "code_refs":           {"kind": "list",   "required": False, "default": []},
    # Session continuity links — scaffolded by spin_up.py Phase 5 AND the
    # template. hub is REQUIRED: a daily note with no hub is unreachable as a
    # continuation brief (the 2026-07-10 blind spot). link:True fields are
    # checked for target existence by validate().
    "plan":                {"kind": "scalar", "required": False, "link": True},
    "plan_status":         {"kind": "scalar", "required": False},
    "journal":             {"kind": "scalar", "required": True,  "link": True},
    "hub":                 {"kind": "scalar", "required": True,  "link": True},
    "wagonwheel":          {"kind": "scalar", "required": False, "link": True},
}


# ── Parsing ───────────────────────────────────────────────────────────────────

def _extract_fm_text(text: str) -> Optional[str]:
    m = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
    return m.group(1) if m else None


def _parse(fm_text: str) -> dict:
    """Parse frontmatter YAML text → dict. PyYAML if available, regex fallback."""
    if _HAS_YAML:
        try:
            data = _yaml.safe_load(fm_text) or {}
            # Normalize None list fields to []
            return {
                k: ([] if v is None and SCHEMA.get(k, {}).get("kind") == "list" else v)
                for k, v in data.items()
            }
        except Exception:
            pass
    return _fallback_parse(fm_text)


def _fallback_parse(fm_text: str) -> dict:
    """Regex-based parser — handles our frontmatter format without PyYAML."""
    result = {}
    lines = fm_text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r'^(\w[\w_]*)\s*:\s*(.*)', lines[i])
        if not m:
            i += 1
            continue
        key, inline = m.group(1), m.group(2).strip()
        if inline == "[]":
            result[key] = []
            i += 1
        elif inline:
            result[key] = inline.strip('"\'')
            i += 1
        else:
            # Collect indented list items
            items, i = [], i + 1
            while i < len(lines) and re.match(r'^\s+-\s+', lines[i]):
                items.append(re.sub(r'^\s+-\s+', '', lines[i]).strip().strip('"\''))
                i += 1
            result[key] = items if items else None
    return result


# ── Serialization ─────────────────────────────────────────────────────────────

def _needs_quoting(s: str) -> bool:
    """True if YAML scalar must be quoted to parse correctly."""
    return bool(
        re.search(r'[\[\]:#\*&!|>{}\']', s)
        or s.lower() in ("true", "false", "null", "yes", "no")
        or s.strip() != s
    )


def _write_field(lines: list, key: str, val: Any):
    if isinstance(val, list):
        if not val:
            lines.append(f"{key}: []")
        else:
            lines.append(f"{key}:")
            for item in val:
                s = str(item)
                lines.append(f'  - "{s}"' if _needs_quoting(s) else f"  - {s}")
    elif val is None:
        lines.append(f"{key}:")
    else:
        s = str(val)
        lines.append(f'{key}: "{s}"' if _needs_quoting(s) else f"{key}: {s}")


def _serialize(data: dict) -> str:
    """Serialize frontmatter dict → YAML text (schema-ordered, extras appended)."""
    lines = []
    for key in SCHEMA:
        if key in data:
            _write_field(lines, key, data[key])
    for key, val in data.items():
        if key not in SCHEMA:
            _write_field(lines, key, val)
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def read_fm(date: Optional[str] = None) -> dict:
    """Read and parse frontmatter for the given date."""
    text = daily_note.read_full(date)
    fm_text = _extract_fm_text(text)
    return _parse(fm_text) if fm_text else {}


def write_fm(data: dict, date: Optional[str] = None):
    """Serialize dict and write frontmatter back."""
    daily_note._write_frontmatter(_serialize(data), date)


def get_field(field: str, date: Optional[str] = None) -> Any:
    return read_fm(date).get(field)


def set_field(field: str, value: Any, date: Optional[str] = None) -> Any:
    data = read_fm(date)
    data[field] = value
    write_fm(data, date)
    return value


def add_to_list(field: str, item: str, date: Optional[str] = None) -> list:
    """Append item to list field. No-op if already present."""
    data = read_fm(date)
    current = data.get(field, [])
    if not isinstance(current, list):
        current = [current] if current else []
    if item not in current:
        current.append(item)
        data[field] = current
        write_fm(data, date)
    return current


def remove_from_list(field: str, item: str, date: Optional[str] = None) -> list:
    data = read_fm(date)
    current = data.get(field, [])
    if isinstance(current, list) and item in current:
        current.remove(item)
        data[field] = current
        write_fm(data, date)
    return current if isinstance(current, list) else []


def _resolve_wikilink(link: str) -> bool:
    """True if a [[wikilink]] (optionally with |alias or path) resolves to a
    real .md file in the vault. Accepts both 'Hubs/2026-07-10_hub' (pathed)
    and bare basenames (Obsidian resolves those by filename anywhere)."""
    m = re.search(r'\[\[([^\]|#]+)', str(link))
    if not m:
        return True  # not a wikilink — nothing to resolve (skip)
    target = m.group(1).strip()
    vault = daily_note.VAULT_DIR
    # Pathed target: try direct, and .md-suffixed
    direct = vault / target
    if direct.is_file() or (vault / f"{target}.md").is_file():
        return True
    # Bare basename: search the whole vault (Obsidian's resolution)
    base = Path(target).name
    for p in vault.rglob(f"{base}.md"):
        if ".obsidian" not in p.parts and ".trash" not in p.parts:
            return True
    return False


def validate(date: Optional[str] = None, check_links: bool = True) -> list[str]:
    """Return list of validation issues. Empty = valid.

    check_links=True also verifies that every link:True field points at a file
    that actually exists on disk — catches dangling hub/journal/plan links."""
    data = read_fm(date)
    issues = []
    for field, spec in SCHEMA.items():
        if spec.get("required") and field not in data:
            issues.append(f"MISSING required: {field}")
        elif field in data:
            val = data[field]
            if spec["kind"] == "list" and not isinstance(val, list):
                issues.append(f"TYPE: '{field}' should be list, got {type(val).__name__}")
            if check_links and spec.get("link") and val:
                if "[[" not in str(val):
                    issues.append(f"LINK: '{field}' is not a wikilink: {val!r}")
                elif not _resolve_wikilink(val):
                    issues.append(f"DANGLING: '{field}' → {val} (no such file)")
    date_val = data.get("date")
    if date_val and not re.match(r'^\d{4}-\d{2}-\d{2}$', str(date_val)):
        issues.append(f"FORMAT: date '{date_val}' not YYYY-MM-DD")
    return issues


def sync_defaults(date: Optional[str] = None) -> list[str]:
    """Fill missing fields with schema defaults. Never overwrites existing."""
    data = read_fm(date)
    changed = []
    if "date" not in data:
        data["date"] = date or datetime.now().strftime("%Y-%m-%d")
        changed.append("date")
    for field, spec in SCHEMA.items():
        if field not in data and "default" in spec:
            data[field] = spec["default"]
            changed.append(field)
    if changed:
        write_fm(data, date)
    return changed


# ── CLI ───────────────────────────────────────────────────────────────────────

def _status_table(data: dict, issues: list):
    print(f"\n  {'Field':<18} {'Value':<42} Status")
    print("  " + "─" * 68)
    seen = set()
    for field, spec in SCHEMA.items():
        seen.add(field)
        val = data.get(field)
        if val is None:
            display, icon = "—", "·"
        elif isinstance(val, list):
            display = f"[{len(val)}] " + ", ".join(str(v)[:20] for v in val[:2])
            display = display[:42]
            icon = "●" if val else "○"
        else:
            s = str(val)
            display = (s[:39] + "…") if len(s) > 40 else s
            icon = "●"
        req = "required" if spec.get("required") else ""
        print(f"  {icon} {field:<18} {display:<42} {req}")
    for field, val in data.items():
        if field in seen:
            continue
        s = str(val)[:42]
        print(f"  ● {field:<18} {s:<42} extra")
    if issues:
        print(f"\n  ✗ {len(issues)} issue(s):")
        for iss in issues:
            print(f"    · {iss}")
    else:
        print(f"\n  ✓ Valid")
    print()


def main():
    import argparse
    p = argparse.ArgumentParser(description="Frontmatter manager for daily notes.")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="Target date (default: today)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status",   help="Show all fields + validation")

    pg = sub.add_parser("get",    help="Get field value")
    pg.add_argument("field")

    ps = sub.add_parser("set",    help="Set scalar field")
    ps.add_argument("field")
    ps.add_argument("value")

    pa = sub.add_parser("add",    help="Append to list field")
    pa.add_argument("field")
    pa.add_argument("value")

    pr = sub.add_parser("remove", help="Remove from list field")
    pr.add_argument("field")
    pr.add_argument("value")

    pl = sub.add_parser("link",   help="Add [[wikilink]] to related")
    pl.add_argument("target", help="Note name (adds [[]] if missing)")

    prf = sub.add_parser("ref",   help="Add path to code_refs")
    prf.add_argument("path")

    sub.add_parser("validate", help="Validate schema (exit 1 on failure)")
    sub.add_parser("sync",     help="Fill missing fields with defaults")

    args = p.parse_args()
    date = args.date

    if not args.cmd or args.cmd == "status":
        _status_table(read_fm(date), validate(date))

    elif args.cmd == "get":
        val = get_field(args.field, date)
        if isinstance(val, list):
            for item in val:
                print(f"  - {item}")
        else:
            print(val)

    elif args.cmd == "set":
        set_field(args.field, args.value, date)
        print(f"  set {args.field} = {args.value}")

    elif args.cmd == "add":
        result = add_to_list(args.field, args.value, date)
        print(f"  {args.field} ({len(result)} items)")
        for item in result:
            print(f"    - {item}")

    elif args.cmd == "remove":
        result = remove_from_list(args.field, args.value, date)
        print(f"  {args.field} ({len(result)} items remaining)")

    elif args.cmd == "link":
        target = args.target
        if not target.startswith("[["):
            target = f"[[{target}]]"
        result = add_to_list("related", target, date)
        print(f"  related ({len(result)} links)")
        for item in result:
            print(f"    - {item}")

    elif args.cmd == "ref":
        result = add_to_list("code_refs", args.path, date)
        print(f"  code_refs ({len(result)} refs)")
        for item in result:
            print(f"    - {item}")

    elif args.cmd == "validate":
        issues = validate(date)
        if issues:
            for iss in issues:
                print(f"  ✗ {iss}")
            sys.exit(1)
        else:
            print("  ✓ Valid")

    elif args.cmd == "sync":
        changed = sync_defaults(date)
        print(f"  Filled: {', '.join(changed)}" if changed else "  Nothing to fill")


if __name__ == "__main__":
    main()
