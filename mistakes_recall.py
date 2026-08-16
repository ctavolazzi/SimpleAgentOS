#!/usr/bin/env python3
"""
mistakes_recall.py - queryable recall over the mistakes ledger and the vault.

v0.0.1

The corpus lives in the vault (CT decision, 2026-08-15): every entry in
~/.claude/MISTAKES.md is mirrored as a spoke note under
_work_efforts_/00-09_meta/00_mistakes/, and the search index covers those
spokes plus all work efforts and the Claude Journal. The ledger stays the
source of truth; the spokes are managed output of `sync` and hand edits to
them are overwritten.

Commands:
    sync                 mirror ledger entries into vault spokes (idempotent)
    build [--no-sync]    sync, then rebuild the FTS5 index in ~/.cache
    query TERMS...       search the index; --type, --limit, --json
    hook                 PreToolUse mode: read hook JSON on stdin, emit
                         additionalContext when the pending action matches a
                         known mistake trigger; always exits 0

The hook path never touches SQLite. It parses the ledger directly (15 KB,
single read) and matches curated per-entry trigger patterns, so it cannot go
stale against an unbuilt index and it stays high-precision: a pattern fires
on the distinctive fingerprint of a past mistake, not on shared vocabulary.
"""

import json
import os
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

VERSION = "0.0.1"

LEDGER = Path(os.environ.get("MISTAKES_RECALL_LEDGER")
              or Path.home() / ".claude" / "MISTAKES.md")
VAULT = Path(os.environ.get("MISTAKES_RECALL_VAULT")
             or Path.home() / "Documents" / "Personal-Remote-Vault")
CACHE = Path(os.environ.get("MISTAKES_RECALL_CACHE")
             or Path.home() / ".cache" / "mistakes_recall")

CORPUS_DIR_REL = Path("_work_efforts_") / "00-09_meta" / "00_mistakes"
INDEX_NOTE_BASENAME = "mistakes_ledger_index"
DB_NAME = "index.db"

# Curated trigger patterns per ledger entry. Case-insensitive regex, matched
# against the flattened tool input of a pending action. High precision only:
# a pattern belongs here when its presence alone is strong evidence the
# session is about to walk into that entry's failure mode. Entries with no
# distinctive command fingerprint (behavioral mistakes like M-08 or M-10)
# are reachable through `query`, not the hook.
TRIGGERS = {
    "M-02": [r"--dump-dom", r"--virtual-time-budget"],
    "M-03": [r"--window-size=[1-4]\d\d\b"],
    "M-04": [r"--screenshot=/dev/null"],
    "M-05": [r"rsync[^\n|;&]*--dry-run", r"--dry-run[^\n|;&]*rsync"],
    "M-07": [r"update_frontmatter_fields"],
}

STOPWORDS = frozenset("""
a an and are as at be by for from has have i if in into is it its no not of
on or that the this to was were will with you your
""".split())


# -- ledger parsing ----------------------------------------------------------

def parse_ledger(text: str) -> list:
    """Parse MISTAKES.md into entry dicts.

    Returns [{id, title, family, body, what, why, rule, check}].
    Family is the `## N. heading` the entry sits under.
    """
    entries = []
    family = ""
    current = None
    for line in text.splitlines():
        fam = re.match(r"^## (\d+)\. (.+)$", line)
        if fam:
            family = fam.group(2).strip()
        m = re.match(r"^### (M-\d+)\. (.+)$", line)
        if m:
            if current:
                entries.append(current)
            current = {"id": m.group(1), "title": m.group(2).strip(),
                       "family": family, "lines": []}
            continue
        if current is not None:
            if line.startswith("## ") or line.startswith("---"):
                entries.append(current)
                current = None
            else:
                current["lines"].append(line)
    if current:
        entries.append(current)

    for e in entries:
        e["body"] = "\n".join(e.pop("lines")).strip()
        for field in ("what", "why", "rule", "check"):
            m = re.search(
                rf"\*\*{field.capitalize()}:\*\*\s*(.+?)(?=\n\s*\n|\Z)",
                e["body"], re.S)
            e[field] = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    return entries


def ledger_header_count(text: str):
    m = re.search(r"^\*\*Entries:\*\*\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


# -- vault spoke sync --------------------------------------------------------

def _slug(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s[:60].rstrip("_")


def _entry_hash(e: dict) -> str:
    import hashlib
    raw = "\n".join([e["id"], e["title"], e["family"], e["body"]])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _spoke_content(e: dict, today: str) -> str:
    triggers = TRIGGERS.get(e["id"], [])
    trig_yaml = ""
    if triggers:
        trig_yaml = "trigger_patterns:\n" + "".join(
            f'  - "{t}"\n' for t in triggers)
    return f"""---
title: "{e['id']}: {e['title']}"
entry: "{e['id']}"
family: "{e['family']}"
managed_by: "mistakes_recall sync"
source: "~/.claude/MISTAKES.md"
source_hash: "{_entry_hash(e)}"
last_synced: "{today}"
{trig_yaml}---
# {e['id']}: {e['title']}

> Managed mirror of ledger entry {e['id']} in `~/.claude/MISTAKES.md`.
> Hand edits here are overwritten by `mistakes_recall sync`. Edit the ledger.
> Corpus index: [[{INDEX_NOTE_BASENAME}]]

Family: {e['family']}

{e['body']}
"""


def _index_note_content(entries: list, today: str) -> str:
    rows = "\n".join(
        f"- [[{e['id']}_{_slug(e['title'])}|{e['id']}]]: {e['title']}"
        for e in entries)
    return f"""---
title: "Mistakes ledger corpus index"
managed_by: "mistakes_recall sync"
source: "~/.claude/MISTAKES.md"
entry_count: {len(entries)}
last_synced: "{today}"
---
# Mistakes ledger corpus index

> Managed by `mistakes_recall sync`. One spoke per entry in
> `~/.claude/MISTAKES.md`, which stays the source of truth.
> Query with: `python3 mistakes_recall.py query "..."`

{rows}
"""


def sync(verbose: bool = True) -> dict:
    """Mirror ledger entries into vault spokes. Returns a change summary."""
    import atomic_io
    from datetime import date
    text = LEDGER.read_text(encoding="utf-8")
    entries = parse_ledger(text)
    header = ledger_header_count(text)
    if header is not None and header != len(entries):
        print(f"WARN: ledger header says {header} entries, parsed "
              f"{len(entries)}", file=sys.stderr)

    corpus = VAULT / CORPUS_DIR_REL
    corpus.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    written, unchanged = [], []

    with atomic_io.vault_lock(vault_root=VAULT):
        for e in entries:
            path = corpus / f"{e['id']}_{_slug(e['title'])}.md"
            if path.exists():
                old = path.read_text(encoding="utf-8")
                m = re.search(r'^source_hash: "(\w+)"', old, re.M)
                if m and m.group(1) == _entry_hash(e):
                    unchanged.append(e["id"])
                    continue
            atomic_io.atomic_write(path, _spoke_content(e, today))
            written.append(e["id"])

        idx_path = corpus / f"{INDEX_NOTE_BASENAME}.md"
        idx_new = _index_note_content(entries, today)
        # Rewrite the index note only when the entry list itself changed,
        # so a no-op sync leaves every mtime alone.
        rewrite_idx = True
        if idx_path.exists():
            old_rows = re.findall(r"^- \[\[.+$", idx_path.read_text(
                encoding="utf-8"), re.M)
            new_rows = re.findall(r"^- \[\[.+$", idx_new, re.M)
            rewrite_idx = old_rows != new_rows
        if rewrite_idx:
            atomic_io.atomic_write(idx_path, idx_new)

    summary = {"entries": len(entries), "written": written,
               "unchanged": unchanged, "index_rewritten": rewrite_idx}
    if verbose:
        print(f"sync: {len(entries)} entries, {len(written)} written, "
              f"{len(unchanged)} unchanged, index "
              f"{'rewritten' if rewrite_idx else 'untouched'}")
    return summary


# -- FTS5 index --------------------------------------------------------------

def _iter_corpus():
    """Yield (doc_type, doc_id, title, body, path) for every indexable doc."""
    corpus = VAULT / CORPUS_DIR_REL
    for p in sorted(corpus.glob("M-*.md")) if corpus.is_dir() else []:
        text = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^title: "(.+)"$', text, re.M)
        yield ("mistake", p.stem, m.group(1) if m else p.stem, text, str(p))

    we_root = VAULT / "_work_efforts_"
    if we_root.is_dir():
        for p in sorted(we_root.rglob("*.md")):
            if CORPUS_DIR_REL.name in p.parts:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            yield ("work_effort", p.stem, p.stem, text, str(p))

    journal = VAULT / "Claude Journal"
    if journal.is_dir():
        for p in sorted(journal.glob("*.md")):
            text = p.read_text(encoding="utf-8", errors="replace")
            yield ("journal", p.stem, p.stem, text, str(p))


def build(do_sync: bool = True) -> int:
    if do_sync:
        sync(verbose=False)
    CACHE.mkdir(parents=True, exist_ok=True)
    db_path = CACHE / DB_NAME
    tmp = CACHE / (DB_NAME + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.execute(
        "CREATE VIRTUAL TABLE docs USING fts5("
        "doc_type, doc_id, title, body, path UNINDEXED, "
        "tokenize='porter unicode61')")
    n = 0
    for row in _iter_corpus():
        con.execute("INSERT INTO docs VALUES (?,?,?,?,?)", row)
        n += 1
    con.commit()
    con.close()
    tmp.replace(db_path)
    print(f"build: indexed {n} docs -> {db_path}")
    return n


def _fts_escape(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def query(terms: list, doc_type=None, limit: int = 8, as_json: bool = False):
    db_path = CACHE / DB_NAME
    if not db_path.exists():
        print("no index yet: run `mistakes_recall.py build` first",
              file=sys.stderr)
        return []
    tokens = [t for t in re.split(r"[^\w./-]+", " ".join(terms))
              if t and t.lower() not in STOPWORDS]
    if not tokens:
        return []
    match = " OR ".join(_fts_escape(t) for t in tokens)
    con = sqlite3.connect(db_path)
    sql = ("SELECT doc_type, doc_id, title, path, bm25(docs) AS score, "
           "snippet(docs, 3, '>>', '<<', ' ... ', 18) "
           "FROM docs WHERE docs MATCH ?")
    args = [match]
    if doc_type:
        sql += " AND doc_type = ?"
        args.append(doc_type)
    sql += " ORDER BY score LIMIT ?"
    args.append(limit)
    rows = con.execute(sql, args).fetchall()
    con.close()
    results = [{"doc_type": r[0], "doc_id": r[1], "title": r[2],
                "path": r[3], "score": round(r[4], 3), "snippet": r[5]}
               for r in rows]
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"[{r['doc_type']}] {r['doc_id']}  (score {r['score']})")
            print(f"    {r['snippet']}")
            print(f"    {r['path']}")
    return results


# -- PreToolUse hook ---------------------------------------------------------

def _flatten(obj) -> str:
    if isinstance(obj, dict):
        return " ".join(_flatten(v) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(_flatten(v) for v in obj)
    return str(obj)


def hook() -> int:
    """Read PreToolUse JSON on stdin; emit additionalContext on a trigger hit.

    Emits each entry at most once per session (state in CACHE). Any failure
    is swallowed: this must never block or slow a tool call visibly.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        haystack = _flatten(payload.get("tool_input", {}))[:20000]
        if not haystack:
            return 0
        hits = []
        for entry_id, patterns in TRIGGERS.items():
            if any(re.search(p, haystack, re.I) for p in patterns):
                hits.append(entry_id)
        if not hits:
            return 0

        session = str(payload.get("session_id", "nosession"))
        CACHE.mkdir(parents=True, exist_ok=True)
        state_path = CACHE / "hook_state.json"
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        seen = set(state.get(session, []))
        fresh = [h for h in hits if h not in seen][:2]
        if not fresh:
            return 0
        # Keep every session's seen-list: several tabs share this file, and
        # dropping other sessions here clobbers their suppression state
        # (observed live 2026-08-15: M-02 re-emitted minutes after being
        # suppressed because another tab's write reset the state). Bound the
        # file by evicting oldest sessions past 50.
        state[session] = sorted(seen | set(fresh))
        while len(state) > 50:
            state.pop(next(iter(state)))
        state_path.write_text(json.dumps(state))

        text = LEDGER.read_text(encoding="utf-8")
        by_id = {e["id"]: e for e in parse_ledger(text)}
        lines = []
        for h in fresh:
            e = by_id.get(h)
            if not e:
                continue
            lines.append(f"{h} ({e['title']}). Rule: {e['rule']}")
        if not lines:
            return 0
        ctx = ("MISTAKES RECALL: the pending action matches ledger "
               "entries in ~/.claude/MISTAKES.md. "
               + " | ".join(lines))
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": ctx}}))
        return 0
    except Exception:
        return 0


# -- CLI ---------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    b = sub.add_parser("build")
    b.add_argument("--no-sync", action="store_true")
    q = sub.add_parser("query")
    q.add_argument("terms", nargs="+")
    q.add_argument("--type", choices=["mistake", "work_effort", "journal"])
    q.add_argument("--limit", type=int, default=8)
    q.add_argument("--json", action="store_true")
    sub.add_parser("hook")
    args = ap.parse_args(argv)

    if args.cmd == "sync":
        sync()
        return 0
    if args.cmd == "build":
        build(do_sync=not args.no_sync)
        return 0
    if args.cmd == "query":
        query(args.terms, doc_type=args.type, limit=args.limit,
              as_json=args.json)
        return 0
    if args.cmd == "hook":
        return hook()
    return 1


if __name__ == "__main__":
    sys.exit(main())
