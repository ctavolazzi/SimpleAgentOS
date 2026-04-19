#!/usr/bin/env python3
"""
spin_up.py — Orchestrator for daily note initialization.

Runs Phase 1 (gather state) + Phase 2 (fill note) as a single coordinated
operation with caching, idempotency, and run transcripts.

Usage:
  python3 spin_up.py [--force] [--no-cache] [--dry-run]

Returns exit code 0 on success, nonzero on unrecovered failures.
"""

import json
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

# Local modules — all in same directory
import daily_note
import weather
import local_news
import music_pick
import git_scanner
import arxiv


# ── Config ─────────────────────────────────────────────────────────────────

CACHE_DIR = Path.home() / ".cache" / "daily-harness"
RUNS_DIR = Path.home() / ".spin_up" / "runs"
VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"

# TTLs: how long until a cached pull is considered stale
TTL_WEATHER = 3600  # 1 hour
TTL_NEWS = 10800    # 3 hours
TTL_ARXIV = 86400   # 24 hours (once per day sufficient)
TTL_GIT = 600       # 10 minutes
TTL_MUSIC = 86400   # 24 hours (only one per day needed)


# ── Caching ────────────────────────────────────────────────────────────────

def _cache_path(name: str, date: Optional[str] = None) -> Path:
    """Return cache file path for a named data source."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    return CACHE_DIR / f"{name}-{date}.json"


def _read_cache(name: str, ttl_seconds: int,
                date: Optional[str] = None) -> Optional[dict]:
    """Read cached data if it exists and is fresh."""
    path = _cache_path(name, date)
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    age = datetime.now().timestamp() - mtime
    if age > ttl_seconds:
        return None  # Stale
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_cache(name: str, data: dict, date: Optional[str] = None):
    """Write data to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(name, date)
    path.write_text(json.dumps(data, indent=2))


# ── Fetchers with cache fallback ───────────────────────────────────────────

def _fetch_weather(force: bool = False) -> Tuple[dict, str]:
    """Fetch or cache weather. Returns (data, status: ok|cached|failed)."""
    if not force:
        cached = _read_cache("weather", TTL_WEATHER)
        if cached:
            return cached, "cached"
    try:
        w = weather.fetch()
        _write_cache("weather", w)
        return w, "ok"
    except Exception as e:
        cached = _read_cache("weather", float('inf'))  # Any age OK if fetch fails
        if cached:
            return cached, f"failed (using stale: {age_str(cached.get('fetched_at'))})"
        return {}, f"failed (no cache): {e}"


def _fetch_news(force: bool = False) -> Tuple[dict, str]:
    """Fetch or cache news."""
    if not force:
        cached = _read_cache("news", TTL_NEWS)
        if cached:
            return cached, "cached"
    try:
        n = local_news.fetch("Chico California", limit=3)
        _write_cache("news", n)
        return n, "ok"
    except Exception as e:
        cached = _read_cache("news", float('inf'))
        if cached:
            return cached, f"failed (using stale)"
        return {}, f"failed (no cache): {e}"


def _fetch_arxiv(force: bool = False) -> Tuple[dict, str]:
    """Fetch or cache arxiv dual-pane digest (physics + AI/agents)."""
    if not force:
        cached = _read_cache("arxiv-dual", TTL_ARXIV)
        if cached:
            return cached, "cached"
    try:
        a = arxiv.fetch_dual_pane(days=2, top_n=arxiv.DEFAULT_TOP_N)
        _write_cache("arxiv-dual", a)
        return a, "ok"
    except Exception as e:
        cached = _read_cache("arxiv-dual", float('inf'))
        if cached:
            return cached, f"failed (using stale)"
        return {}, f"failed (no cache): {e}"


def _fetch_music(force: bool = False) -> Tuple[Optional[dict], str]:
    """Fetch or cache a verified music pick."""
    if not force:
        cached = _read_cache("music", TTL_MUSIC)
        if cached:
            return cached, "cached"
    try:
        m = music_pick.pick("Iranian jazz instrumental fusion")
        if m:
            _write_cache("music", m)
            return m, "ok"
        return None, "failed: no verified video found"
    except Exception as e:
        cached = _read_cache("music", float('inf'))
        if cached:
            return cached, f"failed (using stale)"
        return None, f"failed (no cache): {e}"


def _fetch_git_scan() -> Tuple[list, str]:
    """Scan git repos. No caching — always fresh."""
    try:
        repos = git_scanner.scan_workspace(Path("/Users/ctavolazzi/Code"))
        return repos, "ok"
    except Exception as e:
        return [], f"failed: {e}"


# ── Section writing ───────────────────────────────────────────────────────

def _write_location(w: dict, n: dict, force: bool = False) -> Tuple[bool, str]:
    """Write location section if needed."""
    status = daily_note.section_status()
    if status.get("location") == "filled" and not force:
        return True, "skipped (already filled)"
    try:
        md = f"""---

**Chico, CA** · 39.73°N, 121.84°W

**Weather:**
{weather.format_weather_md(w)}

**Local today:**
{local_news.format_news_md(n)}

---"""
        daily_note.write_section("location", md, actor="claude")
        return True, "written"
    except Exception as e:
        return False, f"failed: {e}"


def _write_sitrep(w: dict, m: Optional[dict], force: bool = False) -> Tuple[bool, str]:
    """Write sitrep with weather + music."""
    status = daily_note.section_status()
    if status.get("sitrep") == "filled" and not force:
        return True, "skipped (already filled)"
    try:
        music_section = ""
        if m:
            music_section = f"""\n**Music:** {m.get("author", "Unknown")} — "{m.get("title", "")}"
Focused work session.

{m.get("iframe", "")}"""

        md = f"""---

**Status:** {datetime.now().strftime("%A morning")} — {w.get("current_temp_f", "?")}°F in Chico, {weather.describe(w.get("weather_code", 0))[0]}. Workspace ready.

**Active threads:**
- harness: weather.py, local_news.py, music_pick.py shipped + integrated
- orchestrator: spin_up.py in progress
- north star: scientific author → Substack/YouTube/Threads

**Blockers:** None blocking spin-up.{music_section}

---"""
        daily_note.write_section("sitrep", md, actor="claude")
        return True, "written"
    except Exception as e:
        return False, f"failed: {e}"


def _write_research_feed(a: dict, force: bool = False) -> Tuple[bool, str]:
    """Write research feed section."""
    status = daily_note.section_status()
    if status.get("research_feed") == "filled" and not force:
        return True, "skipped (already filled)"
    try:
        md = arxiv.format_dual_pane_md(a) if "physics" in a else arxiv.format_digest_md(a)
        daily_note.write_section("research_feed", md, actor="claude")
        return True, "written"
    except Exception as e:
        return False, f"failed: {e}"


def _write_work_efforts(repos: list, force: bool = False) -> Tuple[bool, str]:
    """Write git health scan."""
    status = daily_note.section_status()
    if status.get("work_efforts") == "filled" and not force:
        return True, "skipped (already filled)"
    try:
        clean = sum(1 for r in repos if r.get("health") == "clean")
        dirty = sum(1 for r in repos if r.get("health") == "dirty")

        md = f"**Summary:** {len(repos)} repos · {clean} clean · {dirty} dirty\n\n"
        md += "*Scanned just now.*"

        daily_note.write_section("work_efforts", md, actor="claude")
        return True, "written"
    except Exception as e:
        return False, f"failed: {e}"


# ── Run transcript ─────────────────────────────────────────────────────────

def _write_run_transcript(results: dict):
    """Write run results to .spin_up/runs/<timestamp>.json."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{ts}.json"
    path.write_text(json.dumps(results, indent=2))
    return str(path)


# ── CLI ────────────────────────────────────────────────────────────────────

def age_str(iso_str: str) -> str:
    """Return human-readable age of an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(iso_str)
        age = datetime.now() - dt
        if age.seconds < 60:
            return f"{age.seconds}s ago"
        elif age.seconds < 3600:
            return f"{age.seconds // 60}m ago"
        elif age.seconds < 86400:
            return f"{age.seconds // 3600}h ago"
        return f"{age.days}d ago"
    except Exception:
        return iso_str


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Spin up the daily note harness with cached pulls."
    )
    parser.add_argument("--force", action="store_true",
                        help="Re-pull all data, ignore cache")
    parser.add_argument("--no-cache", action="store_true",
                        help="Don't use cache, don't write cache")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be written, don't write")
    args = parser.parse_args()

    results = {
        "timestamp": datetime.now().isoformat(),
        "args": vars(args),
        "phases": {},
    }

    # Phase 1: Gather
    print("📥 Phase 1: Gathering state...")
    phase1 = {}

    print("  • weather...", end=" ", flush=True)
    w, ws = _fetch_weather(args.force)
    phase1["weather"] = {"status": ws}
    print(ws)

    print("  • news...", end=" ", flush=True)
    n, ns = _fetch_news(args.force)
    phase1["news"] = {"status": ns}
    print(ns)

    print("  • arxiv...", end=" ", flush=True)
    a, aas = _fetch_arxiv(args.force)
    phase1["arxiv"] = {"status": aas}
    print(aas)

    print("  • music...", end=" ", flush=True)
    m, ms = _fetch_music(args.force)
    phase1["music"] = {"status": ms}
    print(ms)

    print("  • git scan...", end=" ", flush=True)
    repos, gs = _fetch_git_scan()
    phase1["git"] = {"status": gs}
    print(gs)

    results["phases"]["gather"] = phase1

    # Phase 2: Fill note
    print("\n✍️  Phase 2: Filling daily note...")
    phase2 = {}

    if not args.dry_run:
        print("  • location...", end=" ", flush=True)
        ok, status = _write_location(w, n, args.force)
        phase2["location"] = {"ok": ok, "status": status}
        print(status)

        print("  • research_feed...", end=" ", flush=True)
        ok, status = _write_research_feed(a, args.force)
        phase2["research_feed"] = {"ok": ok, "status": status}
        print(status)

        print("  • sitrep...", end=" ", flush=True)
        ok, status = _write_sitrep(w, m, args.force)
        phase2["sitrep"] = {"ok": ok, "status": status}
        print(status)

        print("  • work_efforts...", end=" ", flush=True)
        ok, status = _write_work_efforts(repos, args.force)
        phase2["work_efforts"] = {"ok": ok, "status": status}
        print(status)
    else:
        print("  (dry-run: skipping writes)")

    results["phases"]["fill"] = phase2

    # Phase 3: Frontmatter sync — populate code_refs + set focus if missing
    if not args.dry_run:
        print("\n📎 Phase 3: Frontmatter sync...")
        try:
            import frontmatter as fm
            harness_modules = [
                "_experiments/SimpleAgentOS/spin_up.py",
                "_experiments/SimpleAgentOS/arxiv.py",
                "_experiments/SimpleAgentOS/weather.py",
                "_experiments/SimpleAgentOS/local_news.py",
                "_experiments/SimpleAgentOS/git_scanner.py",
            ]
            for mod in harness_modules:
                fm.add_to_list("code_refs", mod)
            if not fm.get_field("focus"):
                fm.set_field("focus", "daily spin-up")
            print("  ✓ code_refs populated, focus set")
        except Exception as e:
            print(f"  · frontmatter sync skipped: {e}")

    # Report
    print("\n" + "=" * 60)
    print(f"✅ Spin-up complete at {datetime.now().strftime('%H:%M:%S')}")
    print(f"📋 Transcript: {_write_run_transcript(results)}")
    print(f"🌍 Weather: {w.get('current_temp_f', '?')}°F, {weather.describe(w.get('weather_code', 0))[0]}")
    print(f"📰 News: {n.get('count', 0)} stories")
    if "physics" in a:
        print(f"📚 arXiv: {len(a['physics']['papers'])} phys + {len(a['ai']['papers'])} ai "
              f"(from {a['physics']['total_fetched']}+{a['ai']['total_fetched']})")
    else:
        print(f"📚 arXiv: {a.get('count', 0)} papers")
    print(f"🎵 Music: {m.get('title', 'none') if m else 'none'}")
    print(f"📊 Repos: {len(repos)} scanned")
    print("=" * 60)

    return 0 if all(
        r.get("ok", True) for r in list(phase2.values())
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
