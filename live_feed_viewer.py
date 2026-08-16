"""
live_feed_viewer.py — turn the Live Feed JSONL archive into a browsable page.

The daily note shows the last 14 rows of the Live Feed. The JSONL underneath it
keeps everything, one file per day, forever:

    System/40-49_telemetry/live_feed/YYYY-MM/YYYY-MM-DD.jsonl

That archive is the real record of what the agents did, and until now nothing
read it back. This module does: it loads every day on disk, derives the parts
the raw rows only imply (which project a row touched, which tool family it
belongs to, which session tab it came from), and emits ONE self-contained HTML
file with no external requests.

Design notes worth keeping:

  * The page embeds its data. No fetch(), so it works from file:// with the
    network off, and it can be copied anywhere as a single artifact.

  * Rows are stored as arrays, not objects, and the kind/session/project
    strings are interned into lookup tables. On 12k rows that is the
    difference between a ~6 MB page and a ~2 MB one.

  * Charts follow the validated categorical palette (5 slots, checked for
    colorblind separation in both light and dark). Tool families were chosen
    to fit inside 5 slots for exactly that reason — an 11-colour chart would
    not have survived the check.

Two modes:

  SERVE (default)  A stdlib HTTP server on localhost. The page polls today's
                   feed file by BYTE OFFSET, so each tick ships only the rows
                   appended since the last one — a few hundred bytes, not the
                   whole day. New rows stream into the open page.

                   A server is required, not a preference. No browser can
                   watch a file on disk: `fetch()` is blocked outright on
                   file:// origins, and the File System Access API is
                   Chromium-only, needs a fresh grant click per session, and
                   still polls underneath. A local server is the portable
                   route and costs nothing here.

  BUILD (--build)  One self-contained HTML file, data baked in, no requests.
                   Frozen at build time, but it opens anywhere and survives
                   being emailed to someone.

CLI:
    python3 live_feed_viewer.py                 serve live at localhost, open browser
    python3 live_feed_viewer.py --port 9000     serve on another port
    python3 live_feed_viewer.py --build         write a frozen HTML file instead
    python3 live_feed_viewer.py --out PATH      where --build writes
    python3 live_feed_viewer.py --days 30       cap how many days are loaded
    python3 live_feed_viewer.py --no-open       never launch a browser
    python3 live_feed_viewer.py --json          print a summary as JSON
"""

import json
import re
import sys
import threading
import webbrowser
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import shell_audit

try:
    import daily_note
    VAULT_DIR = daily_note.VAULT_DIR
except Exception:                                    # standalone / no vault
    VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"

FEED_ROOT = VAULT_DIR / "System" / "40-49_telemetry" / "live_feed"
DEFAULT_OUT = Path(__file__).resolve().parent / "build" / "live_feed_archive.html"

# Per-row detail cap. Long Bash one-liners run to several KB; past this they
# stop being scannable and start being page weight.
DETAIL_CHARS = 420

# ── Tool families ──────────────────────────────────────────────────────
#
# Five families, because the validated categorical palette seats five slots
# safely. Order here IS the colour-slot order, so it is not cosmetic.

FAMILIES = [
    ("shell", "Shell", ["Bash", "BashOutput", "KillShell"]),
    ("write", "Write", ["Edit", "Write", "MultiEdit", "NotebookEdit"]),
    ("read",  "Read",  ["Read", "Grep", "Glob", "NotebookRead"]),
    ("web",   "Web",   ["WebFetch", "WebSearch"]),
    ("plan",  "Plan",  ["TodoWrite", "Task", "Agent", "note", "Skill"]),
]
FAMILY_OF = {kind: key for key, _, kinds in FAMILIES for kind in kinds}
FAMILY_KEYS = [key for key, _, _ in FAMILIES]


def family_of(kind: str) -> str:
    """Map a tool name to its family. Unknown tools land in `plan`, which is
    the catch-all rather than a sixth colour."""
    return FAMILY_OF.get(kind, "plan")


# ── Project attribution ────────────────────────────────────────────────

HOME = str(Path.home())

_PATH_RE = re.compile(
    r'(?:' + re.escape(HOME) + r'|~)/(?:Code|Documents|Desktop|Downloads)/([^\s/"\'`;|&)]+)'
)
_CD_RE = re.compile(r'\bcd\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))')

# Directories that are containers, not projects. When a path lands in one,
# take the next segment down instead.
_PASSTHROUGH = {"active", "_experiments", "archived", "_hidden", "_references",
                "experiments", "snapshots", "_work_efforts", "_docs"}


def _project_from_path(raw: str) -> Optional[str]:
    """Best-effort 'which project is this' from an absolute path."""
    if not raw:
        return None
    p = raw.replace("~", HOME, 1) if raw.startswith("~") else raw

    if "/Personal-Remote-Vault" in p:
        return "vault"
    if "/scratchpad" in p or "/claude-501/" in p:
        return "scratchpad"
    if p.startswith(HOME + "/.claude") or "/.claude/" in p:
        return "claude-config"

    for root in ("/Code/", "/Documents/", "/Desktop/"):
        idx = p.find(HOME + root)
        if idx == -1:
            continue
        rest = p[idx + len(HOME) + len(root):].split("/")
        rest = [seg for seg in rest if seg]
        if not rest:
            continue
        name = rest[0]
        if name in _PASSTHROUGH and len(rest) > 1:
            name = rest[1]
        # A bare file at the root of ~/Code is not a project.
        if "." in name and len(rest) == 1:
            return None
        return name
    return None


def project_of(kind: str, detail: str) -> Optional[str]:
    """Attribute one row to a project.

    File-taking tools carry the path directly. Bash carries it inside the
    command, usually as a leading `cd`, sometimes anywhere in the line.
    """
    if not detail:
        return None
    if kind in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read",
                "NotebookRead", "Glob", "Grep"):
        return _project_from_path(detail)

    if kind == "Bash":
        m = _CD_RE.search(detail)
        if m:
            found = _project_from_path(m.group(1) or m.group(2) or m.group(3) or "")
            if found:
                return found
        m = _PATH_RE.search(detail)
        if m:
            return _project_from_path(m.group(0))
    return None


# ── Load ───────────────────────────────────────────────────────────────

def feed_files(limit_days: Optional[int] = None) -> list:
    """Every day file on disk, oldest first."""
    if not FEED_ROOT.is_dir():
        return []
    files = sorted(FEED_ROOT.glob("*/*.jsonl"))
    files = [f for f in files if re.fullmatch(r'\d{4}-\d{2}-\d{2}', f.stem)]
    if limit_days:
        files = files[-limit_days:]
    return files


def _focus_for(date: str) -> Optional[dict]:
    path = FEED_ROOT / date[:7] / f"{date}.state.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("focus") else None
    except (OSError, json.JSONDecodeError):
        return None


def load(limit_days: Optional[int] = None) -> dict:
    """Read the whole archive into the compact shape the page expects.

    Returns interned tables plus one row array per day:
        row = [seconds_since_midnight, kind_idx, session_idx, project_idx, detail]
    """
    kinds, sessions, projects = [], [], []
    kind_ix, sess_ix, proj_ix = {}, {}, {}

    def intern(value, table, index):
        if value not in index:
            index[value] = len(table)
            table.append(value)
        return index[value]

    days = []
    for path in feed_files(limit_days):
        date = path.stem
        try:
            raw = path.read_bytes()
        except OSError:
            continue

        rows, consumed, bash = [], 0, []
        for secs, kind, sid, proj, detail, used in _parse(raw):
            consumed = used
            if kind == "Bash":
                bash.append(detail)
            rows.append([
                secs,
                intern(kind, kinds, kind_ix),
                intern(sid, sessions, sess_ix),
                intern(proj, projects, proj_ix),
                _display(detail),
            ])

        if rows:
            # `size` is the resume point for tail polling: bytes of COMPLETE
            # lines, so a half-written final line is re-read next tick rather
            # than silently dropped.
            days.append({"date": date, "rows": rows, "size": consumed,
                         "shell": shell_audit.analyze(bash),
                         "focus": _focus_for(date)})

    return {
        "kinds": kinds,
        "families": [family_of(k) for k in kinds],
        "sessions": sessions,
        "projects": projects,
        "familyOrder": FAMILY_KEYS,
        "familyLabels": {key: label for key, label, _ in FAMILIES},
        "familyMap": FAMILY_OF,          # so the client can classify new kinds
        "shellGroups": shell_audit.GROUP_ORDER,
        "shellLabels": shell_audit.GROUP_LABELS,
        "shellCovered": shell_audit.COVERED,
        "days": days,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "feedRoot": str(FEED_ROOT).replace(HOME, "~"),
        "today": datetime.now().strftime("%Y-%m-%d"),
        "live": False,
    }


def _display(detail: str) -> str:
    """Collapse to one line and cap for the feed list. Long Bash one-liners run
    to several KB; past the cap they stop being scannable and start being page
    weight."""
    detail = " ".join(detail.split())
    return detail[:DETAIL_CHARS - 1] + "…" if len(detail) > DETAIL_CHARS else detail


def _parse(raw: bytes):
    """Yield (secs, kind, sid, project, detail, bytes_consumed) per complete line.

    Consumed counts only whole, newline-terminated lines. The hook appends with
    a single write, but a poll can still land mid-write on a big Bash command,
    and a truncated JSON line must not advance the offset past itself.
    """
    consumed = 0
    for chunk in raw.split(b"\n")[:-1]:
        consumed += len(chunk) + 1
        line = chunk.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or not row.get("kind"):
            continue

        kind = str(row.get("kind"))
        # Full detail, NOT truncated. Display truncation happens at the call
        # site: shell_audit needs the whole command, and the long ones are
        # precisely the compound ones its whole analysis is about.
        detail = str(row.get("detail") or "")

        parts = str(row.get("ts") or "").split(":")
        try:
            secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, IndexError):
            secs = 0

        sid = str(row.get("sid") or "") or "—"
        yield secs, kind, sid, project_of(kind, detail) or "", detail, consumed


def tail(date: str, offset: int) -> dict:
    """Rows appended to `date`'s feed since byte `offset`.

    Strings come back raw rather than interned; the client folds them into its
    own lookup tables. Interning server-side would mean holding per-client
    state, which is not worth it for a page one person has open.
    """
    path = FEED_ROOT / date[:7] / f"{date}.jsonl"
    if not path.is_file():
        return {"date": date, "offset": 0, "rows": [], "focus": _focus_for(date)}

    size = path.stat().st_size
    if offset > size:            # file rotated or truncated: resync from zero
        offset = 0
    if offset == size:
        return {"date": date, "offset": size, "rows": [], "focus": _focus_for(date)}

    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            raw = fh.read()
    except OSError:
        return {"date": date, "offset": offset, "rows": [], "focus": _focus_for(date)}

    rows, consumed, bash = [], 0, []
    for secs, kind, sid, proj, detail, used in _parse(raw):
        consumed = used
        if kind == "Bash":
            bash.append(detail)
        rows.append([secs, kind, sid, proj, _display(detail)])

    # Classify only the NEW commands and let the client add the delta to its
    # running totals. Re-analysing the whole day on every 2s poll would be
    # wasteful, and shipping no delta at all would leave the shell panel
    # silently stale while every other panel moved.
    return {"date": date, "offset": offset + consumed, "rows": rows,
            "shellDelta": shell_audit.analyze(bash) if bash else None,
            "focus": _focus_for(date)}


def summarize(data: dict) -> dict:
    """Small stats block, also useful on its own via --json."""
    total = sum(len(d["rows"]) for d in data["days"])
    sessions = set()
    per_kind = Counter()
    per_project = Counter()
    for day in data["days"]:
        for secs, ki, si, pi, _ in day["rows"]:
            sessions.add((day["date"], si))
            per_kind[data["kinds"][ki]] += 1
            if data["projects"][pi]:
                per_project[data["projects"][pi]] += 1
    busiest = max(data["days"], key=lambda d: len(d["rows"])) if data["days"] else None
    return {
        "days": len(data["days"]),
        "actions": total,
        "sessions": len(sessions),
        "range": [data["days"][0]["date"], data["days"][-1]["date"]] if data["days"] else [],
        "busiest_day": busiest["date"] if busiest else None,
        "busiest_count": len(busiest["rows"]) if busiest else 0,
        "top_kinds": per_kind.most_common(5),
        "top_projects": per_project.most_common(8),
    }


# ── Render ─────────────────────────────────────────────────────────────

def render_page(live: bool = False, limit_days: Optional[int] = None) -> tuple:
    """(html, stats). `live` tells the page to poll /api/tail."""
    data = load(limit_days)
    data["live"] = live
    stats = summarize(data)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # A literal </script> inside a Bash detail would end the block early.
    payload = payload.replace("</", "<\\/")
    return TEMPLATE.replace("/*__DATA__*/null", payload), stats


def build(out: Path, limit_days: Optional[int] = None) -> dict:
    html, stats = render_page(live=False, limit_days=limit_days)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    stats["out"] = str(out)
    stats["bytes"] = out.stat().st_size
    return stats


# ── Serve ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    """Two routes and nothing else. Bound to loopback, so no auth layer."""

    limit_days = None
    protocol_version = "HTTP/1.1"

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)

        if url.path in ("/", "/index.html"):
            html, _ = render_page(live=True, limit_days=self.limit_days)
            self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if url.path == "/api/tail":
            qs = parse_qs(url.query)
            date = (qs.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
            # The date lands in a filesystem path, so it is validated, not trusted.
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                self._send(b'{"error":"bad date"}', "application/json")
                return
            try:
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
            except ValueError:
                offset = 0
            body = json.dumps(tail(date, offset), ensure_ascii=False)
            self._send(body.encode("utf-8"), "application/json; charset=utf-8")
            return

        self.send_error(404)

    def log_message(self, *args) -> None:
        pass                      # a poll every 2s would drown the console


def serve(port: int = 8787, limit_days: Optional[int] = None,
          open_browser: bool = True) -> int:
    _Handler.limit_days = limit_days
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        print(f"live_feed_viewer: cannot bind port {port} ({exc}). "
              f"Try --port {port + 1}.", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{port}/"
    stats = summarize(load(limit_days))
    print(f"live_feed_viewer: serving {stats['actions']} actions across "
          f"{stats['days']} days at {url}")
    print("  live tail on today's feed, polled every 2s. Ctrl-C to stop.")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nlive_feed_viewer: stopped.")
    finally:
        httpd.server_close()
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Feed Archive</title>
<!-- Inline, so the browser never issues a /favicon.ico request. A 404 in the
     console on every load is noise that trains you to ignore the console. -->
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3' fill='%231a1a19'/%3E%3Crect x='3' y='9' width='2' height='4' fill='%233987e5'/%3E%3Crect x='6' y='5' width='2' height='8' fill='%233987e5'/%3E%3Crect x='9' y='7' width='2' height='6' fill='%23d95926'/%3E%3Crect x='12' y='3' width='2' height='10' fill='%230ca30c'/%3E%3C/svg%3E">
<style>
:root {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --plane:     #0d0d0d;
  --raised:    #222221;
  --ink:       #ffffff;
  --ink-2:     #c3c2b7;
  --muted:     #898781;
  --grid:      #2c2c2a;
  --axis:      #383835;
  --hair:      rgba(255,255,255,0.10);
  --s-shell:   #3987e5;
  --s-write:   #d95926;
  --s-read:    #199e70;
  --s-web:     #c98500;
  --s-plan:    #d55181;
  --seq-lo:    #184f95;
  --seq-hi:    #86b6ef;
  --good:      #0ca30c;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", sans-serif;
}
:root[data-theme="light"] {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --plane:     #f4f3ef;
  --raised:    #ffffff;
  --ink:       #0b0b0b;
  --ink-2:     #52514e;
  --muted:     #898781;
  --grid:      #e1e0d9;
  --axis:      #c3c2b7;
  --hair:      rgba(11,11,11,0.10);
  --s-shell:   #2a78d6;
  --s-write:   #eb6834;
  --s-read:    #1baf7a;
  --s-web:     #eda100;
  --s-plan:    #e87ba4;
  /* Ordinal floor, not the sequential floor: these are discrete ordered marks
     (day bars, lane cells, project bars), so the step nearest the surface has
     to stay visible rather than recede into it. Step 250 is that floor. */
  --seq-lo:    #86b6ef;
  --seq-hi:    #184f95;
  --good:      #006300;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--plane);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 22px 80px; }

/* ── header ─────────────────────────────────────────────── */
header.top { display: flex; align-items: flex-end; justify-content: space-between;
  gap: 20px; flex-wrap: wrap; margin-bottom: 22px; }
h1 { font-size: 19px; font-weight: 700; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 12px; font-family: var(--mono); }
.sub code { color: var(--ink-2); }
.toolbtn { background: var(--raised); color: var(--ink-2); border: 1px solid var(--hair);
  border-radius: 7px; padding: 7px 12px; font-size: 12px; font-family: var(--sans);
  cursor: pointer; }
.toolbtn:hover { color: var(--ink); border-color: var(--axis); }
.headtools { display: flex; align-items: center; gap: 10px; }

/* ── live badge ─────────────────────────────────────────── */
.live { display: none; align-items: center; gap: 7px; background: var(--raised);
  border: 1px solid var(--hair); border-radius: 7px; padding: 6px 11px; font-size: 11px;
  color: var(--ink-2); font-family: var(--mono); }
.live.on { display: inline-flex; }
.live .pip { width: 7px; height: 7px; border-radius: 50%; background: var(--good);
  flex: none; animation: pulse 2s ease-in-out infinite; }
.live.stale .pip { background: var(--muted); animation: none; }
.live.hit .pip { animation: none; transform: scale(1.5); transition: transform .18s; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.28; } }
@media (prefers-reduced-motion: reduce) { .live .pip { animation: none; } }
.newflag { display: inline-block; margin-left: 8px; padding: 1px 6px; border-radius: 4px;
  background: var(--good); color: #fff; font-size: 10px; font-family: var(--mono);
  opacity: 0; transition: opacity .25s; }
.newflag.on { opacity: 1; }
.row.fresh { animation: slidein .5s ease-out; }
@keyframes slidein { from { background: rgba(12,163,12,0.16); } to { background: transparent; } }
@media (prefers-reduced-motion: reduce) { .row.fresh { animation: none; } }

/* ── kpi row ────────────────────────────────────────────── */
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr));
  gap: 12px; margin-bottom: 26px; }
.kpi { background: var(--surface-1); border: 1px solid var(--hair); border-radius: 10px;
  padding: 14px 16px; }
.kpi .label { font-size: 11px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.06em; margin-bottom: 6px; }
.kpi .value { font-size: 30px; font-weight: 700; line-height: 1.05; letter-spacing: -0.02em; }
.kpi .value.sm { font-size: 19px; }
.kpi .foot { font-size: 11px; color: var(--ink-2); margin-top: 5px; font-family: var(--mono); }

/* ── panels ─────────────────────────────────────────────── */
.panel { background: var(--surface-1); border: 1px solid var(--hair); border-radius: 10px;
  padding: 16px 18px 14px; margin-bottom: 16px; }
.panel h2 { font-size: 12px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.07em; color: var(--ink-2); margin: 0 0 2px; }
.panel .hint { font-size: 11px; color: var(--muted); margin: 0 0 14px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 860px) { .grid2 { grid-template-columns: 1fr; } }

/* ── legend ─────────────────────────────────────────────── */
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 0 0 12px; }
.legend .item { display: flex; align-items: center; gap: 6px; font-size: 11px;
  color: var(--ink-2); cursor: pointer; user-select: none; }
.legend .item.off { opacity: 0.34; }
.legend .swatch { width: 11px; height: 11px; border-radius: 3px; flex: none; }
.legend .n { font-family: var(--mono); color: var(--muted); font-variant-numeric: tabular-nums; }

/* ── charts ─────────────────────────────────────────────── */
.chartbox { position: relative; }
svg { display: block; width: 100%; overflow: visible; }
svg text { font-family: var(--sans); }
.tick { font-size: 10px; fill: var(--muted); font-variant-numeric: tabular-nums; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.baseline { stroke: var(--axis); stroke-width: 1; }
.dlabel { font-size: 10px; fill: var(--ink-2); font-variant-numeric: tabular-nums; }

/* ── day strip ──────────────────────────────────────────── */
.daybar { cursor: pointer; }
.daybar rect.hit { fill: transparent; }
.daybar:hover rect.mark { opacity: 0.82; }
.daybar text.dl { font-size: 10px; fill: var(--muted); }
.daybar.sel text.dl { fill: var(--ink); font-weight: 600; }

/* ── tooltip ────────────────────────────────────────────── */
.tip { position: absolute; pointer-events: none; z-index: 20; background: var(--raised);
  border: 1px solid var(--axis); border-radius: 8px; padding: 8px 10px; font-size: 11px;
  color: var(--ink); box-shadow: 0 6px 22px rgba(0,0,0,0.34); opacity: 0;
  transition: opacity .09s; min-width: 128px; }
.tip.on { opacity: 1; }
.tip .th { font-weight: 600; margin-bottom: 5px; font-family: var(--mono); }
.tip .tr { display: flex; align-items: center; gap: 6px; justify-content: space-between; }
.tip .tr .l { display: flex; align-items: center; gap: 6px; color: var(--ink-2); }
.tip .tr .v { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.tip .sw { width: 9px; height: 9px; border-radius: 2px; flex: none; }

/* ── filters ────────────────────────────────────────────── */
.filters { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
input[type=search] { flex: 1 1 240px; min-width: 190px; background: var(--plane);
  border: 1px solid var(--hair); border-radius: 7px; padding: 8px 11px; color: var(--ink);
  font-family: var(--mono); font-size: 12px; }
input[type=search]:focus { outline: none; border-color: var(--s-shell); }
select { background: var(--plane); border: 1px solid var(--hair); border-radius: 7px;
  padding: 8px 10px; color: var(--ink-2); font-size: 12px; font-family: var(--sans); }
.count { font-size: 11px; color: var(--muted); font-family: var(--mono);
  font-variant-numeric: tabular-nums; margin-left: auto; }

/* ── feed table ─────────────────────────────────────────── */
.feed { max-height: 560px; overflow-y: auto; border-top: 1px solid var(--hair);
  font-family: var(--mono); font-size: 12px; }
.row { display: grid; grid-template-columns: 62px 74px 46px 1fr; gap: 10px;
  padding: 5px 4px; border-bottom: 1px solid var(--grid); align-items: baseline; }
.row:hover { background: var(--raised); }
.row .t { color: var(--muted); font-variant-numeric: tabular-nums; }
.row .k { display: flex; align-items: center; gap: 5px; color: var(--ink-2); }
.row .k .dot { width: 7px; height: 7px; border-radius: 2px; flex: none; }
.row .s { color: var(--muted); font-size: 11px; }
.row .d { color: var(--ink); overflow-wrap: anywhere; white-space: pre-wrap; }
.row .d .proj { color: var(--muted); }
.row.note .d { color: var(--s-plan); }
.empty { padding: 26px 4px; color: var(--muted); font-family: var(--sans); }
mark { background: rgba(233,161,0,0.28); color: inherit; border-radius: 2px; }
:root[data-theme="light"] mark { background: rgba(237,161,0,0.35); }

/* ── lanes / bars ───────────────────────────────────────── */
.lanelabel { font-size: 11px; fill: var(--ink-2); font-family: var(--mono); }
.barlabel { font-size: 11px; fill: var(--ink-2); }
.barval { font-size: 11px; fill: var(--ink-2); font-variant-numeric: tabular-nums; }

.marks { display: flex; flex-direction: column; gap: 7px; }
.marks .empty { padding: 2px 0 6px; margin: 0; }

/* ── shell breakdown ────────────────────────────────────── */
.panel .hint.tight { margin-bottom: 10px; }
.minitable { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 11px; }
.minitable th { text-align: left; font-weight: 500; color: var(--muted); padding: 0 0 5px;
  border-bottom: 1px solid var(--grid); font-size: 10px; text-transform: uppercase;
  letter-spacing: 0.05em; }
.minitable th.num, .minitable td.num { text-align: right; font-variant-numeric: tabular-nums;
  font-family: var(--mono); }
.minitable td { padding: 4px 0; border-bottom: 1px solid var(--grid); color: var(--ink-2); }
.minitable td.name { font-family: var(--mono); color: var(--ink); }
.minitable td.cov { color: var(--muted); padding-left: 14px; }
.minitable tr:last-child td { border-bottom: none; }
/* Explicit widths: without them "share" and "covered by" ran together into a
   single unreadable "SHARECOVERED BY" header. */
.minitable th:nth-child(2), .minitable td:nth-child(2) { width: 62px; }
.minitable th:nth-child(3), .minitable td:nth-child(3) { width: 58px; }
.minitable th:nth-child(4), .minitable td:nth-child(4) { width: 104px; padding-left: 14px; }
.legend .item.static { cursor: default; }
.gdot { display: inline-block; width: 7px; height: 7px; border-radius: 2px; margin-right: 6px; }
.mark-item { display: flex; gap: 10px; font-size: 12px; align-items: baseline; }
.mark-item .mt { font-family: var(--mono); color: var(--muted); flex: none; }
.mark-item.focus .mx { color: var(--good); font-weight: 600; }
.mark-item .mx { color: var(--ink); }
footer.note { color: var(--muted); font-size: 11px; margin-top: 24px; text-align: center;
  font-family: var(--mono); }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>Live Feed Archive</h1>
      <div class="sub" id="subline"></div>
    </div>
    <div class="headtools">
      <span class="live" id="liveBadge"><span class="pip"></span><span id="liveText">live</span></span>
      <button class="toolbtn" id="themeBtn" type="button">Light mode</button>
    </div>
  </header>

  <div class="kpis" id="kpis"></div>

  <section class="panel">
    <h2>Days on record</h2>
    <p class="hint">Every JSONL day file on disk. Click a bar to load that day below.</p>
    <div class="chartbox"><div id="dayChart"></div></div>
  </section>

  <section class="panel">
    <h2 id="dayTitle">Activity</h2>
    <p class="hint" id="dayHint"></p>
    <div class="legend" id="famLegend"></div>
    <div class="chartbox"><div id="actChart"></div><div class="tip" id="actTip"></div></div>
  </section>

  <div class="grid2">
    <section class="panel">
      <h2>Session tabs</h2>
      <p class="hint">Each lane is one Claude Code tab. Darker means busier.</p>
      <div class="chartbox"><div id="laneChart"></div><div class="tip" id="laneTip"></div></div>
    </section>
    <section class="panel">
      <h2>Projects touched</h2>
      <p class="hint">Attributed from file paths and <code>cd</code> targets.</p>
      <div class="chartbox"><div id="projChart"></div></div>
    </section>
  </div>

  <section class="panel">
    <h2>Shell breakdown</h2>
    <p class="hint" id="shellHint"></p>
    <div class="grid2">
      <div>
        <div class="legend" id="shellLegend"></div>
        <div class="chartbox"><div id="shellBar"></div><div class="tip" id="shellTip"></div></div>
        <table class="minitable" id="shellTable"></table>
      </div>
      <div>
        <p class="hint tight">Ceremony share per day. This is the number that moves
          if the habit changes; the Bash share alone will not.</p>
        <div class="chartbox"><div id="shellTrend"></div><div class="tip" id="trendTip"></div></div>
      </div>
    </div>
  </section>

  <section class="panel" id="marksPanel">
    <h2>Marks</h2>
    <p class="hint">The rows a human wrote: focus headlines and notes.</p>
    <div class="marks" id="marks"></div>
  </section>

  <section class="panel">
    <h2>The feed</h2>
    <p class="hint">Every row for the selected day, newest first.</p>
    <div class="filters">
      <input type="search" id="q" placeholder="Search details, paths, commands...">
      <select id="fSess"></select>
      <select id="fProj"></select>
      <span class="count" id="rowCount"></span>
    </div>
    <div class="feed" id="feed"></div>
  </section>

  <footer class="note" id="footnote"></footer>
</div>

<script id="payload" type="application/json">/*__DATA__*/null</script>
<script>
(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("payload").textContent);
  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement)
                          .getPropertyValue(name).trim();

  const FAM = DATA.familyOrder;
  const FAM_LABEL = DATA.familyLabels;
  const famColor = (f) => css("--s-" + f);
  const esc = (s) => String(s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (n) => n.toLocaleString("en-US");
  const hhmm = (s) => String(Math.floor(s / 3600)).padStart(2, "0") + ":" +
                      String(Math.floor(s / 60) % 60).padStart(2, "0");
  const hms  = (s) => hhmm(s) + ":" + String(s % 60).padStart(2, "0");

  // Row tuple indices.
  const T = 0, K = 1, S = 2, P = 3, D = 4;

  let selDay = DATA.days.length - 1;
  const famOff = new Set();
  const fresh = new Set();            // row indices that arrived on the last tick
  let filterSess = "", filterProj = "", query = "";

  // ── header + KPIs ───────────────────────────────────────
  function totals() {
    let actions = 0; const sess = new Set(); const kinds = {};
    DATA.days.forEach((d) => {
      actions += d.rows.length;
      d.rows.forEach((r) => {
        sess.add(d.date + "|" + r[S]);
        const k = DATA.kinds[r[K]];
        kinds[k] = (kinds[k] || 0) + 1;
      });
    });
    const top = Object.entries(kinds).sort((a, b) => b[1] - a[1])[0] || ["none", 0];
    const busiest = DATA.days.reduce((a, b) => (b.rows.length > a.rows.length ? b : a),
                                     DATA.days[0]);
    return { actions, sessions: sess.size, top, busiest };
  }

  function renderHead() {
    const t = totals();
    const first = DATA.days[0].date, last = DATA.days[DATA.days.length - 1].date;
    $("subline").innerHTML = first + " to " + last + " &middot; <code>" +
      esc(DATA.feedRoot) + "/</code>";
    $("footnote").textContent =
      "Generated " + DATA.generated + " by live_feed_viewer.py. " +
      "Source of record: one JSONL file per day under " + DATA.feedRoot + "/";

    const perDay = Math.round(t.actions / DATA.days.length);
    $("kpis").innerHTML = [
      ["Actions logged", fmt(t.actions), DATA.days.length + " days on disk"],
      ["Per day", fmt(perDay), "mean across the archive"],
      ["Session tabs", fmt(t.sessions), "distinct day + tab pairs"],
      ["Busiest day", fmt(t.busiest.rows.length), t.busiest.date],
      ["Top tool", t.top[0], fmt(t.top[1]) + " calls"],
    ].map(([label, value, foot]) =>
      '<div class="kpi"><div class="label">' + esc(label) + '</div>' +
      '<div class="value' + (String(value).length > 7 ? " sm" : "") + '">' + esc(value) +
      '</div><div class="foot">' + esc(foot) + "</div></div>"
    ).join("");
  }

  // ── day strip ───────────────────────────────────────────
  function renderDays() {
    const W = 1100, H = 132, padB = 26, padT = 16;
    const n = DATA.days.length;
    const slot = W / n, bw = Math.min(46, slot * 0.62);
    const max = Math.max.apply(null, DATA.days.map((d) => d.rows.length));
    const plot = H - padB - padT;
    let out = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" ' +
              'aria-label="Actions logged per day">';

    [0, 0.5, 1].forEach((f) => {
      const y = padT + plot - plot * f;
      out += '<line class="gridline" x1="0" x2="' + W + '" y1="' + y + '" y2="' + y + '"/>';
      out += '<text class="tick" x="4" y="' + (y - 4) + '">' + fmt(Math.round(max * f)) + "</text>";
    });

    DATA.days.forEach((d, i) => {
      const h = Math.max(3, (d.rows.length / max) * plot);
      const x = i * slot + (slot - bw) / 2;
      const y = padT + plot - h;
      const on = i === selDay;
      const fill = on ? css("--seq-hi") : css("--seq-lo");
      out += '<g class="daybar' + (on ? " sel" : "") + '" data-i="' + i + '">' +
             '<rect class="hit" x="' + (i * slot) + '" y="0" width="' + slot +
               '" height="' + H + '"/>' +
             '<rect class="mark" x="' + x + '" y="' + y + '" width="' + bw +
               '" height="' + h + '" rx="4" fill="' + fill + '"/>' +
             '<text class="dlabel" x="' + (x + bw / 2) + '" y="' + (y - 5) +
               '" text-anchor="middle">' + fmt(d.rows.length) + "</text>" +
             '<text class="dl" x="' + (x + bw / 2) + '" y="' + (H - 9) +
               '" text-anchor="middle">' + d.date.slice(5) + "</text></g>";
    });
    out += '<line class="baseline" x1="0" x2="' + W + '" y1="' + (padT + plot) +
           '" y2="' + (padT + plot) + '"/></svg>';
    $("dayChart").innerHTML = out;

    $("dayChart").querySelectorAll(".daybar").forEach((g) => {
      g.addEventListener("click", () => { selDay = +g.dataset.i; selectDay(); });
    });
  }

  // ── activity chart (stacked 15-min columns) ─────────────
  const BUCKET = 900;

  function renderActivity() {
    const day = DATA.days[selDay];
    const rows = day.rows;
    const counts = {};
    let lo = Infinity, hi = -Infinity;
    rows.forEach((r) => {
      const b = Math.floor(r[T] / BUCKET);
      lo = Math.min(lo, b); hi = Math.max(hi, b);
      const f = DATA.families[r[K]];
      (counts[b] = counts[b] || {})[f] = ((counts[b] || {})[f] || 0) + 1;
    });
    if (!isFinite(lo)) { $("actChart").innerHTML = '<p class="empty">No rows.</p>'; return; }

    const live = FAM.filter((f) => !famOff.has(f));
    const buckets = [];
    for (let b = lo; b <= hi; b++) {
      const c = counts[b] || {};
      buckets.push({ b: b, c: c, total: live.reduce((s, f) => s + (c[f] || 0), 0) });
    }
    const max = Math.max(1, ...buckets.map((x) => x.total));

    const W = 1100, H = 250, padL = 34, padB = 26, padT = 10;
    const plot = H - padB - padT, innerW = W - padL;
    const slot = innerW / buckets.length;
    const bw = Math.max(2, Math.min(20, slot - 2));
    const GAP = 2;                       // surface gap between stacked segments

    let out = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" ' +
              'aria-label="Tool calls per 15 minutes, stacked by tool family">';
    [0, 0.25, 0.5, 0.75, 1].forEach((f) => {
      const y = padT + plot - plot * f;
      out += '<line class="gridline" x1="' + padL + '" x2="' + W + '" y1="' + y +
             '" y2="' + y + '"/>';
      out += '<text class="tick" x="' + (padL - 6) + '" y="' + (y + 3) +
             '" text-anchor="end">' + Math.round(max * f) + "</text>";
    });

    buckets.forEach((x, i) => {
      const cx = padL + i * slot + (slot - bw) / 2;
      let acc = 0;
      live.forEach((f) => {
        const v = x.c[f] || 0;
        if (!v) return;
        const hFull = (v / max) * plot;
        const yTop = padT + plot - ((acc + v) / max) * plot;
        acc += v;
        const h = Math.max(1.5, hFull - GAP);
        out += '<rect x="' + cx + '" y="' + yTop + '" width="' + bw + '" height="' + h +
               '" rx="' + (h > 5 ? 3 : 1) + '" fill="' + famColor(f) + '"/>';
      });
      out += '<rect class="hit" data-b="' + i + '" x="' + (padL + i * slot) + '" y="' + padT +
             '" width="' + slot + '" height="' + plot + '" fill="transparent"/>';
    });

    const step = Math.max(1, Math.ceil(buckets.length / 14));
    buckets.forEach((x, i) => {
      if (i % step) return;
      out += '<text class="tick" x="' + (padL + i * slot + slot / 2) + '" y="' + (H - 9) +
             '" text-anchor="middle">' + hhmm(x.b * BUCKET) + "</text>";
    });
    out += '<line class="baseline" x1="' + padL + '" x2="' + W + '" y1="' + (padT + plot) +
           '" y2="' + (padT + plot) + '"/>';
    out += '<line id="cross" x1="0" x2="0" y1="' + padT + '" y2="' + (padT + plot) +
           '" stroke="' + css("--axis") + '" stroke-width="1" opacity="0"/>';
    out += "</svg>";
    $("actChart").innerHTML = out;

    const tip = $("actTip"), box = $("actChart"), svg = box.querySelector("svg");
    const cross = svg.querySelector("#cross");
    svg.querySelectorAll("rect.hit").forEach((h) => {
      h.addEventListener("mouseenter", () => {
        const x = buckets[+h.dataset.b];
        const t0 = x.b * BUCKET;
        let body = '<div class="th">' + hhmm(t0) + " to " + hhmm(t0 + BUCKET) + "</div>";
        let any = false;
        FAM.forEach((f) => {
          const v = x.c[f] || 0;
          if (!v) return;
          any = true;
          body += '<div class="tr"><span class="l"><span class="sw" style="background:' +
                  famColor(f) + '"></span>' + FAM_LABEL[f] + '</span><span class="v">' +
                  v + "</span></div>";
        });
        if (!any) body += '<div class="tr"><span class="l">idle</span></div>';
        tip.innerHTML = body;
        tip.classList.add("on");
        const cx = padL + (+h.dataset.b) * slot + slot / 2;
        cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
        cross.setAttribute("opacity", "0.7");
        const px = (cx / W) * box.clientWidth;
        tip.style.left = Math.min(Math.max(px + 12, 0), box.clientWidth - 170) + "px";
        tip.style.top = "8px";
      });
      h.addEventListener("mouseleave", () => {
        tip.classList.remove("on"); cross.setAttribute("opacity", "0");
      });
    });
  }

  function renderLegend() {
    const day = DATA.days[selDay];
    const per = {};
    day.rows.forEach((r) => {
      const f = DATA.families[r[K]];
      per[f] = (per[f] || 0) + 1;
    });
    $("famLegend").innerHTML = FAM.map((f) =>
      '<span class="item' + (famOff.has(f) ? " off" : "") + '" data-f="' + f + '">' +
      '<span class="swatch" style="background:' + famColor(f) + '"></span>' +
      FAM_LABEL[f] + ' <span class="n">' + fmt(per[f] || 0) + "</span></span>"
    ).join("");
    $("famLegend").querySelectorAll(".item").forEach((el) => {
      el.addEventListener("click", () => {
        const f = el.dataset.f;
        if (famOff.has(f)) famOff.delete(f); else famOff.add(f);
        if (famOff.size === FAM.length) famOff.clear();
        renderLegend(); renderActivity(); renderFeed();
      });
    });
  }

  // ── session lanes ───────────────────────────────────────
  const LANE_BUCKET = 900;

  function renderLanes() {
    const day = DATA.days[selDay];
    const bySess = {};
    let lo = Infinity, hi = -Infinity;
    day.rows.forEach((r) => {
      const sid = DATA.sessions[r[S]];
      const b = Math.floor(r[T] / LANE_BUCKET);
      lo = Math.min(lo, b); hi = Math.max(hi, b);
      (bySess[sid] = bySess[sid] || {})[b] = (bySess[sid][b] || 0) + 1;
    });
    const sids = Object.keys(bySess)
      .sort((a, b) => Object.values(bySess[b]).reduce((x, y) => x + y, 0) -
                      Object.values(bySess[a]).reduce((x, y) => x + y, 0))
      .slice(0, 14);
    if (!sids.length) { $("laneChart").innerHTML = '<p class="empty">No rows.</p>'; return; }

    let peak = 1;
    sids.forEach((s) => Object.values(bySess[s]).forEach((v) => { peak = Math.max(peak, v); }));

    const W = 540, padL = 44, padB = 20, rowH = 17, gap = 4;
    const H = sids.length * (rowH + gap) + padB + 6;
    const cols = hi - lo + 1, cw = (W - padL) / cols;
    const lerp = (a, b, t) => Math.round(a + (b - a) * t);
    const hex = (h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16),
                        parseInt(h.slice(5, 7), 16)];
    const c0 = hex(css("--seq-lo")), c1 = hex(css("--seq-hi"));

    let out = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" ' +
              'aria-label="Session tab activity over the day">';
    sids.forEach((sid, i) => {
      const y = i * (rowH + gap);
      out += '<text class="lanelabel" x="0" y="' + (y + rowH - 4) + '">' + esc(sid) + "</text>";
      out += '<rect x="' + padL + '" y="' + y + '" width="' + (W - padL) + '" height="' + rowH +
             '" rx="3" fill="' + css("--grid") + '" opacity="0.5"/>';
      for (let b = lo; b <= hi; b++) {
        const v = bySess[sid][b] || 0;
        if (!v) continue;
        const t = Math.sqrt(v / peak);
        const col = "rgb(" + lerp(c0[0], c1[0], t) + "," + lerp(c0[1], c1[1], t) + "," +
                    lerp(c0[2], c1[2], t) + ")";
        out += '<rect class="cell" data-s="' + esc(sid) + '" data-v="' + v + '" data-t="' +
               (b * LANE_BUCKET) + '" x="' + (padL + (b - lo) * cw) + '" y="' + y +
               '" width="' + Math.max(1.5, cw - 1) + '" height="' + rowH + '" rx="2" fill="' +
               col + '"/>';
      }
    });
    const ticks = 5;
    for (let i = 0; i <= ticks; i++) {
      const b = lo + Math.round(((hi - lo) * i) / ticks);
      out += '<text class="tick" x="' + (padL + (b - lo) * cw) + '" y="' + (H - 5) +
             '" text-anchor="middle">' + hhmm(b * LANE_BUCKET) + "</text>";
    }
    out += "</svg>";
    $("laneChart").innerHTML = out;

    const tip = $("laneTip"), box = $("laneChart");
    box.querySelectorAll("rect.cell").forEach((c) => {
      c.addEventListener("mouseenter", (e) => {
        tip.innerHTML = '<div class="th">tab ' + esc(c.dataset.s) + "</div>" +
          '<div class="tr"><span class="l">' + hhmm(+c.dataset.t) + " to " +
          hhmm(+c.dataset.t + LANE_BUCKET) + '</span><span class="v">' +
          c.dataset.v + "</span></div>";
        tip.classList.add("on");
        const r = box.getBoundingClientRect();
        tip.style.left = Math.min(Math.max(e.clientX - r.left + 10, 0),
                                  box.clientWidth - 160) + "px";
        tip.style.top = Math.max(e.clientY - r.top - 44, 0) + "px";
      });
      c.addEventListener("mouseleave", () => tip.classList.remove("on"));
    });
  }

  // ── projects ────────────────────────────────────────────
  function renderProjects() {
    const day = DATA.days[selDay];
    const per = {};
    day.rows.forEach((r) => {
      const p = DATA.projects[r[P]];
      if (p) per[p] = (per[p] || 0) + 1;
    });
    const items = Object.entries(per).sort((a, b) => b[1] - a[1]).slice(0, 9);
    if (!items.length) {
      $("projChart").innerHTML = '<p class="empty">No project paths in this day.</p>';
      return;
    }
    const max = items[0][1];
    const W = 540, rowH = 22, gap = 5, padL = 132, padR = 44;
    const H = items.length * (rowH + gap);
    let out = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" ' +
              'aria-label="Actions per project">';
    const lerp = (a, b, t) => Math.round(a + (b - a) * t);
    const hex = (h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16),
                        parseInt(h.slice(5, 7), 16)];
    const c0 = hex(css("--seq-lo")), c1 = hex(css("--seq-hi"));
    items.forEach(([name, v], i) => {
      const y = i * (rowH + gap);
      const w = Math.max(3, (v / max) * (W - padL - padR));
      const t = 0.35 + 0.65 * (v / max);
      const col = "rgb(" + lerp(c0[0], c1[0], t) + "," + lerp(c0[1], c1[1], t) + "," +
                  lerp(c0[2], c1[2], t) + ")";
      const label = name.length > 19 ? name.slice(0, 18) + "…" : name;
      out += '<text class="barlabel" x="' + (padL - 8) + '" y="' + (y + rowH - 6) +
             '" text-anchor="end">' + esc(label) + "</text>";
      out += '<rect x="' + padL + '" y="' + y + '" width="' + w + '" height="' + rowH +
             '" rx="4" fill="' + col + '"/>';
      out += '<text class="barval" x="' + (padL + w + 7) + '" y="' + (y + rowH - 6) + '">' +
             fmt(v) + "</text>";
    });
    out += "</svg>";
    $("projChart").innerHTML = out;
  }

  // ── shell breakdown ─────────────────────────────────────
  //
  // Three groups, not eleven buckets: the point is "how much of this needed a
  // shell", and a stacked bar of three answers that. The bucket table below
  // carries the detail, and doubles as the relief the light-mode contrast
  // check requires.

  const GRP = DATA.shellGroups;
  const GRP_LABEL = DATA.shellLabels;
  const GRP_SLOT = { ceremony: "--s-shell", covered: "--s-write", shell: "--s-read" };
  const grpColor = (g) => css(GRP_SLOT[g] || "--muted");

  function renderShell() {
    const day = DATA.days[selDay];
    const s = day.shell;
    if (!s || !s.commands) {
      $("shellHint").textContent = "No Bash calls on this day.";
      $("shellBar").innerHTML = "";
      $("shellLegend").innerHTML = "";
      $("shellTable").innerHTML = "";
      renderTrend();
      return;
    }

    $("shellHint").innerHTML = fmt(s.calls) + " Bash calls containing " +
      fmt(s.commands) + " commands (" + (s.commands / s.calls).toFixed(1) +
      " per call). Only " + (s.single / s.calls * 100).toFixed(0) +
      "% are a single command, so most are compound pipelines.";

    // Largest-remainder rounding. Rounding each share independently printed
    // 44% + 15% + 42% = 101% on the bar, which reads as a broken measurement
    // in a panel whose entire job is measurement.
    const exact = GRP.map((g) => (s.groups[g] || 0) / s.commands * 100);
    const pcts = exact.map(Math.floor);
    let short = 100 - pcts.reduce((a, b) => a + b, 0);
    exact.map((v, i) => [v - Math.floor(v), i])
      .sort((a, b) => b[0] - a[0])
      .forEach(([, i]) => { if (short > 0) { pcts[i]++; short--; } });

    $("shellLegend").innerHTML = GRP.map((g, i) =>
      '<span class="item static"><span class="swatch" style="background:' + grpColor(g) +
      '"></span>' + GRP_LABEL[g] + ' <span class="n">' +
      exact[i].toFixed(1) + "%</span></span>").join("");

    // One stacked horizontal bar. 2px surface gaps between segments per the
    // mark spec, so adjacent fills never touch.
    const W = 540, H = 34, GAP = 2;
    let x = 0, out = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" ' +
      'aria-label="Share of shell commands by group">';
    GRP.forEach((g, i) => {
      const v = s.groups[g] || 0;
      if (!v) return;
      const w = (v / s.commands) * W;
      out += '<rect class="ghit" data-g="' + g + '" x="' + x + '" y="0" width="' +
        Math.max(1, w - GAP) + '" height="' + H + '" rx="4" fill="' + grpColor(g) + '"/>';
      if (w > 46) {
        out += '<text x="' + (x + w / 2 - 1) + '" y="' + (H / 2 + 4) +
          '" text-anchor="middle" font-size="12" font-weight="600" fill="#fff">' +
          pcts[i] + "%</text>";
      }
      x += w;
    });
    out += "</svg>";
    $("shellBar").innerHTML = out;

    const tip = $("shellTip"), box = $("shellBar");
    box.querySelectorAll("rect.ghit").forEach((r) => {
      r.addEventListener("mouseenter", (e) => {
        const g = r.dataset.g;
        tip.innerHTML = '<div class="th">' + GRP_LABEL[g] + "</div>" +
          '<div class="tr"><span class="l">commands</span><span class="v">' +
          fmt(s.groups[g]) + "</span></div>";
        tip.classList.add("on");
        const bb = box.getBoundingClientRect();
        tip.style.left = Math.min(Math.max(e.clientX - bb.left - 60, 0),
                                  box.clientWidth - 150) + "px";
        tip.style.top = "-6px";
      });
      r.addEventListener("mouseleave", () => tip.classList.remove("on"));
    });

    const rows = Object.entries(s.buckets).sort((a, b) => b[1] - a[1]).slice(0, 9);
    $("shellTable").innerHTML =
      "<thead><tr><th>bucket</th><th class='num'>cmds</th><th class='num'>share</th>" +
      "<th>covered by</th></tr></thead><tbody>" +
      rows.map(([name, n]) => {
        const g = name === "echo" || name === "navigate" ? "ceremony"
                : (DATA.shellCovered[name] ? "covered" : "shell");
        return "<tr><td class='name'><span class='gdot' style='background:" + grpColor(g) +
          "'></span>" + esc(name) + "</td><td class='num'>" + fmt(n) +
          "</td><td class='num'>" + (n / s.commands * 100).toFixed(1) + "%</td>" +
          "<td class='cov'>" + esc(DATA.shellCovered[name] || "") + "</td></tr>";
      }).join("") + "</tbody>";

    renderTrend();
  }

  function renderTrend() {
    const pts = DATA.days
      .map((d, i) => ({ i: i, date: d.date, v: d.shell && d.shell.commands
        ? d.shell.groups.ceremony / d.shell.commands : null }))
      .filter((p) => p.v !== null);
    if (pts.length < 2) { $("shellTrend").innerHTML = ""; return; }

    const W = 540, H = 190, padL = 34, padB = 26, padT = 14;
    const plot = H - padB - padT, innerW = W - padL - 8;
    const max = Math.max(0.6, Math.ceil(Math.max.apply(null, pts.map((p) => p.v)) * 10) / 10);
    const X = (k) => padL + (pts.length === 1 ? innerW / 2 : (k / (pts.length - 1)) * innerW);
    const Y = (v) => padT + plot - (v / max) * plot;

    let out = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" ' +
      'aria-label="Ceremony share of shell commands per day">';
    [0, 0.5, 1].forEach((f) => {
      const y = padT + plot - plot * f;
      out += '<line class="gridline" x1="' + padL + '" x2="' + W + '" y1="' + y +
        '" y2="' + y + '"/><text class="tick" x="' + (padL - 6) + '" y="' + (y + 3) +
        '" text-anchor="end">' + Math.round(max * f * 100) + "%</text>";
    });

    const d = pts.map((p, k) => (k ? "L" : "M") + X(k) + " " + Y(p.v)).join(" ");
    out += '<path d="' + d + '" fill="none" stroke="' + css("--s-shell") +
      '" stroke-width="2" stroke-linejoin="round"/>';
    pts.forEach((p, k) => {
      // 2px surface ring so a marker never merges into the line behind it.
      out += '<circle class="thit" data-k="' + k + '" cx="' + X(k) + '" cy="' + Y(p.v) +
        '" r="4.5" fill="' + css("--s-shell") + '" stroke="' + css("--surface-1") +
        '" stroke-width="2"/>';
    });
    // Direct-label the endpoints only; a number on every point is noise.
    [0, pts.length - 1].forEach((k) => {
      const p = pts[k];
      out += '<text class="dlabel" x="' + X(k) + '" y="' + (Y(p.v) - 11) +
        '" text-anchor="' + (k === 0 ? "start" : "end") + '">' +
        (p.v * 100).toFixed(0) + "%</text>";
    });
    const step = Math.max(1, Math.ceil(pts.length / 6));
    pts.forEach((p, k) => {
      if (k % step && k !== pts.length - 1) return;
      out += '<text class="tick" x="' + X(k) + '" y="' + (H - 8) +
        '" text-anchor="middle">' + p.date.slice(5) + "</text>";
    });
    out += '<line class="baseline" x1="' + padL + '" x2="' + W + '" y1="' + (padT + plot) +
      '" y2="' + (padT + plot) + '"/></svg>';
    $("shellTrend").innerHTML = out;

    const tip = $("trendTip"), box = $("shellTrend");
    box.querySelectorAll("circle.thit").forEach((c) => {
      c.addEventListener("mouseenter", () => {
        const p = pts[+c.dataset.k];
        const day = DATA.days[p.i];
        tip.innerHTML = '<div class="th">' + p.date + "</div>" +
          '<div class="tr"><span class="l">ceremony</span><span class="v">' +
          (p.v * 100).toFixed(1) + "%</span></div>" +
          '<div class="tr"><span class="l">tool-covered</span><span class="v">' +
          (day.shell.groups.covered / day.shell.commands * 100).toFixed(1) + "%</span></div>";
        tip.classList.add("on");
        tip.style.left = Math.min((+c.getAttribute("cx") / W) * box.clientWidth + 10,
                                  box.clientWidth - 160) + "px";
        tip.style.top = "6px";
      });
      c.addEventListener("mouseleave", () => tip.classList.remove("on"));
    });
  }

  // ── marks ───────────────────────────────────────────────
  function renderMarks() {
    const day = DATA.days[selDay];
    const items = [];
    if (day.focus) {
      items.push('<div class="mark-item focus"><span class="mt">' +
        esc(day.focus.set_at || "") + '</span><span class="mx">Now: ' +
        esc(day.focus.focus) + "</span></div>");
    }
    day.rows.forEach((r) => {
      if (DATA.kinds[r[K]] !== "note") return;
      items.push('<div class="mark-item"><span class="mt">' + hhmm(r[T]) +
        '</span><span class="mx">' + esc(r[D]) + "</span></div>");
    });
    if (!items.length) {
      $("marks").innerHTML = '<p class="empty">No focus headline or notes on this day. ' +
        'Set one with <code>live_feed.py --focus "..."</code>.</p>';
    } else {
      $("marks").innerHTML = items.join("");
    }
  }

  // ── feed ────────────────────────────────────────────────
  function renderFilters() {
    const day = DATA.days[selDay];
    const sess = {}, proj = {};
    day.rows.forEach((r) => {
      const s = DATA.sessions[r[S]]; sess[s] = (sess[s] || 0) + 1;
      const p = DATA.projects[r[P]]; if (p) proj[p] = (proj[p] || 0) + 1;
    });
    const opt = (val, label, sel) =>
      '<option value="' + esc(val) + '"' + (sel ? " selected" : "") + ">" +
      esc(label) + "</option>";
    $("fSess").innerHTML = opt("", "All tabs", !filterSess) +
      Object.entries(sess).sort((a, b) => b[1] - a[1])
        .map(([s, n]) => opt(s, "tab " + s + " (" + n + ")", filterSess === s)).join("");
    $("fProj").innerHTML = opt("", "All projects", !filterProj) +
      Object.entries(proj).sort((a, b) => b[1] - a[1])
        .map(([p, n]) => opt(p, p + " (" + n + ")", filterProj === p)).join("");
  }

  function highlight(text, q) {
    const safe = esc(text);
    if (!q) return safe;
    const i = safe.toLowerCase().indexOf(esc(q).toLowerCase());
    if (i === -1) return safe;
    return safe.slice(0, i) + "<mark>" + safe.slice(i, i + q.length) + "</mark>" +
           safe.slice(i + q.length);
  }

  function renderFeed() {
    const day = DATA.days[selDay];
    const q = query.trim().toLowerCase();
    const out = [];
    let shown = 0;
    const LIMIT = 900;
    const feedEl = $("feed");
    const keepScroll = feedEl.scrollTop;

    for (let i = day.rows.length - 1; i >= 0; i--) {
      const r = day.rows[i];
      const kind = DATA.kinds[r[K]];
      const fam = DATA.families[r[K]];
      if (famOff.has(fam)) continue;
      const sid = DATA.sessions[r[S]];
      if (filterSess && sid !== filterSess) continue;
      const proj = DATA.projects[r[P]];
      if (filterProj && proj !== filterProj) continue;
      if (q && r[D].toLowerCase().indexOf(q) === -1 &&
               kind.toLowerCase().indexOf(q) === -1) continue;
      shown++;
      if (shown > LIMIT) continue;
      out.push('<div class="row' + (kind === "note" ? " note" : "") +
        (fresh.has(i) ? " fresh" : "") + '">' +
        '<span class="t">' + hms(r[T]) + "</span>" +
        '<span class="k"><span class="dot" style="background:' + famColor(fam) + '"></span>' +
          esc(kind) + "</span>" +
        '<span class="s">' + esc(sid) + "</span>" +
        '<span class="d">' + (proj ? '<span class="proj">' + esc(proj) + " &rsaquo; </span>" : "") +
          highlight(r[D], query.trim()) + "</span></div>");
    }

    feedEl.innerHTML = out.length ? out.join("")
      : '<p class="empty">Nothing matches these filters.</p>';
    // The list is newest-first, so a reader parked mid-history should stay
    // there when new rows land at the top rather than being yanked upward.
    feedEl.scrollTop = keepScroll;
    fresh.clear();
    $("rowCount").textContent = fmt(shown) + " of " + fmt(day.rows.length) + " rows" +
      (shown > LIMIT ? " (showing first " + fmt(LIMIT) + ")" : "");
  }

  // ── orchestration ───────────────────────────────────────
  // Kept separate from selectDay so a live tick refreshes the count too. When
  // it lived inside selectDay the header went stale the moment a row landed,
  // and the page quietly disagreed with its own day strip.
  function renderDayHeader() {
    const day = DATA.days[selDay];
    $("dayTitle").textContent = "Activity on " + day.date;
    $("dayHint").textContent = fmt(day.rows.length) +
      " tool calls, bucketed into 15 minute columns and stacked by tool family. " +
      "Click a family in the legend to mute it.";
  }

  function selectDay() {
    renderDayHeader();
    filterSess = ""; filterProj = "";
    renderDays(); renderLegend(); renderActivity(); renderLanes();
    renderProjects(); renderShell(); renderMarks(); renderFilters(); renderFeed();
  }

  $("q").addEventListener("input", (e) => { query = e.target.value; renderFeed(); });
  $("fSess").addEventListener("change", (e) => { filterSess = e.target.value; renderFeed(); });
  $("fProj").addEventListener("change", (e) => { filterProj = e.target.value; renderFeed(); });
  $("themeBtn").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "light";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    $("themeBtn").textContent = dark ? "Dark mode" : "Light mode";
    renderDays(); renderLegend(); renderActivity(); renderLanes();
    renderProjects(); renderShell(); renderFeed();
  });

  // ── live tail ───────────────────────────────────────────
  //
  // No browser can watch a file on disk, so the page asks the server for
  // whatever was appended past the byte offset it already holds. Each tick
  // moves a few hundred bytes, not the whole day.

  const POLL_MS = 2000;
  const IX = { k: {}, s: {}, p: {} };
  DATA.kinds.forEach((v, i) => { IX.k[v] = i; });
  DATA.sessions.forEach((v, i) => { IX.s[v] = i; });
  DATA.projects.forEach((v, i) => { IX.p[v] = i; });

  function ik(v) {
    if (!(v in IX.k)) {
      IX.k[v] = DATA.kinds.length;
      DATA.kinds.push(v);
      DATA.families.push(DATA.familyMap[v] || "plan");
    }
    return IX.k[v];
  }
  function is(v) {
    if (!(v in IX.s)) { IX.s[v] = DATA.sessions.length; DATA.sessions.push(v); }
    return IX.s[v];
  }
  function ip(v) {
    if (!(v in IX.p)) { IX.p[v] = DATA.projects.length; DATA.projects.push(v); }
    return IX.p[v];
  }

  function todayIndex() {
    for (let i = 0; i < DATA.days.length; i++) {
      if (DATA.days[i].date === DATA.today) return i;
    }
    return -1;
  }

  let misses = 0;

  function badge(state, text) {
    const el = $("liveBadge");
    el.classList.toggle("stale", state === "stale");
    $("liveText").textContent = text;
    if (state === "hit") {
      el.classList.add("hit");
      setTimeout(() => el.classList.remove("hit"), 200);
    }
  }

  async function poll() {
    let ti = todayIndex();
    const offset = ti === -1 ? 0 : (DATA.days[ti].size || 0);
    let j;
    try {
      const res = await fetch("/api/tail?date=" + encodeURIComponent(DATA.today) +
                              "&offset=" + offset, { cache: "no-store" });
      if (!res.ok) throw new Error(res.status);
      j = await res.json();
      misses = 0;
    } catch (err) {
      misses++;
      badge("stale", misses > 2 ? "server gone" : "retrying");
      return;
    }

    // A day that was empty when the page loaded (opened just after midnight)
    // has no entry yet, so create one the moment its first row lands.
    let born = false;
    if (ti === -1 && j.rows.length) {
      const wasOnLatest = selDay === DATA.days.length - 1;
      DATA.days.push({ date: DATA.today, rows: [], size: 0, focus: j.focus || null });
      ti = DATA.days.length - 1;
      born = true;
      if (wasOnLatest) selDay = ti;      // follow the front of the archive
    }
    if (ti === -1) { badge("live", "live"); return; }

    const day = DATA.days[ti];
    const focusChanged =
      JSON.stringify(day.focus || null) !== JSON.stringify(j.focus || null);
    day.focus = j.focus || null;
    day.size = j.offset;

    if (!j.rows.length) {
      badge("live", "live");
      if (focusChanged && selDay === ti) renderMarks();
      return;
    }

    fresh.clear();
    j.rows.forEach((r) => {
      fresh.add(day.rows.length);
      day.rows.push([r[0], ik(r[1]), is(r[2]), ip(r[3]), r[4]]);
    });

    // Fold the server's classification of just the new commands into the
    // running totals, so the shell panel tracks the feed instead of showing
    // whatever was true when the page loaded.
    if (j.shellDelta && day.shell) {
      const s = day.shell, dlt = j.shellDelta;
      s.calls += dlt.calls;
      s.single += dlt.single;
      s.commands += dlt.commands;
      GRP.forEach((g) => { s.groups[g] = (s.groups[g] || 0) + (dlt.groups[g] || 0); });
      Object.entries(dlt.buckets).forEach(([k, v]) => {
        s.buckets[k] = (s.buckets[k] || 0) + v;
      });
    }

    badge("hit", "live " + hhmm(j.rows[j.rows.length - 1][0]));
    if (born) { renderHead(); selectDay(); return; }   // first day ever: full boot
    renderHead();
    renderDays();
    if (selDay === ti) {
      renderDayHeader(); renderLegend(); renderActivity(); renderLanes();
      renderProjects(); renderShell(); renderMarks(); renderFilters(); renderFeed();
    }
  }

  if (!DATA.days.length) {
    if (!DATA.live) {
      document.querySelector(".wrap").innerHTML =
        '<h1>Live Feed Archive</h1><p class="empty">No feed files found under ' +
        esc(DATA.feedRoot) + "/</p>";
      return;
    }
    // Live with nothing on disk yet: sit and wait for the first row.
    $("subline").textContent = "waiting for the first row under " + DATA.feedRoot + "/";
  } else {
    renderHead();
    selectDay();
  }

  if (DATA.live) {
    $("liveBadge").classList.add("on");
    badge("live", "live");
    poll();
    setInterval(poll, POLL_MS);
    // A backgrounded tab throttles timers; catch up the moment it is looked at.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) poll();
    });
  }
})();
</script>
</body>
</html>
"""


# ── CLI ────────────────────────────────────────────────────────────────

def main(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Browsable view of the Live Feed archive")
    ap.add_argument("--build", action="store_true",
                    help="write a frozen self-contained HTML file instead of serving")
    ap.add_argument("--port", type=int, default=8787, help="serve port (default 8787)")
    ap.add_argument("--out", help="output HTML path for --build")
    ap.add_argument("--days", type=int, help="only load the last N days")
    ap.add_argument("--no-open", action="store_true", help="never launch a browser")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)

    if args.build or args.out:
        out = Path(args.out).expanduser() if args.out else DEFAULT_OUT
        stats = build(out, args.days)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(f"live_feed_viewer: {stats['actions']} actions across "
                  f"{stats['days']} days -> {out} ({stats['bytes'] / 1024:.0f} KB)")
        if not args.no_open:
            webbrowser.open(out.resolve().as_uri())
        return 0

    if args.json:
        print(json.dumps(summarize(load(args.days)), indent=2))
        return 0

    return serve(args.port, args.days, not args.no_open)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
