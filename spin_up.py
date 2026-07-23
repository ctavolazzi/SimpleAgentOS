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
import atomic_io
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
import air_quality
import on_this_day
import daily_quote
import vault_stats
import lab_report
import commit_summary
import data_archive

# WIRETAP telemetry — optional live observability (~/Code/_experiments/wiretap).
# Fails soft everywhere: if the wire isn't running, spin-up must not notice.
try:
    sys.path.append(str(Path.home() / "Code" / "_experiments" / "wiretap"))
    import tap as _wiretap
except Exception:
    _wiretap = None


def _tap(msg: str, level: str = "info"):
    if _wiretap is None:
        return
    try:
        _wiretap.log("spin_up", msg, level)
    except Exception:
        pass


def _tap_status(name: str, status: str):
    lvl = "warn" if any(k in status for k in ("failed", "degraded", "empty")) else "info"
    _tap(f"{name} · {status}", lvl)


# ── Config ─────────────────────────────────────────────────────────────────

CACHE_DIR = Path.home() / ".cache" / "daily-harness"
RUNS_DIR = Path.home() / ".spin_up" / "runs"
VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
CODE_ROOT = Path.home() / "Code"
HARNESS_DIR = Path(__file__).parent.resolve()
TOOLS_DIR = Path.home() / ".claude" / "tools"

_HARNESS_FILES = [
    "spin_up.py",
    "arxiv.py",
    "weather.py",
    "local_news.py",
    "git_scanner.py",
    "work_vibe.py",
    "waft_workspace.py",
    "sitrep_gen.py",
    "check_in.py",
    "frontmatter.py",
    "air_quality.py",
    "on_this_day.py",
    "daily_quote.py",
    "vault_stats.py",
    "lab_report.py",
    "commit_summary.py",
    "data_archive.py",
]

# TTLs: how long until a cached pull is considered stale
TTL_WEATHER = 3600  # 1 hour
TTL_NEWS = 10800    # 3 hours
TTL_ARXIV = 86400   # 24 hours (once per day sufficient)
TTL_GIT = 600       # 10 minutes
TTL_MUSIC = 86400   # 24 hours (only one per day needed)
TTL_IMAGE = 86400   # 24 hours (one POTD per day)
TTL_HOROSCOPE = 86400  # 24 hours (one reading per day)
TTL_AIR = 3600      # 1 hour (AQI moves with the smoke)
TTL_OTD = 86400     # 24 hours (history doesn't change intraday)
TTL_QUOTE = 86400   # 24 hours (one quote per day by design)


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


def _fetch_air_quality(force: bool = False) -> Tuple[Optional[dict], str]:
    """Fetch current AQI. Cached 1h."""
    if not force:
        cached = _read_cache("air_quality", TTL_AIR)
        if cached:
            return cached, "cached"
    try:
        aq = air_quality.fetch()
        _write_cache("air_quality", aq)
        return aq, f"ok (AQI {aq['us_aqi']:.0f})"
    except Exception as e:
        cached = _read_cache("air_quality", float('inf'))
        if cached:
            return cached, "failed (using stale)"
        return None, f"failed (no cache) ({type(e).__name__}): {e}"


def _fetch_on_this_day(force: bool = False) -> Tuple[Optional[dict], str]:
    """Fetch on-this-day history events. Cached 24h."""
    if not force:
        cached = _read_cache("on_this_day", TTL_OTD)
        if cached:
            return cached, "cached"
    try:
        otd = on_this_day.fetch()
        _write_cache("on_this_day", otd)
        return otd, f"ok ({len(otd['events'])} events)"
    except Exception as e:
        cached = _read_cache("on_this_day", float('inf'))
        if cached:
            return cached, "failed (using stale)"
        return None, f"failed (no cache) ({type(e).__name__}): {e}"


def _fetch_quote(force: bool = False) -> Tuple[Optional[dict], str]:
    """Fetch quote of the day. Cached 24h."""
    if not force:
        cached = _read_cache("daily_quote", TTL_QUOTE)
        if cached:
            return cached, "cached"
    try:
        q = daily_quote.fetch()
        _write_cache("daily_quote", q)
        return q, f"ok ({q['author']})"
    except Exception as e:
        cached = _read_cache("daily_quote", float('inf'))
        if cached:
            return cached, "failed (using stale)"
        return None, f"failed (no cache) ({type(e).__name__}): {e}"


# ── Section writing ───────────────────────────────────────────────────────

def _write_location(w: dict, n: dict, aq: Optional[dict] = None,
                    force: bool = False) -> Tuple[bool, str]:
    """Write location section if needed."""
    status = daily_note.section_status()
    if status.get("location") == "filled" and not force:
        return True, "skipped (already filled)"
    try:
        air_line = ""
        if aq:
            air_line = f"\n\n**Air quality:**\n{air_quality.format_md(aq)}"
        md = f"""---

**Chico, CA** · 39.73°N, 121.84°W

**Weather:**
{weather.format_weather_md(w)}{air_line}

**Local today:**
{local_news.format_news_md(n)}

---"""
        daily_note.write_section("location", md, actor="claude")
        return True, "written"
    except Exception as e:
        return False, f"failed ({type(e).__name__}): {e}"


# Marker embedded in generated In the Lab content. Presence of this string is
# the ONLY thing that authorizes an overwrite — a human's notes never carry it,
# so even --force cannot clobber hand-written lab content.
_LAB_MARKER = "refreshed each spin-up"


def _write_in_the_lab(repos: list, force: bool = False) -> Tuple[bool, str]:
    """Fill In the Lab with the morning workbench snapshot."""
    status = daily_note.section_status()
    current = status.get("in_the_lab", "absent")
    if current == "filled":
        existing = daily_note.read_section("in_the_lab")
        if _LAB_MARKER not in existing:
            return True, "skipped (human content — never overwrite)"
        if not force:
            return True, "skipped (already filled)"
    try:
        md = lab_report.format_md(lab_report.build(repos))
        daily_note.write_section("in_the_lab", md, actor="claude")
        return True, "written"
    except Exception as e:
        return False, f"failed ({type(e).__name__}): {e}"


def _write_commits_provisional(repos: list, force: bool = False) -> Tuple[bool, str]:
    """Morning provisional Commits Today tally. wrap_up refreshes at EOD."""
    status = daily_note.section_status()
    if status.get("commits_today") == "filled" and not force:
        return True, "skipped (already filled)"
    try:
        paths = [r["path"] for r in repos if r.get("path")]
        summary = commit_summary.summarize_today(paths)
        md = commit_summary.format_markdown(summary)
        ts = datetime.now().strftime("%H:%M")
        md += f"\n\n*Provisional tally at {ts} — wrap-up refreshes this at EOD.*"
        daily_note.write_section("commits_today", md, actor="claude")
        n = summary.get("total_commits", 0)
        return True, f"written ({n} commit(s) so far)"
    except Exception as e:
        return False, f"failed ({type(e).__name__}): {e}"


def _write_research_feed(a: dict, force: bool = False) -> Tuple[bool, str]:
    """Write research feed section."""
    status = daily_note.section_status()
    if status.get("research_feed") == "filled" and not force:
        return True, "skipped (already filled)"
    try:
        # A degraded fetch (no network, no cache) hands us {} or a digest
        # missing 'papers'/'categories'. Formatting that raises KeyError and
        # historically left the whole section for manual fill every morning.
        # Detect it and write an honest placeholder instead of crashing.
        has_dual = "physics" in a and "ai" in a
        has_single = "papers" in a and "categories" in a
        if not (has_dual or has_single):
            md = ("*arXiv feed unavailable at spin-up "
                  f"({datetime.now().strftime('%H:%M')}) — fetch failed with no "
                  "cache to fall back on. Re-run `python3 spin_up.py --force` once "
                  "network is back, or fill manually.*")
            daily_note.write_section("research_feed", md, actor="claude")
            return True, "degraded (arxiv unavailable — placeholder written)"
        md = arxiv.format_dual_pane_md(a) if has_dual else arxiv.format_digest_md(a)
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
    quote: Optional[dict] = None,
    otd: Optional[dict] = None,
    dt_ctx: Optional[dict] = None,
    force: bool = False,
) -> Tuple[bool, str]:
    """Write daily reading (horoscope + WAFT overlay + quote + on-this-day).
    Injects section if absent."""
    import re
    if horo is None and waft_data is None and quote is None and otd is None:
        return False, "skipped (no data)"

    status = daily_note.section_status()
    if status.get("daily_reading") == "filled" and not force:
        return True, "skipped (already filled)"

    blocks = [horoscope.format_md(horo or {}, waft_data, moon_phase=(dt_ctx or {}).get("moon_phase"))]
    if quote:
        blocks.append(daily_quote.format_md(quote))
    if otd:
        blocks.append(on_this_day.format_md(otd))
    md = "\n\n".join(b for b in blocks if b.strip())

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

    atomic_io.vault_write(path, new_text)
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
        # Join only the parts that exist — an empty caption used to render
        # as "* · [Source](…)*" with a dangling separator.
        parts = [p for p in (
            caption[:160] if caption else "",
            f"[Source]({source_url})" if source_url else "",
            source or "",
        ) if p]
        caption_line = f"\n*{' · '.join(parts)}*"

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

    atomic_io.vault_write(path, new_text)
    return True, "written"


# ── Vault scaffold ────────────────────────────────────────────────────────

def _get_datetime_context() -> dict:
    """Call ~/.claude/tools/datetime_info.py --json for moon phase + week context.
    Returns {} on failure so callers degrade gracefully."""
    import subprocess
    tool = TOOLS_DIR / "datetime_info.py"
    if not tool.exists():
        return {}
    try:
        result = subprocess.run(
            [sys.executable, str(tool), "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def _find_prev_hub(date_str: str) -> Optional[str]:
    """Return the date string of the most recent hub/wagonwheel before date_str.
    Checks Hubs/ (new format) first, then Wagonwheels/ as fallback for older sessions."""
    candidates = []
    for glob_dir, pattern in [
        (VAULT_DIR / "Hubs",        "[0-9][0-9][0-9][0-9]-*_hub.md"),
        (VAULT_DIR / "Wagonwheels", "[0-9][0-9][0-9][0-9]-*_wagonwheel.md"),
    ]:
        if glob_dir.exists():
            for f in glob_dir.glob(pattern):
                file_date = f.stem[:10]
                if file_date < date_str:
                    candidates.append(file_date)
    return max(candidates) if candidates else None


def _scaffold_journal(date_str: str, today_label: str, dt_ctx: dict,
                      force: bool = False) -> Tuple[bool, str]:
    """Create Claude Journal entry for today if missing.

    NEVER overwrites — `force` is accepted for signature compat but ignored.
    --force means "re-pull data and refresh generated sections", not "destroy
    the day's journal". (A --force rerun on 2026-07-18 recreated the journal
    over the live one; only timestamps were lost that day, by luck.)
    """
    journal_dir = VAULT_DIR / "Claude Journal"
    journal_path = journal_dir / f"{date_str}.md"
    if journal_path.exists():
        return True, "skipped (already exists)"
    journal_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H:%M UTC")
    moon = dt_ctx.get("moon_phase", "")
    week = dt_ctx.get("week_number", "")
    doy = dt_ctx.get("day_of_year", "")
    context_line = ""
    if moon or week:
        parts = []
        if doy and week:
            parts.append(f"Day {doy} · Week {week}")
        if moon:
            parts.append(moon)
        context_line = f"\n*{' · '.join(parts)}*"
    content = f"""---
type: claude_journal
date: {date_str}
parent: "[[Daily Notes/{date_str}]]"
tags:
  - journal
  - daily
---

# Claude's Journal — {today_label}

## Session Log

**Session start:** {ts}{context_line}

---

## Notes

"""
    atomic_io.vault_write(journal_path, content)
    return True, "created"


def _scaffold_hub(date_str: str, today_label: str, dt_ctx: dict,
                  force: bool = False) -> Tuple[bool, str]:
    """Create Hub file for today if missing. Wires axle to previous hub.

    NEVER overwrites — `force` is accepted for signature compat but ignored.
    The hub accumulates session state all day; recreating it from template
    on a --force rerun would erase the continuation brief.
    """
    hubs_dir = VAULT_DIR / "Hubs"
    hub_path = hubs_dir / f"{date_str}_hub.md"
    if hub_path.exists():
        return True, "skipped (already exists)"
    hubs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    time_str = datetime.now().strftime("%H:%M")
    prev_date = _find_prev_hub(date_str)
    axle_val = f'"[[Hubs/{prev_date}_hub]]"' if prev_date else "null"
    moon = dt_ctx.get("moon_phase", "")
    week = dt_ctx.get("week_number", "")
    doy = dt_ctx.get("day_of_year", "")
    context_badge = ""
    if doy or moon:
        parts = []
        if doy and week:
            parts.append(f"Day {doy} · Week {week}")
        if moon:
            parts.append(moon)
        context_badge = f"\n*{' · '.join(parts)}*"
    content = f"""---
type: hub
date: {date_str}
parent: "[[Daily Notes/{date_str}]]"
axle: {axle_val}
session_id: null
last_spun_at: {ts}
hub_count: 1
spokes: []
tags:
  - hub
  - daily-hub
---

# Hub — {today_label}{context_badge}

> Read this if you are a fresh Claude chat or a human resuming this session.
> Goal: be productive within 5 minutes.

---

## Where We Are

(To be populated as session progresses)

---

## Active Threads (Spokes)

(To be populated as work begins)

---

## Decisions Log

(none yet)

---

## Open Questions

(none yet)

---

## State Snapshot

- **Vault dirty:** (check on startup)
- **Code dirty:** (check on startup)
- **Tests:** (pending)
- **Last commit:** (pending)

---

## How to Resume

(To be populated at end of session)

---

## Mode & Preferences

- **Caveman mode:** full
- **Model:** (current model)
- **Custom rules:** (none new this session)

---

## Spin History (this day)

| # | Time | Note |
|---|------|------|
| 1 | {time_str} | Initial hub created by spin-up |
"""
    atomic_io.vault_write(hub_path, content)
    return True, f"created (axle→{prev_date or 'none'})"


def _scaffold_idea_dump(date_str: str, today_label: str) -> Tuple[bool, str]:
    """Create the Idea Dump doc the daily note links to. Never overwrites."""
    path = VAULT_DIR / f"{date_str}_Idea_Dump.md"
    if path.exists():
        return True, "skipped (already exists)"
    content = f"""---
type: idea_dump
date: {date_str}
parent: "[[Daily Notes/{date_str}]]"
tags:
  - ideas
---

# Idea Dump — {today_label}

> Quick capture space. One idea per line. Nothing too small.
> Promote keepers to the plan or a work effort at wrap-up.

---

-
"""
    atomic_io.vault_write(path, content)
    return True, "created"


def _append_spin_up_log_entry(phase2: dict) -> Tuple[bool, str]:
    """Log the spin-up into the note's session log — once per day, not per run."""
    try:
        existing = daily_note.read_section("claude_session_log")
        if "Morning spin-up" in existing:
            return True, "skipped (already logged today)"
        written = sorted(
            k for k, v in phase2.items()
            if v.get("ok") and "written" in str(v.get("status", ""))
        )
        daily_note.append_session_log(
            focus="Morning spin-up — note populated",
            changes=[f"filled: {', '.join(written)}" if written
                     else "all sections already current"],
        )
        return True, "written"
    except Exception as e:
        return False, f"failed ({type(e).__name__}): {e}"


# ── Run transcript ─────────────────────────────────────────────────────────

def _serialize_paths(obj):
    """Recursively convert Path objects to strings for JSON serialization."""
    if isinstance(obj, Path):
        return str(obj)
    elif isinstance(obj, dict):
        return {k: _serialize_paths(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_serialize_paths(item) for item in obj]
    return obj


def _write_run_transcript(results: dict):
    """Write run results to .spin_up/runs/<timestamp>.json."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{ts}.json"
    serialized = _serialize_paths(results)
    path.write_text(json.dumps(serialized, indent=2))
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
    _tap("phase 1 · gathering state" + (" (force)" if args.force else ""))
    phase1 = {}

    dt_ctx = _get_datetime_context()

    print("  • weather...", end=" ", flush=True)
    w, ws = _fetch_weather(args.force)
    phase1["weather"] = {"status": ws}
    print(ws)
    _tap_status("weather", ws)

    print("  • news...", end=" ", flush=True)
    n, ns = _fetch_news(args.force)
    phase1["news"] = {"status": ns}
    print(ns)
    _tap_status("news", ns)

    print("  • arxiv...", end=" ", flush=True)
    a, aas = _fetch_arxiv(args.force)
    phase1["arxiv"] = {"status": aas}
    print(aas)
    _tap_status("arxiv", aas)

    print("  • music...", end=" ", flush=True)
    m, ms = _fetch_music(args.force)
    phase1["music"] = {"status": ms}
    print(ms)
    _tap_status("music", f"{ms} · {m.get('title', '')[:50]}" if m else ms)

    print("  • git scan...", end=" ", flush=True)
    repos, gs = _fetch_git_scan()
    phase1["git"] = {"status": gs}
    print(gs)
    _tap_status("git scan", gs)

    print("  • waft state...", end=" ", flush=True)
    waft_data, wws = _fetch_waft_state()
    phase1["waft"] = {"status": wws}
    print(wws)
    _tap_status("waft", wws)

    print("  • we_factory (quests)...", end=" ", flush=True)
    try:
        import we_factory
        pending = [q for q in (waft_data or {}).get("quests", []) if not q.get("complete")][:3]
        wef_results = we_factory.create_for_quests(pending)
        created = sum(1 for r in wef_results if r["status"] == "created")
        existed = sum(1 for r in wef_results if r["status"] == "exists")
        skipped = sum(1 for r in wef_results if r["status"] == "skipped_not_worthy")
        phase1["we_factory"] = {
            "status": f"ok ({created} new, {existed} existed, {skipped} not-worthy)",
            "results": wef_results,
        }
        print(f"ok ({created} new, {existed} existing, {skipped} not-worthy)")
    except Exception as e:
        phase1["we_factory"] = {"status": f"failed: {e}"}
        print(f"failed: {e}")

    print("  • horoscope...", end=" ", flush=True)
    horo, hs = _fetch_horoscope(args.force)
    phase1["horoscope"] = {"status": hs}
    print(hs)
    _tap_status("horoscope", hs)

    print("  • daily image...", end=" ", flush=True)
    img, imgs = _fetch_daily_image(args.force)
    phase1["daily_image"] = {"status": imgs}
    print(imgs)
    _tap_status("daily image", imgs)

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
    _tap_status("image quip", "ok" if _quip else "empty — placeholder used")

    print("  • air quality...", end=" ", flush=True)
    aq, aqs = _fetch_air_quality(args.force)
    phase1["air_quality"] = {"status": aqs}
    print(aqs)
    _tap_status("air quality", aqs)

    print("  • on this day...", end=" ", flush=True)
    otd, otds = _fetch_on_this_day(args.force)
    phase1["on_this_day"] = {"status": otds}
    print(otds)
    _tap_status("on this day", otds)

    print("  • quote of the day...", end=" ", flush=True)
    quote, qs = _fetch_quote(args.force)
    phase1["daily_quote"] = {"status": qs}
    print(qs)
    _tap_status("quote", qs)

    results["phases"]["gather"] = phase1

    # Archive every gathered payload into the vault — append-only, one new
    # timestamped file per run, rides the vault's private GitHub repo. Even
    # if every fill below fails, today's pulled data is already saved.
    print("  • data archive...", end=" ", flush=True)
    if args.dry_run:
        print("skipped (dry-run)")
    else:
        try:
            archive_path = data_archive.archive({
                "weather": w, "news": n, "arxiv": a, "music": m,
                "git_repos": repos, "waft": waft_data, "horoscope": horo,
                "daily_image": img, "image_quip": _quip, "air_quality": aq,
                "on_this_day": otd, "daily_quote": quote,
                "fetch_statuses": {k: v.get("status") for k, v in phase1.items()},
            })
            results["archive_path"] = str(archive_path)
            print(f"saved ({archive_path.name})")
            _tap(f"data archived · {archive_path.name}")
        except Exception as e:
            results["archive_path"] = None
            print(f"failed ({type(e).__name__}): {e}")
            _tap(f"data archive failed: {e}", "error")

    # Phase 2: Fill note
    print("\n✍️  Phase 2: Filling daily note...")
    _tap("phase 2 · filling daily note")
    phase2 = {}

    if not daily_note.exists():
        if args.dry_run:
            print("  • note missing — would create from template (dry-run)")
            phase2["note_created"] = {"ok": True, "status": "dry-run"}
        else:
            print("  • note missing — creating from template...", end=" ", flush=True)
            try:
                daily_note.create_from_template()
                phase2["note_created"] = {"ok": True, "status": "created"}
                print("created")
            except Exception as e:
                phase2["note_created"] = {"ok": False, "status": f"failed: {e}"}
                print(f"failed: {e}")
                results["phases"]["fill"] = phase2
                print("\n❌ Cannot fill sections without a daily note. Aborting.")
                return 1

    if not args.dry_run:
        print("  • hero_image...", end=" ", flush=True)
        ok, status = _write_hero_image(img, quip=_quip, force=args.force)
        phase2["hero_image"] = {"ok": ok, "status": status}
        print(status)
        _tap_status("§ hero_image", status)

        print("  • daily_reading...", end=" ", flush=True)
        ok, status = _write_daily_reading(horo, waft_data, quote=quote,
                                          otd=otd, dt_ctx=dt_ctx, force=args.force)
        phase2["daily_reading"] = {"ok": ok, "status": status}
        print(status)
        _tap_status("§ daily_reading", status)

        print("  • location...", end=" ", flush=True)
        ok, status = _write_location(w, n, aq=aq, force=args.force)
        phase2["location"] = {"ok": ok, "status": status}
        print(status)
        _tap_status("§ location", status)

        print("  • research_feed...", end=" ", flush=True)
        ok, status = _write_research_feed(a, args.force)
        phase2["research_feed"] = {"ok": ok, "status": status}
        print(status)
        _tap_status("§ research_feed", status)

        print("  • in_the_lab...", end=" ", flush=True)
        ok, status = _write_in_the_lab(repos, args.force)
        phase2["in_the_lab"] = {"ok": ok, "status": status}
        print(status)
        _tap_status("§ in_the_lab", status)

        print("  • commits_today...", end=" ", flush=True)
        ok, status = _write_commits_provisional(repos, args.force)
        phase2["commits_today"] = {"ok": ok, "status": status}
        print(status)
        _tap_status("§ commits_today", status)

        print("  • work_efforts...", end=" ", flush=True)
        ok, status = _write_work_efforts(repos, args.force)
        phase2["work_efforts"] = {"ok": ok, "status": status}
        print(status)
        _tap_status("§ work_efforts", status)

        print("  • waft_workspace...", end=" ", flush=True)
        ok, status = _write_waft_workspace(waft_data, args.force)
        phase2["waft_workspace"] = {"ok": ok, "status": status}
        print(status)
        _tap_status("§ waft_workspace", status)

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

    # Phase 3: Daily plan (create if missing, seed from rollover)
    if args.dry_run:
        print("\n📅 Phase 3: Daily plan (dry-run — skipping writes)")
    if not args.dry_run:
        print("\n📅 Phase 3: Daily plan...")
        try:
            import plan_rollover
            plan_result = plan_rollover.create_today_from_rollover(days=7)
            if plan_result.get("created"):
                seeds = plan_result.get("seed_count", 0)
                print(f"  ✓ plan created — {seeds} items rolled over")
                _tap(f"phase 3 · plan created, {seeds} items rolled over")
            else:
                print(f"  · plan already exists ({plan_result.get('status', 'ok')})")
                _tap("phase 3 · plan already exists")
        except Exception as e:
            print(f"  · daily plan skipped ({type(e).__name__}): {e}")
            _tap(f"phase 3 · daily plan failed: {e}", "error")

    # Phase 4: Frontmatter sync
    if args.dry_run:
        print("\n📎 Phase 4: Frontmatter sync (dry-run — skipping writes)")
    if not args.dry_run:
        print("\n📎 Phase 4: Frontmatter sync...")
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

    # Phase 5: Scaffold missing vault files
    if args.dry_run:
        print("\n📁 Phase 5: Vault scaffold (dry-run — skipping writes)")
    if not args.dry_run:
        print("\n📁 Phase 5: Vault scaffold...")
        date_str = datetime.now().strftime("%Y-%m-%d")
        today_label = datetime.now().strftime("%A, %B %-d")

        print("  • claude journal...", end=" ", flush=True)
        ok, status = _scaffold_journal(date_str, today_label, dt_ctx, args.force)
        print(status)

        print("  • hub...", end=" ", flush=True)
        ok, status = _scaffold_hub(date_str, today_label, dt_ctx, args.force)
        hub_status = status
        print(status)

        print("  • idea dump...", end=" ", flush=True)
        ok, status = _scaffold_idea_dump(date_str, today_label)
        print(status)

        # Wire continuity links into daily note frontmatter. The template now
        # ships these too, but this is the CODE BACKSTOP: a note created any
        # other way (legacy, headless, hand-made) still gets a guaranteed hub
        # link. A daily note with no hub is an unreachable continuation brief —
        # the 2026-07-10 blind spot. Every link:True field is enforced here.
        try:
            import frontmatter as fm
            links = {
                "hub":        f"[[Hubs/{date_str}_hub]]",
                "wagonwheel": f"[[Hubs/{date_str}_hub]]",
                "journal":    f"[[Claude Journal/{date_str}]]",
                "plan":       f"[[Plans/{date_str}_daily_plan]]",
            }
            for field, wikilink in links.items():
                current = fm.get_field(field)
                # Set if empty OR if present but not a proper wikilink (older
                # notes stored bare paths like "Plans/…" that don't render).
                if not current or "[[" not in str(current):
                    fm.set_field(field, wikilink)

            # Fail-loud: validate the wheel links right after wiring them.
            issues = fm.validate(date_str)
            if issues:
                print("  ⚠ frontmatter issues after wiring:")
                for iss in issues:
                    print(f"      · {iss}")
            else:
                print("  ✓ continuity links wired + validated")
        except Exception as e:
            print(f"  · frontmatter link skipped: {e}")

    # Phase 6: Sitrep generation from hub + plan + journal
    if args.dry_run:
        print("\n📋 Phase 6: Sitrep generation (dry-run — skipping write)")
    if not args.dry_run:
        print("\n📋 Phase 6: Sitrep generation...")
        try:
            import sitrep_gen
            date_str_6 = datetime.now().strftime("%Y-%m-%d")
            weather_str = (
                f"{w.get('current_temp_f', '?')}°F, "
                f"{weather.describe(w.get('weather_code', 0))[0]}"
                if w else ""
            )
            # Stats computed here (not phase 1) so plan/journal scaffolds from
            # phases 3+5 register as "ready" in the dashboard line.
            try:
                stats_line = vault_stats.format_md(vault_stats.compute())
            except Exception:
                stats_line = ""
            ok, status = sitrep_gen.write_to_note(
                date_str_6, weather_str=weather_str, music=m,
                stats_line=stats_line, force=args.force,
            )
            phase2["sitrep"] = {"ok": ok, "status": status}
            print(f"  {'✓' if ok else '·'} sitrep: {status}")
        except Exception as e:
            print(f"  · sitrep skipped ({type(e).__name__}): {e}")

    # Phase 7: Session log entry — the note records its own spin-up
    if args.dry_run:
        print("\n📝 Phase 7: Session log entry (dry-run — skipping write)")
    if not args.dry_run:
        print("\n📝 Phase 7: Session log entry...")
        ok, status = _append_spin_up_log_entry(phase2)
        print(f"  {'✓' if ok else '·'} session log: {status}")

    # Report
    _fails = [k for k, v in phase2.items() if not v.get("ok", True)]
    if _fails:
        _tap(f"spin-up finished with failures: {', '.join(_fails)}", "error")
    else:
        _tap(f"✅ spin-up complete · {w.get('current_temp_f', '?')}°F · "
             f"{n.get('count', 0)} news · {len(repos)} repos scanned")
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
    if aq:
        print(f"💨 Air: AQI {aq['us_aqi']:.0f} ({air_quality.describe(aq['us_aqi'])[0]})")
    if results.get("archive_path"):
        print(f"🗄  Data archived: {results['archive_path']}")
    print("=" * 60)

    return 0 if all(
        r.get("ok", True) for r in list(phase2.values())
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
