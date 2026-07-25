#!/usr/bin/env python3
"""
word_count.py — How many words did we actually generate today?

Scans the daily note plus every file attached to it (the wagonwheel: plan,
journal, hub, spokes, investigations, idea dumps, and any file the note
wikilinks) and counts the words. Also catches vault files written that day
that never got wired into the wheel, so escaped work still shows up.

Three buckets, because "words today" and "words in scope" are different
questions and conflating them inflates the number:

  linked_fresh    — reachable from the daily note AND modified that day.
                    This is the day's real output.
  unlinked_fresh  — modified that day but NOT reachable from the daily note.
                    Real output too, but it escaped the wagonwheel.
  linked_carried  — reachable from the daily note, modified some other day.
                    Context you were working against, not words you wrote.

Headline "words written" = linked_fresh + unlinked_fresh.

Attribution is by mtime, which is the honest limit of this measurement: a file
created Monday and edited Wednesday counts entirely toward Wednesday. For daily
notes and their same-day containers (the overwhelming majority of the corpus)
that is exactly right; for long-lived reference notes it is not. The
linked_carried bucket exists so those files stay visible without distorting the
headline.

What counts as a word: a whitespace-separated token starting with a letter or
digit. Markdown syntax (`##`, `-`, `>`, `|`) contributes nothing. YAML
frontmatter, HTML comments, and URLs are stripped before counting; fenced code
blocks are counted separately from prose so the split is visible.

Usage:
  python3 word_count.py                    # today, human summary
  python3 word_count.py --date 2026-07-20
  python3 word_count.py --json             # machine-readable
  python3 word_count.py --history 30       # per-day totals, last 30 days
  python3 word_count.py --files            # per-file breakdown
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_note

VAULT_DIR = daily_note.VAULT_DIR
CACHE_PATH = Path.home() / ".wrap_up" / "wordcount_cache.json"

# Text extensions worth counting. Everything else in the vault is an asset.
TEXT_SUFFIXES = {".md", ".txt"}

# Directory names never descended into, anywhere in the tree.
EXCLUDE_DIR_PARTS = {
    ".obsidian", ".trash", ".git", ".smart-env", ".DS_Store",
    "node_modules", "__pycache__", ".venv", "venv", ".cache",
}

# Vault-relative path prefixes excluded from counting. These hold material that
# was archived, backed up, scraped, or machine-emitted — not words we wrote.
EXCLUDE_PREFIXES = (
    "Backups/",
    "Archive/",
    "Scraped_Content/",
    "Captured/",
    "System/40-49_telemetry/",
    "System/00-09_system_meta/02_templates/",
)

# Frontmatter fields whose links are part of the day's wheel — follow these.
FOLLOW_FIELDS = {
    "plan", "journal", "hub", "wagonwheel", "case_file", "spokes",
    "related", "spoke", "investigation", "handoff", "idea_dump",
}

# Frontmatter fields that navigate AWAY from the day (up to the index, back to
# yesterday). Following them would drag the whole vault into one day's count.
SKIP_FIELDS = {
    "parent", "axle", "up", "prev", "next", "rolled_over_from",
    "tags", "type", "date", "status", "project",
}

_DATE_STEM = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The vault date-stamps its filenames, in two conventions:
#   2026-07-25.md · 2026-07-25_hub.md   and   10.90_20260725_vault_backup.md
# That stamp beats mtime for attribution — yesterday's note touched by today's
# plan rollover is still yesterday's words.
_DATE_IN_STEM = re.compile(r"(?:^|\D)(\d{4})-(\d{2})-(\d{2})(?:\D|$)")
_COMPACT_IN_STEM = re.compile(r"(?:^|\D)(\d{4})(\d{2})(\d{2})(?:\D|$)")
_FM_BLOCK = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_URL = re.compile(r"(?:https?://|www\.)\S+")
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")
# A word starts with a letter or digit; internal marks (apostrophes, hyphens,
# underscores, dots) keep `daily_note.py` and `don't` intact as single tokens.
_WORD = re.compile(r"[0-9A-Za-z][0-9A-Za-z'’_.\-]*")

STOPWORDS = {
    "a", "about", "above", "after", "again", "all", "also", "am", "an", "and",
    "any", "are", "aren", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can", "cannot", "could", "did",
    "do", "does", "doing", "don", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers",
    "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "let", "like", "me", "more", "most", "much",
    "must", "my", "myself", "no", "nor", "not", "now", "of", "off", "on", "once",
    "one", "only", "or", "other", "ought", "our", "ours", "ourselves", "out",
    "over", "own", "per", "same", "she", "should", "so", "some", "such", "than",
    "that", "the", "their", "theirs", "them", "themselves", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "under", "until",
    "up", "use", "used", "using", "very", "was", "we", "were", "what", "when",
    "where", "which", "while", "who", "whom", "why", "will", "with", "would",
    "you", "your", "yours", "yourself", "yourselves", "get", "got", "make",
    "made", "way", "via", "yet", "still", "back", "even", "ever", "every",
    "may", "might", "new", "old", "see", "seen", "say", "said", "want", "went",
    "come", "came", "take", "took", "know", "known", "think", "thought",
    # Vault / markdown / harness noise that would otherwise dominate every cloud
    "md", "http", "https", "www", "com", "org", "net", "html", "png", "jpg",
    "note", "notes", "daily", "true", "false", "null", "none", "todo", "tbd",
    "info", "tip", "warning", "quote", "summary", "index", "item", "items",
    "file", "files", "line", "lines", "add", "added", "run", "ran", "set",
}


# ── Cache ────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    """Per-file counts keyed by path, invalidated on (mtime, size)."""
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


# ── Text → counts ────────────────────────────────────────────────────────────

def split_text(text: str) -> tuple:
    """Split raw file text into (frontmatter, prose, code) strings.

    Prose is everything outside the YAML block and outside fenced code, with
    HTML comments and URLs removed and link syntax reduced to its display text.
    """
    fm_match = _FM_BLOCK.match(text)
    frontmatter = fm_match.group(1) if fm_match else ""
    body = text[fm_match.end():] if fm_match else text

    body = _HTML_COMMENT.sub(" ", body)

    prose_lines, code_lines = [], []
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        (code_lines if in_fence else prose_lines).append(line)

    prose = "\n".join(prose_lines)
    # [[Folder/Note|Display]] → Display ; [[Note]] → Note
    prose = _WIKILINK.sub(
        lambda m: (m.group(1).split("|")[-1] if "|" in m.group(1)
                   else Path(m.group(1).split("#")[0]).name),
        prose,
    )
    prose = _MD_LINK.sub(lambda m: m.group(1), prose)  # [text](url) → text
    prose = _URL.sub(" ", prose)

    return frontmatter, prose, "\n".join(code_lines)


def count_text(text: str) -> dict:
    """Word counts for one file's text: prose, code, and their total."""
    _fm, prose, code = split_text(text)
    prose_words = len(_WORD.findall(prose))
    code_words = len(_WORD.findall(code))
    return {
        "prose": prose_words,
        "code": code_words,
        "total": prose_words + code_words,
        "chars": len(prose),
    }


def tokens(text: str) -> list:
    """Meaningful lowercase prose tokens for frequency analysis."""
    _fm, prose, _code = split_text(text)
    out = []
    for raw in _WORD.findall(prose.lower()):
        word = raw.strip("._-'’")
        if len(word) < 3 or word in STOPWORDS:
            continue
        if word.replace(".", "").replace("-", "").isdigit():
            continue
        out.append(word)
    return out


# ── Vault index ──────────────────────────────────────────────────────────────

def _is_excluded(rel: str) -> bool:
    return any(rel.startswith(p) for p in EXCLUDE_PREFIXES)


def vault_files() -> list:
    """Every countable text file in the vault, exclusions applied."""
    out = []
    if not VAULT_DIR.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(VAULT_DIR):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_PARTS]
        for name in filenames:
            if Path(name).suffix.lower() not in TEXT_SUFFIXES:
                continue
            path = Path(dirpath) / name
            rel = os.path.relpath(path, VAULT_DIR)
            if _is_excluded(rel):
                continue
            out.append(path)
    return out


def build_index(files: Optional[list] = None) -> dict:
    """Map lowercase stem → [paths] so wikilinks resolve without re-globbing."""
    index = {}
    for path in (files if files is not None else vault_files()):
        index.setdefault(path.stem.lower(), []).append(path)
    return index


def resolve(target: str, index: dict) -> Optional[Path]:
    """Resolve a wikilink target (pathed or bare) to a vault file."""
    target = target.split("#")[0].split("|")[0].strip().strip('"').strip("'")
    if not target:
        return None
    for cand in (VAULT_DIR / target, VAULT_DIR / f"{target}.md"):
        if cand.is_file():
            return cand
    matches = index.get(Path(target).name.lower())
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    # Ambiguous stem — prefer the one whose path actually ends with the target.
    suffix = target.lower().rstrip(".md")
    for m in matches:
        if str(m).lower().replace(".md", "").endswith(suffix):
            return m
    return matches[0]


# ── Link closure ─────────────────────────────────────────────────────────────

def _outbound_links(text: str) -> list:
    """Wikilink targets worth following from one note."""
    fm_match = _FM_BLOCK.match(text)
    fm_block = fm_match.group(1) if fm_match else ""
    body = text[fm_match.end():] if fm_match else text

    targets = []

    # Frontmatter: only fields that point deeper into the day's own wheel.
    field = None
    for line in fm_block.splitlines():
        key = re.match(r"^([A-Za-z_][\w\-]*):\s*(.*)$", line)
        if key:
            field, value = key.group(1).lower(), key.group(2)
        elif re.match(r"^\s+-\s", line) and field:
            value = line.strip()[1:].strip()
        else:
            continue
        if field in SKIP_FIELDS or field not in FOLLOW_FIELDS:
            continue
        targets.extend(_WIKILINK.findall(value))

    targets.extend(_WIKILINK.findall(_HTML_COMMENT.sub(" ", body)))
    return targets


def _skip_target(path: Path, date: str) -> bool:
    """Links that lead out of the day rather than into it."""
    stem = path.stem
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return True   # a linked .html/.pdf artifact is markup, not words written
    if _DATE_STEM.match(stem) and stem != date:
        return True                      # yesterday's / tomorrow's note
    if stem.startswith("00.00_"):
        return True                      # vault index
    rel = os.path.relpath(path, VAULT_DIR)
    if _is_excluded(rel):
        return True
    # A hub/plan/journal belonging to a different date is that day's wheel.
    other = re.match(r"^(\d{4}-\d{2}-\d{2})_", stem)
    if other and other.group(1) != date:
        return True
    return False


def associated_files(date: str, index: dict, max_depth: int = 2,
                     max_files: int = 250) -> dict:
    """BFS the wagonwheel from the daily note. Returns {path: depth}."""
    start = daily_note.daily_path(date)
    if not start.is_file():
        return {}
    found = {start: 0}
    frontier = [start]
    depth = 0
    while frontier and depth < max_depth and len(found) < max_files:
        depth += 1
        nxt = []
        for path in frontier:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for target in _outbound_links(text):
                hit = resolve(target, index)
                if hit is None or hit in found:
                    continue
                if _skip_target(hit, date):
                    continue
                found[hit] = depth
                nxt.append(hit)
                if len(found) >= max_files:
                    break
        frontier = nxt
    return found


# ── Day scan ─────────────────────────────────────────────────────────────────

def attribution_date(path: Path, mtime: float) -> str:
    """Which day a file's words belong to.

    A date in the filename wins over mtime. Without this, every file the
    morning rollover touches — yesterday's note, hub, plan, journal — gets
    re-attributed to today and double-counts on every single day.
    """
    stem = path.stem
    for pattern in (_DATE_IN_STEM, _COMPACT_IN_STEM):
        m = pattern.search(stem)
        if not m:
            continue
        year, month, day = (int(g) for g in m.groups())
        if not (2000 <= year <= 2100):
            continue
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def _file_record(path: Path, cache: dict) -> Optional[dict]:
    """Counts for one file, cached on (mtime, size)."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = cache.get(key)
    if hit and hit.get("mtime") == st.st_mtime and hit.get("size") == st.st_size:
        counts = hit["counts"]
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        counts = count_text(text)
        cache[key] = {"mtime": st.st_mtime, "size": st.st_size, "counts": counts}
    return {
        "path": str(path),
        "rel": os.path.relpath(path, VAULT_DIR),
        "name": path.stem,
        "mtime": st.st_mtime,
        "attributed": attribution_date(path, st.st_mtime),
        **counts,
    }


def scan_day(date: Optional[str] = None, index: Optional[dict] = None,
             files: Optional[list] = None, cache: Optional[dict] = None,
             max_depth: int = 2, save_cache: bool = True) -> dict:
    """Full word-count picture for one day.

    Returns totals plus a per-file list, each file tagged with its bucket
    (linked_fresh / unlinked_fresh / linked_carried).
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    if files is None:
        files = vault_files()
    if index is None:
        index = build_index(files)
    own_cache = cache is None
    if own_cache:
        cache = _load_cache()

    linked = associated_files(date, index, max_depth=max_depth)

    records = []
    seen = set()

    for path, depth in linked.items():
        rec = _file_record(path, cache)
        if rec is None:
            continue
        rec["depth"] = depth
        rec["linked"] = True
        rec["fresh"] = rec["attributed"] == date
        rec["bucket"] = "linked_fresh" if rec["fresh"] else "linked_carried"
        records.append(rec)
        seen.add(path)

    for path in files:
        if path in seen:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if attribution_date(path, mtime) != date:
            continue
        rec = _file_record(path, cache)
        if rec is None:
            continue
        rec["depth"] = None
        rec["linked"] = False
        rec["fresh"] = True
        rec["bucket"] = "unlinked_fresh"
        records.append(rec)

    if own_cache and save_cache:
        _save_cache(cache)

    def _sum(field, pred):
        return sum(r[field] for r in records if pred(r))

    linked_fresh = [r for r in records if r["bucket"] == "linked_fresh"]
    unlinked_fresh = [r for r in records if r["bucket"] == "unlinked_fresh"]
    carried = [r for r in records if r["bucket"] == "linked_carried"]

    records.sort(key=lambda r: (-r["total"], r["rel"]))

    note_rec = next((r for r in records
                     if r["rel"].endswith(f"Daily Notes/{date}.md")), None)

    return {
        "date": date,
        "words_written": sum(r["total"] for r in linked_fresh + unlinked_fresh),
        "prose_written": sum(r["prose"] for r in linked_fresh + unlinked_fresh),
        "code_written": sum(r["code"] for r in linked_fresh + unlinked_fresh),
        "words_linked_fresh": sum(r["total"] for r in linked_fresh),
        "words_unlinked_fresh": sum(r["total"] for r in unlinked_fresh),
        "words_carried": sum(r["total"] for r in carried),
        "words_in_scope": _sum("total", lambda r: True),
        "files_written": len(linked_fresh) + len(unlinked_fresh),
        "files_linked_fresh": len(linked_fresh),
        "files_unlinked_fresh": len(unlinked_fresh),
        "files_carried": len(carried),
        "files_in_scope": len(records),
        "daily_note_words": note_rec["total"] if note_rec else 0,
        "note_exists": daily_note.daily_path(date).is_file(),
        "files": records,
    }


# ── History (for the heatmap) ────────────────────────────────────────────────

def window_records(days: int = 365, end_date: Optional[str] = None,
                   files: Optional[list] = None, cache: Optional[dict] = None,
                   save_cache: bool = True) -> list:
    """Every counted file whose attribution date falls inside the window.

    One pass; the per-day and per-area roll-ups are both built from it.
    Attribution is cheap (stem + mtime, no read), so it filters first and only
    the files that land inside the window are ever opened.
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    lo_key = (end_dt - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    hi_key = end_dt.strftime("%Y-%m-%d")

    if files is None:
        files = vault_files()
    own_cache = cache is None
    if own_cache:
        cache = _load_cache()

    out = []
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if not (lo_key <= attribution_date(path, mtime) <= hi_key):
            continue
        rec = _file_record(path, cache)
        if rec is not None:
            out.append(rec)

    if own_cache and save_cache:
        _save_cache(cache)
    return out


def rollup_history(records: list, days: int = 365,
                   end_date: Optional[str] = None) -> list:
    """Per-day totals, zero-filled across the whole window."""
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=days - 1)

    buckets = {}
    for rec in records:
        b = buckets.setdefault(rec["attributed"],
                               {"words": 0, "prose": 0, "code": 0, "files": 0})
        b["words"] += rec["total"]
        b["prose"] += rec["prose"]
        b["code"] += rec["code"]
        b["files"] += 1

    out, cursor = [], start_dt
    while cursor <= end_dt:
        key = cursor.strftime("%Y-%m-%d")
        out.append({"date": key,
                    **buckets.get(key, {"words": 0, "prose": 0,
                                        "code": 0, "files": 0})})
        cursor += timedelta(days=1)
    return out


def rollup_areas(records: list, top: int = 10) -> list:
    """Where the words landed, by top-level vault folder."""
    buckets = {}
    for rec in records:
        parts = Path(rec["rel"]).parts
        area = parts[0] if len(parts) > 1 else "(vault root)"
        b = buckets.setdefault(area, {"area": area, "words": 0, "files": 0})
        b["words"] += rec["total"]
        b["files"] += 1
    ranked = sorted(buckets.values(), key=lambda a: -a["words"])
    if len(ranked) <= top:
        return ranked
    head, tail = ranked[:top], ranked[top:]
    head.append({"area": f"Other ({len(tail)} folders)",
                 "words": sum(a["words"] for a in tail),
                 "files": sum(a["files"] for a in tail)})
    return head


def history(days: int = 365, end_date: Optional[str] = None,
            files: Optional[list] = None, cache: Optional[dict] = None,
            save_cache: bool = True) -> list:
    """Per-day word totals over a window. Convenience wrapper."""
    recs = window_records(days, end_date, files, cache, save_cache)
    return rollup_history(recs, days, end_date)


def word_frequencies(records: list, top: int = 120, cache: Optional[dict] = None) -> list:
    """Top prose words across a set of file records, for the word cloud."""
    counter = Counter()
    for rec in records:
        try:
            text = Path(rec["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        counter.update(tokens(text))
    return [{"word": w, "count": c} for w, c in counter.most_common(top)]


# ── Formatting ───────────────────────────────────────────────────────────────

def format_line(scan: dict) -> str:
    """One-line summary for the daily note / terminal."""
    return (
        f"{scan['words_written']:,} words written "
        f"({scan['prose_written']:,} prose + {scan['code_written']:,} code) "
        f"across {scan['files_written']} file(s) · "
        f"{scan['words_in_scope']:,} words in scope across "
        f"{scan['files_in_scope']} associated file(s)"
    )


def format_md(scan: dict, history_rows: Optional[list] = None, top_n: int = 8) -> str:
    """Markdown block for the daily note's EOD summary."""
    lines = [
        f"- **Words written:** {scan['words_written']:,} "
        f"({scan['prose_written']:,} prose · {scan['code_written']:,} code)",
        f"- **Files written:** {scan['files_written']} "
        f"({scan['files_linked_fresh']} wired to the note, "
        f"{scan['files_unlinked_fresh']} unlinked)",
        f"- **Daily note itself:** {scan['daily_note_words']:,} words",
        f"- **Total in scope:** {scan['words_in_scope']:,} words across "
        f"{scan['files_in_scope']} associated file(s)",
    ]
    if history_rows:
        recent = [r for r in history_rows if r["date"] <= scan["date"]][-7:]
        week = sum(r["words"] for r in recent)
        active = [r["words"] for r in recent if r["words"]]
        avg = sum(active) / len(active) if active else 0
        lines.append(f"- **Last 7 days:** {week:,} words "
                     f"({avg:,.0f}/active day)")
    top = [r for r in scan["files"] if r["fresh"]][:top_n]
    if top:
        lines.append("")
        lines.append("**Biggest files today:**")
        for r in top:
            tag = "" if r["linked"] else " *(unlinked)*"
            lines.append(f"- `{r['rel']}` — {r['total']:,} words{tag}")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Word counts for the daily note and everything attached to it.")
    p.add_argument("--date", metavar="YYYY-MM-DD", help="target date (default today)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--files", action="store_true", help="per-file breakdown")
    p.add_argument("--history", type=int, metavar="N", default=0,
                   help="also print per-day totals for the last N days")
    p.add_argument("--depth", type=int, default=2,
                   help="wikilink hops to follow from the daily note (default 2)")
    args = p.parse_args()

    date = args.date or datetime.now().strftime("%Y-%m-%d")
    files = vault_files()
    index = build_index(files)
    cache = _load_cache()

    scan = scan_day(date, index=index, files=files, cache=cache,
                    max_depth=args.depth, save_cache=False)
    rows = history(args.history, end_date=date, files=files, cache=cache,
                   save_cache=False) if args.history else None
    _save_cache(cache)

    if args.json:
        payload = dict(scan)
        if rows:
            payload["history"] = rows
        print(json.dumps(payload, indent=2))
        return 0

    print(f"\n📝 Word count — {date}\n" + "─" * 60)
    if not scan["note_exists"]:
        print("  (no daily note for this date — counting mtime-fresh files only)")
    print(f"  Words written    : {scan['words_written']:,}"
          f"   ({scan['prose_written']:,} prose · {scan['code_written']:,} code)")
    print(f"  Files written    : {scan['files_written']}"
          f"   ({scan['files_linked_fresh']} wired · "
          f"{scan['files_unlinked_fresh']} unlinked)")
    print(f"  Daily note       : {scan['daily_note_words']:,} words")
    print(f"  In scope (total) : {scan['words_in_scope']:,} words across "
          f"{scan['files_in_scope']} file(s)")

    if args.files:
        print("\n  Per file:")
        badge = {"linked_fresh": "●", "unlinked_fresh": "○", "linked_carried": "·"}
        for r in scan["files"]:
            print(f"    {badge[r['bucket']]} {r['total']:>7,}  {r['rel']}")
        print("\n    ● wired + written today   ○ written today, unlinked"
              "   · associated, written earlier")

    if rows:
        print(f"\n  Last {args.history} days:")
        for r in rows[-args.history:]:
            if r["words"]:
                bar = "█" * min(40, max(1, r["words"] // 250))
                print(f"    {r['date']}  {r['words']:>7,}  {bar}")
    print("─" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
