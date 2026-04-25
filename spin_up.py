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
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# Local modules — all in same directory
import daily_note
import daily_image
import image_quip
import horoscope
import weather
import local_news
import music_pick
import work_vibe
import waft_workspace
import git_scanner
import arxiv


# ── Config ─────────────────────────────────────────────────────────────────

CACHE_DIR = Path.home() / ".cache" / "daily-harness"
RUNS_DIR = Path.home() / ".spin_up" / "runs"
VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
CODE_ROOT = Path.home() / "Code"
HARNESS_DIR = Path(__file__).parent.resolve()

_HARNESS_FILES = [
    "spin_up.py",
    "arxiv.py",
    "weather.py",
    "local_news.py",
    "git_scanner.py",
    "work_vibe.py",
    "waft_workspace.py"
]

# TTLs: how long until a cached pull is considered stale
TTL_WEATHER = 3600  # 1 hour
TTL_NEWS = 10800    # 3 hours
TTL_ARXIV = 86400   # 24 hours (once per day sufficient)
TTL_GIT = 600       # 10 minutes
TTL_MUSIC = 86400   # 24 hours (only one per day needed)
TTL_IMAGE = 86400   # 24 hours (one POTD per day)
TTL_HOROSCOPE = 86400  # 24 hours (one reading per day)


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
            return cached, f"failed (using stale)"
        return {}, f"failed (no cache) ({type(e).__name__}): {e}"


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
        return {}, f"failed (no cache) ({type(e).__name__}): {e}"


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
        return {}, f"failed (no cache) ({type(e).__name__}): {e}"


def _fetch_music(force: bool = False) -> Tuple[Optional[dict], str]:
    """Fetch or cache a work-vibe-matched music pick."""
    if not force:
        cached = _read_cache("music", TTL_MUSIC)
        if cached:
            return cached, "cached"
    try:
        vibe = work_vibe.derive()
        query = vibe["music_query"]
        m = music_pick.pick(query)
        if m:
            m["vibe_label"] = vibe["vibe_label"]
            m["music_query"] = query
            _write_cache("music", m)
            return m, f"ok (vibe: {vibe['vibe_label']})"
        return None, "failed: no verified video found"
    except Exception as e:
        cached = _read_cache("music", float('inf'))
        if cached:
            return cached, "failed (using stale)"
        return None, f"failed (no cache) ({type(e).__name__}): {e}"


def _fetch_git_scan() -> Tuple[list, str]:
    """Scan git repos. No caching — always fresh."""
    try:
        repos = git_scanner.scan_workspace(CODE_ROOT)
        return repos, "ok"
    except Exception as e:
        return [], f"failed ({type(e).__name__}): {e}"


def _fetch_waft_state() -> Tuple[Optional[dict], str]:
    """Fetch local WAFT Being state + quest derivation. No caching — always live."""
    try:
        data = waft_workspace.fetch()
        return data, "ok"
    except Exception as e:
        return None, f"failed ({type(e).__name__}): {e}"


def _fetch_horoscope(force: bool = False) -> Tuple[Optional[dict], str]:
    """Fetch daily horoscope. Cached 24h."""
    if not force:
        cached = _read_cache("horoscope", TTL_HOROSCOPE)
        if cached:
            return cached, f"cached ({cached.get('sign', '?')})"
    try:
        data = horoscope.fetch()
        if data.get("error"):
            raise RuntimeError(data["error"])
        _write_cache("horoscope", data)
        return data, f"ok ({data['sign']})"
    except Exception as e:
        cached = _read_cache("horoscope", float('inf'))
        if cached:
            return cached, "failed (using stale)"
        return None, f"failed (no cache) ({type(e).__name__}): {e}"


def _fetch_daily_image(force: bool = False) -> Tuple[Optional[dict], str]:
    """Fetch today's hero image (Wikimedia POTD → Lorem Picsum fallback)."""
    if not force:
        cached = _read_cache("daily_image", TTL_IMAGE)
        if cached:
            return cached, f"cached ({cached.get('source', '?')})"
    try:
        img = daily_image.fetch()
        _write_cache("daily_image", img)
        return img, f"ok ({img['source']})"
    except Exception as e:
        cached = _read_cache("daily_image", float('inf'))
        if cached:
            return cached, "failed (using stale)"
        return None, f"failed (no cache) ({type(e).__name__}): {e}"


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
        return False, f"failed ({type(e).__name__}): {e}"


def _write_sitrep(w: dict, m: Optional[dict], force: bool = False) -> Tuple[bool, str]:
    """Write sitrep with weather + music."""
    status = daily_note.section_status()
    if status.get("sitrep") == "filled" and not force:
        return True, "skipped (already filled)"
    try:
        music_section = ""
        if m:
            vibe_label = m.get("vibe_label", "focused work session")
            music_section = f"""\n**Music:** [{m.get("title", "")}](https://www.youtube.com/watch?v={m.get("video_id", "")}) — {m.get("author", "Unknown")}
*Vibe: {vibe_label}*

{m.get("iframe", "")}"""

        md = f"""---

**Status:** {datetime.now().strftime("%A morning")} — {w.get("current_temp_f", "?")}°F in Chico, {weather.describe(w.get("weather_code", 0))[0]}. Workspace ready.

**Active threads:**
- —

**Blockers:** None blocking spin-up.{music_section}

---"""
        daily_note.write_section("sitrep", md, actor="claude")
        return True, "written"
    except Exception as e:
        return False, f"failed ({type(e).__name__}): {e}"


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
        return False, f"failed ({type(e).__name__}): {e}"


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
        return False, f"failed ({type(e).__name__}): {e}"


def _write_waft_workspace(waft_data: Optional[dict], force: bool = False) -> Tuple[bool, str]:
    """Write WAFT Workspace section to today's note."""
    status = daily_note.section_status()
    if status.get("waft_workspace") == "filled" and not force:
        return True, "skipped (already filled)"
    if waft_data is None:
        return False, "skipped (no waft data)"
    try:
        md = waft_workspace.format_md(waft_data)
        daily_note.write_section("waft_workspace", md, actor="claude")
        return True, "written"
    except Exception as e:
        return False, f"failed ({type(e).__name__}): {e}"


def _write_daily_reading(
    horo: Optional[dict],
    waft_data: Optional[dict],
    force: bool = False,
) -> Tuple[bool, str]:
    """Write daily reading (horoscope + WAFT cosmic overlay). Injects section if absent."""
    import re
    if horo is None and waft_data is None:
        return False, "skipped (no data)"

    status = daily_note.section_status()
    if status.get("daily_reading") == "filled" and not force:
        return True, "skipped (already filled)"

    md = horoscope.format_md(horo or {}, waft_data)

    if status.get("daily_reading") != "absent":
        try:
            daily_note.write_section("daily_reading", md, actor="claude")
            return True, "written"
        except Exception as e:
            return False, f"failed ({type(e).__name__}): {e}"

    # Section header not in note — inject it after the hero_image block (or nav block)
    path = daily_note.daily_path()
    text = path.read_text(encoding="utf-8")

    section_block = f"## Daily Reading\n\n{md}\n"

    # Prefer inserting after <!-- /hero_image --> if present
    if "<!-- /hero_image -->" in text:
        new_text = text.replace(
            "<!-- /hero_image -->",
            f"<!-- /hero_image -->\n\n{section_block}",
            1,
        )
    else:
        # Fall back: insert before ## Location
        new_text = re.sub(
            r'(^## Location)',
            f"{section_block}\n\\1",
            text,
            count=1,
            flags=re.MULTILINE,
        )

    path.write_text(new_text, encoding="utf-8")
    return True, "injected"


def _write_hero_image(img: Optional[dict], quip: str = "", force: bool = False) -> Tuple[bool, str]:
    """Inject hero image block below the nav line, above ## Location."""
    import re
    if not img or not img.get("url"):
        return False, "skipped (no image data)"

    path = daily_note.daily_path()
    text = path.read_text(encoding="utf-8")

    url = img["url"]
    caption = img.get("caption", "")
    source_url = img.get("source_url", "")
    source = img.get("source", "")

    caption_line = ""
    if caption or source_url:
        cap_text = caption[:160] if caption else ""
        src_part = f" · [Source]({source_url})" if source_url else ""
        src_name = f" · {source}" if source else ""
        caption_line = f"\n*{cap_text}{src_part}{src_name}*"

    quip_line = ""
    if quip:
        quip_line = f"\n\n> *{quip}*"
    else:
        quip_line = "\n\n> *<!-- quip: tie the image to today's work -->*"

    img_md = f"![Daily Image]({url}){caption_line}{quip_line}"

    MARKER = "<!-- hero_image -->"
    END_MARKER = "<!-- /hero_image -->"

    if MARKER in text:
        if not force:
            return True, "skipped (already present)"
        new_block = f"{MARKER}\n{img_md}\n{END_MARKER}"
        new_text = re.sub(
            rf'{re.escape(MARKER)}.*?{re.escape(END_MARKER)}',
            new_block,
            text,
            count=1,
            flags=re.DOTALL,
        )
    else:
        m = re.search(r'(\*\*Yesterday:\*\*.*?\n\n---\n)', text, re.DOTALL)
        if not m:
            return False, "failed (nav divider not found)"
        insert_pos = m.end()
        new_block = f"\n{MARKER}\n{img_md}\n{END_MARKER}\n\n"
        new_text = text[:insert_pos] + new_block + text[insert_pos:]

    path.write_text(new_text, encoding="utf-8")
    return True, "written"


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

    print("  • waft state...", end=" ", flush=True)
    waft_data, wws = _fetch_waft_state()
    phase1["waft"] = {"status": wws}
    print(wws)

    print("  • we_factory (quests)...", end=" ", flush=True)
    try:
        import we_factory
        pending = [q for q in (waft_data or {}).get("quests", []) if not q.get("complete")][:3]
        results = we_factory.create_for_quests(pending)
        created = sum(1 for r in results if r["status"] == "created")
        existed = sum(1 for r in results if r["status"] == "exists")
        skipped = sum(1 for r in results if r["status"] == "skipped_not_worthy")
        phase1["we_factory"] = {
            "status": f"ok ({created} new, {existed} existed, {skipped} not-worthy)",
            "results": results,
        }
        print(f"ok ({created} new, {existed} existing, {skipped} not-worthy)")
    except Exception as e:
        phase1["we_factory"] = {"status": f"failed: {e}"}
        print(f"failed: {e}")

    print("  • horoscope...", end=" ", flush=True)
    horo, hs = _fetch_horoscope(args.force)
    phase1["horoscope"] = {"status": hs}
    print(hs)

    print("  • daily image...", end=" ", flush=True)
    img, imgs = _fetch_daily_image(args.force)
    phase1["daily_image"] = {"status": imgs}
    print(imgs)

    print("  • image quip...", end=" ", flush=True)
    _focus = ""
    _top_quest = ""
    try:
        import re as _re
        _full = daily_note.daily_path().read_text(encoding="utf-8")
        _fm = _re.search(r"^---\n(.*?)\n---", _full, _re.DOTALL)
        if _fm:
            _fm_focus = _re.search(r"^focus:\s*(.+)$", _fm.group(1), _re.MULTILINE)
            if _fm_focus:
                _focus = _fm_focus.group(1).strip().strip('"').strip("'")
        if waft_data and waft_data.get("quests"):
            _pending = [q for q in waft_data["quests"] if not q.get("complete")]
            if _pending:
                _top_quest = _pending[0].get("task", "")
    except Exception:
        pass
    _quip = image_quip.generate(
        (img or {}).get("caption", ""), _focus, _top_quest, force=args.force
    )
    phase1["image_quip"] = {"status": "ok" if _quip else "empty", "quip": _quip}
    print("ok" if _quip else "empty (will use placeholder)")

    results["phases"]["gather"] = phase1

    # Phase 2: Fill note
    print("\n✍️  Phase 2: Filling daily note...")
    phase2 = {}

    if not args.dry_run:
        print("  • hero_image...", end=" ", flush=True)
        ok, status = _write_hero_image(img, quip=_quip, force=args.force)
        phase2["hero_image"] = {"ok": ok, "status": status}
        print(status)

        print("  • daily_reading...", end=" ", flush=True)
        ok, status = _write_daily_reading(horo, waft_data, args.force)
        phase2["daily_reading"] = {"ok": ok, "status": status}
        print(status)

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

        print("  • waft_workspace...", end=" ", flush=True)
        ok, status = _write_waft_workspace(waft_data, args.force)
        phase2["waft_workspace"] = {"ok": ok, "status": status}
        print(status)

        # Write session-start entry to being journal
        if waft_data:
            quests = waft_data.get("quests", [])
            quest_labels = [q["task"][:60] for q in quests]
            waft_workspace.write_being_journal_entry(
                "session_start",
                f"spin_up · {len(quests)} quest(s) loaded",
                details=quest_labels or ["no quests derived"],
                being_state=waft_data.get("being"),
            )
    else:
        print("  (dry-run: skipping writes)")

    results["phases"]["fill"] = phase2

    # Phase 3: Frontmatter sync
    if not args.dry_run:
        print("\n📎 Phase 3: Frontmatter sync...")
        try:
            import frontmatter as fm
            harness_modules = [
                str((HARNESS_DIR / f).relative_to(CODE_ROOT))
                for f in _HARNESS_FILES
                if (HARNESS_DIR / f).exists()
            ]
            for mod in harness_modules:
                fm.add_to_list("code_refs", mod)
            if not fm.get_field("focus"):
                fm.set_field("focus", "daily spin-up")
            print("  ✓ code_refs populated, focus set")
        except Exception as e:
            print(f"  · frontmatter sync skipped ({type(e).__name__}): {e}")

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
