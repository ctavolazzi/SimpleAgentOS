"""
pb_journal.py: SimpleAgentOS's own PocketBase-backed journal (queryable memory).

The OS already writes a lot of things down: daily notes, session logs, findings,
commits, errors. Those live in markdown and in scattered SQLite tables, which is
fine for *writing* and bad for *asking*. This module gives SimpleAgentOS one
place it can both write to and query back:

    agent_journal   append-only rows in the project's OWN PocketBase instance
                    (core_engine/pocketbase + core_engine/pb_data)

Design notes
------------
* **Never blocks the caller.** If PocketBase is down, the entry goes to an
  append-only spool (.self_explorer/journal_spool.jsonl) and gets drained on the
  next successful contact. Journaling must never be the reason something fails.
* **Queryable offline too.** PocketBase stores records in plain SQLite, so when
  the server is down we read pb_data/data.db directly (read-only) and merge in
  anything still sitting in the spool. Memory stays askable either way.
* **Stdlib only.** Same constraint as the rest of this codebase.
* **Localhost only.** The collection's access rules are public (matching the
  other collections here), so the server is always bound to 127.0.0.1.

CLI
---
    python3 pb_journal.py serve                     # start the OS's PocketBase
    python3 pb_journal.py status                    # is it up? how many entries?
    python3 pb_journal.py log "text" --kind finding --tags harness,pb
    python3 pb_journal.py recent -n 20
    python3 pb_journal.py query "pocketbase" --kind finding --since 2026-07-01
    python3 pb_journal.py sync                      # drain the offline spool
    python3 pb_journal.py stats

Python API
----------
    from pb_journal import journal, query, recent, stats, sync

    journal("wired the journal into wrap_up", kind="event", tags=["harness"])
    for row in query("pocketbase", kind="finding", limit=10):
        print(row["occurred_at"], row["title"])
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

__version__ = "0.0.1"

# ── Paths + config ────────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
CORE_ENGINE = HERE / "core_engine"
PB_BIN = CORE_ENGINE / "pocketbase"
PB_DATA = CORE_ENGINE / "pb_data"
PB_MIGRATIONS = CORE_ENGINE / "pb_migrations"
PB_DB = PB_DATA / "data.db"

STATE_DIR = HERE / ".self_explorer"
SPOOL_PATH = Path(os.environ.get("PB_JOURNAL_SPOOL", STATE_DIR / "journal_spool.jsonl"))
PB_LOG = STATE_DIR / "pocketbase.log"
PB_PID = STATE_DIR / "pocketbase.pid"

# The spool is an outbox, not an archive. If PocketBase stays down for a long
# time it would otherwise grow without limit, and an unbounded file on the
# write path of a "never blocks the caller" API is a bad trade. So it is
# capped, and what gets dropped is counted rather than silently discarded:
# `stats()` and `doctor` both surface the tally.
SPOOL_MAX_BYTES = int(os.environ.get("PB_JOURNAL_SPOOL_MAX_BYTES", 8 * 1024 * 1024))
SPOOL_MAX_ENTRIES = int(os.environ.get("PB_JOURNAL_SPOOL_MAX_ENTRIES", "5000"))
SPOOL_MAX_AGE_DAYS = float(os.environ.get("PB_JOURNAL_SPOOL_MAX_AGE_DAYS", "30"))

PB_HOST = os.environ.get("PB_HOST", "127.0.0.1")
PB_PORT = int(os.environ.get("PB_PORT", "8090"))
PB_BASE = os.environ.get("PB_BASE", f"http://{PB_HOST}:{PB_PORT}")
PB_TIMEOUT = float(os.environ.get("PB_JOURNAL_TIMEOUT", "2.0"))

# The collection every read and write goes through. Overridable so tests can
# point at `agent_journal_test` instead of polluting the real memory; see
# TEST_COLLECTION below and tests/test_pb_journal.py.
COLLECTION = os.environ.get("PB_JOURNAL_COLLECTION", "agent_journal")
TEST_COLLECTION = "agent_journal_test"
SCHEMA_VERSION = "agent_journal/v1"

# Free-form, but these are the ones the harness uses. Unknown kinds are allowed
# on purpose: an over-strict enum just makes callers lie.
KINDS = {
    "note",        # something worth remembering, no stronger claim
    "finding",     # a discovered fact
    "decision",    # a choice made, with rationale in the body
    "question",    # an open unknown
    "event",       # something happened (harness op, run, deploy)
    "session",     # session start/end boundary
    "error",       # something broke
    "commit",      # a git commit worth recalling
    "artifact",    # a file/doc was produced
    "reflection",  # synthesis, inner voice
}

_TEXT_FIELDS = ("title", "body")
_ALL_FIELDS = (
    "entry_id", "content_hash", "occurred_at", "kind", "actor", "source",
    "project", "session_id", "title", "body", "tags", "path_ref",
    "importance", "metadata",
)

_spool_lock = threading.Lock()


# ── Small helpers ─────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _pb_datetime(iso: str) -> str:
    """PocketBase 0.22 wants 'YYYY-MM-DD HH:MM:SS.mmmZ', not RFC3339 with +00:00."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _norm_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t).strip() for t in tags if str(t).strip()]


def _q(value: str) -> str:
    """Quote a value for a PocketBase filter expression."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ── PocketBase HTTP client ────────────────────────────────────────────

class PB:
    """Thin PocketBase client. Best-effort: callers get None, never an exception."""

    def __init__(self, base: str = PB_BASE, timeout: float = PB_TIMEOUT):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._online: Optional[bool] = None
        self.last_error: str = ""

    def is_online(self, recheck: bool = False) -> bool:
        if self._online is not None and not recheck:
            return self._online
        try:
            req = urllib.request.Request(f"{self.base}/api/health")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._online = resp.status == 200
        except Exception as exc:  # URLError, timeout, refused connection
            self.last_error = str(exc)
            self._online = False
        return self._online

    def _request(self, method: str, path: str, body: Optional[dict] = None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    def create(self, collection: str, record: dict) -> Optional[dict]:
        if not self.is_online():
            return None
        try:
            return self._request("POST", f"/api/collections/{collection}/records", record)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            self.last_error = f"HTTP {exc.code}: {detail}"
            # A unique-index collision means this entry is already remembered.
            # Idempotent by design: report success so the spool can drop it.
            if exc.code == 400 and "entry_id" in detail:
                return {"duplicate": True}
            return None
        except Exception as exc:
            self.last_error = str(exc)
            self._online = False
            return None

    def list(self, collection: str, filt: str = "", sort: str = "-occurred_at",
             limit: int = 50, page: int = 1) -> Optional[list[dict]]:
        if not self.is_online():
            return None
        params = [
            f"perPage={max(1, min(limit, 500))}",
            f"page={page}",
            f"sort={urllib.parse.quote(sort)}",
        ]
        if filt:
            params.append("filter=" + urllib.parse.quote(filt))
        try:
            out = self._request("GET", f"/api/collections/{collection}/records?" + "&".join(params))
            return out.get("items", [])
        except urllib.error.HTTPError as exc:
            self.last_error = f"HTTP {exc.code}: {exc.read()[:400].decode('utf-8', 'replace')}"
            return None
        except Exception as exc:
            self.last_error = str(exc)
            self._online = False
            return None

    def delete(self, collection: str, record_id: str) -> bool:
        """Delete one record. Only `agent_journal_test` permits this: the real
        journal leaves deleteRule null, so a stray call there gets a 403 and
        returns False rather than quietly removing a memory."""
        if not self.is_online():
            return False
        try:
            self._request("DELETE", f"/api/collections/{collection}/records/{record_id}")
            return True
        except urllib.error.HTTPError as exc:
            self.last_error = f"HTTP {exc.code}: {exc.read()[:400].decode('utf-8', 'replace')}"
            return False
        except Exception as exc:
            self.last_error = str(exc)
            self._online = False
            return False

    def has_collection(self, collection: str) -> bool:
        """Probe by listing 1 record. A 404 means the migration hasn't run."""
        if not self.is_online():
            return False
        try:
            self._request("GET", f"/api/collections/{collection}/records?perPage=1")
            return True
        except urllib.error.HTTPError as exc:
            self.last_error = f"HTTP {exc.code}"
            return exc.code != 404
        except Exception as exc:
            self.last_error = str(exc)
            return False


_client: Optional[PB] = None


def client(fresh: bool = False) -> PB:
    global _client
    if _client is None or fresh:
        _client = PB()
    return _client


# ── Server lifecycle ──────────────────────────────────────────────────

def server_running(base: str = PB_BASE) -> bool:
    return PB(base).is_online(recheck=True)


def serve(wait: float = 10.0, quiet: bool = False) -> bool:
    """Start the project's own PocketBase, detached, bound to localhost.

    Idempotent: returns True immediately if it's already up. Applying pending
    migrations (including agent_journal) happens automatically on serve.
    """
    if server_running():
        if not quiet:
            print(f"PocketBase already running at {PB_BASE}")
        return True
    if not PB_BIN.exists():
        print(f"PocketBase binary not found at {PB_BIN}", file=sys.stderr)
        print("  Fetch it with: python3 build_os.py   (or download v0.22.8 manually)",
              file=sys.stderr)
        return False

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PB_DATA.mkdir(parents=True, exist_ok=True)
    log_fh = open(PB_LOG, "ab")
    proc = subprocess.Popen(
        [
            str(PB_BIN), "serve",
            f"--http={PB_HOST}:{PB_PORT}",
            "--dir", str(PB_DATA),
            "--migrationsDir", str(PB_MIGRATIONS),
        ],
        cwd=str(CORE_ENGINE),
        stdout=log_fh, stderr=log_fh, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    PB_PID.write_text(str(proc.pid))

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if server_running():
            if not quiet:
                print(f"PocketBase up at {PB_BASE} (pid {proc.pid}), log: {PB_LOG}")
            client(fresh=True)
            return True
        if proc.poll() is not None:
            print(f"PocketBase exited immediately (code {proc.returncode}). See {PB_LOG}",
                  file=sys.stderr)
            return False
        time.sleep(0.25)
    print(f"PocketBase did not answer within {wait}s. See {PB_LOG}", file=sys.stderr)
    return False


def stop() -> bool:
    """Stop the PocketBase we started (only ours: we go by our own pidfile)."""
    if not PB_PID.exists():
        if launchd_loaded():
            print(f"PocketBase is supervised by launchd ({LAUNCHD_LABEL}).")
            print(f"  stop it with: launchctl bootout gui/{os.getuid()}/{LAUNCHD_LABEL}")
            print("  or remove supervision: python3 pb_journal.py launchd uninstall")
            return False
        print("No pidfile. Not started by pb_journal.")
        return False
    try:
        pid = int(PB_PID.read_text().strip())
    except ValueError:
        PB_PID.unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, 15)
        print(f"Stopped PocketBase (pid {pid})")
    except ProcessLookupError:
        print(f"pid {pid} not running")
    PB_PID.unlink(missing_ok=True)
    return True


# ── launchd supervision ───────────────────────────────────────────────
#
# `serve` is fine for a session. It is not fine as the only way the store ever
# runs: every harness call while PocketBase is down degrades to spool only, and
# nobody notices until the spool is large. Under launchd the store is simply up.

LAUNCHD_LABEL = "com.simpleagentos.pocketbase"
LAUNCHD_SRC = CORE_ENGINE / f"{LAUNCHD_LABEL}.plist"
LAUNCHD_DEST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_loaded() -> bool:
    """True if the agent is registered with launchd right now."""
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
            capture_output=True, timeout=5,
        )
        return out.returncode == 0
    except Exception:
        return False


def launchd_install() -> bool:
    if sys.platform != "darwin":
        print("launchd is macOS only.", file=sys.stderr)
        return False
    if not LAUNCHD_SRC.exists():
        print(f"Missing plist at {LAUNCHD_SRC}", file=sys.stderr)
        return False

    # A hand-started PocketBase holds the port; launchd would fight it.
    if PB_PID.exists():
        stop()
        time.sleep(1)

    LAUNCHD_DEST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_DEST.write_text(LAUNCHD_SRC.read_text())
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                   capture_output=True)
    proc = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCHD_DEST)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"bootstrap failed: {proc.stderr.strip()}", file=sys.stderr)
        return False

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server_running():
            print(f"{LAUNCHD_LABEL} installed and running at {PB_BASE}")
            print(f"  plist: {LAUNCHD_DEST}")
            print("  it now starts at login and restarts if it dies")
            client(fresh=True)
            return True
        time.sleep(0.5)
    print(f"Agent loaded but PocketBase did not answer. See {PB_LOG}", file=sys.stderr)
    return False


def launchd_uninstall() -> bool:
    if sys.platform != "darwin":
        return False
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                   capture_output=True)
    LAUNCHD_DEST.unlink(missing_ok=True)
    print(f"{LAUNCHD_LABEL} removed. PocketBase is back to manual `serve`.")
    return True


def ensure_server(autostart: Optional[bool] = None) -> bool:
    """Make sure the journal has somewhere to write.

    autostart defaults to the PB_JOURNAL_AUTOSTART env var (default off), so
    importing this module never spawns a server behind the caller's back.
    """
    if server_running():
        return True
    if autostart is None:
        autostart = os.environ.get("PB_JOURNAL_AUTOSTART", "0") not in ("0", "", "false")
    return serve(quiet=True) if autostart else False


# ── The spool (offline outbox) ────────────────────────────────────────

def _spool_append(entry: dict) -> None:
    with _spool_lock:
        SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SPOOL_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        oversize = SPOOL_PATH.stat().st_size > SPOOL_MAX_BYTES
    # Outside the lock: _spool_trim takes it itself, and a stat() per append is
    # cheap where re-reading the whole file per append would not be.
    if oversize:
        _spool_trim()


def _dropped_path() -> Path:
    """Derived from SPOOL_PATH at call time, so it follows a redirected spool
    (tests point SPOOL_PATH at a tmp_path and must not touch the real one)."""
    return SPOOL_PATH.with_suffix(".dropped")


def spool_dropped() -> int:
    """How many entries the cap has discarded. Loss should be countable."""
    try:
        return int(_dropped_path().read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _record_dropped(n: int) -> None:
    if n <= 0:
        return
    try:
        _dropped_path().write_text(str(spool_dropped() + n))
    except OSError:
        pass  # a full disk is exactly when this fires; do not make it fatal


def _spool_trim() -> int:
    """Bring the spool back under its caps. Returns how many were dropped.

    Age first, then count. Oldest go first: an outbox that keeps 30-day-old
    entries and drops what just happened has its priorities backwards.
    """
    entries = _spool_read()
    if not entries:
        return 0

    kept = entries
    if SPOOL_MAX_AGE_DAYS > 0:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=SPOOL_MAX_AGE_DAYS)).isoformat()
        kept = [e for e in kept if str(e.get("occurred_at") or "") >= cutoff]
    if SPOOL_MAX_ENTRIES > 0 and len(kept) > SPOOL_MAX_ENTRIES:
        kept = kept[-SPOOL_MAX_ENTRIES:]

    dropped = len(entries) - len(kept)
    if dropped:
        _spool_rewrite(kept)
        _record_dropped(dropped)
    return dropped


def _spool_read() -> list[dict]:
    if not SPOOL_PATH.exists():
        return []
    out = []
    with open(SPOOL_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn line shouldn't poison the whole spool
    return out


def _spool_rewrite(entries: list[dict]) -> None:
    with _spool_lock:
        SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SPOOL_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        tmp.replace(SPOOL_PATH)


def sync(verbose: bool = False) -> dict:
    """Drain the offline spool into PocketBase. Safe to call any time."""
    pending = _spool_read()
    if not pending:
        return {"pending": 0, "synced": 0, "remaining": 0, "online": server_running()}
    pb = client()
    # Re-probe rather than swap in a new client: the caller (or a test) may have
    # installed a specific one, and "is PocketBase back yet" is the only thing
    # that's actually stale here.
    if not pb.is_online(recheck=True):
        return {"pending": len(pending), "synced": 0, "remaining": len(pending),
                "online": False, "error": pb.last_error}

    remaining, synced = [], 0
    for entry in pending:
        if pb.create(COLLECTION, _to_pb_record(entry)) is not None:
            synced += 1
        else:
            remaining.append(entry)
            if verbose:
                print(f"  still stuck: {entry.get('entry_id')}: {pb.last_error}",
                      file=sys.stderr)
    _spool_rewrite(remaining)
    return {"pending": len(pending), "synced": synced, "remaining": len(remaining),
            "online": True, "error": pb.last_error if remaining else ""}


# ── Writing ───────────────────────────────────────────────────────────

def _to_pb_record(entry: dict) -> dict:
    """Entry dict → the shape PocketBase wants on the wire."""
    rec = {k: entry.get(k) for k in _ALL_FIELDS if entry.get(k) is not None}
    rec["occurred_at"] = _pb_datetime(entry.get("occurred_at", ""))
    rec["tags"] = _norm_tags(entry.get("tags"))
    rec["metadata"] = entry.get("metadata") or {}
    return rec


def build_entry(body: str, kind: str = "note", *, title: str = "",
                tags: Any = None, actor: str = "", source: str = "",
                project: str = "", session_id: str = "", path_ref: str = "",
                importance: float = 0.5, metadata: Optional[dict] = None,
                occurred_at: Optional[str] = None) -> dict:
    occurred_at = occurred_at or _now_iso()
    body = body or ""
    if not title:
        first = body.strip().splitlines()[0] if body.strip() else ""
        title = (first[:120] + "…") if len(first) > 120 else first
    content_hash = _hash(f"{kind}|{title}|{body}")
    return {
        "entry_id": _hash(f"{occurred_at}|{content_hash}|{uuid.uuid4()}"),
        "content_hash": content_hash,
        "occurred_at": occurred_at,
        "kind": kind,
        "actor": actor or os.environ.get("AGENT_ACTOR", "claude"),
        "source": source or "api",
        "project": project or os.environ.get("AGENT_PROJECT", "SimpleAgentOS"),
        "session_id": session_id,
        "title": title,
        "body": body,
        "tags": _norm_tags(tags),
        "path_ref": path_ref,
        "importance": float(importance),
        "metadata": metadata or {},
        "_schema": SCHEMA_VERSION,
    }


def journal(body: str, kind: str = "note", **kwargs) -> dict:
    """Write one thing down. Returns the entry, with `_stored` telling you where.

    Never raises. If PocketBase is unreachable the entry lands in the spool and
    `_stored` is "spool", and `sync()` picks it up later.
    """
    entry = build_entry(body, kind, **kwargs)
    # Anything at all can go wrong on the way to PocketBase: a broken client, a
    # DNS stall, a bad monkeypatch. None of it is the caller's problem: worst
    # case the entry goes to the spool.
    try:
        pb = client()
        if pb.is_online():
            if pb.create(COLLECTION, _to_pb_record(entry)) is not None:
                entry["_stored"] = "pocketbase"
                return entry
            # Online but the write failed (missing collection, validation), so
            # spool it rather than lose it, and leave the error where a caller
            # can see it.
            entry["_error"] = pb.last_error
    except Exception as exc:  # noqa: BLE001
        entry["_error"] = f"{type(exc).__name__}: {exc}"
    _spool_append(entry)
    entry["_stored"] = "spool"
    return entry


# ── Reading ───────────────────────────────────────────────────────────

def _matches_offline(row: dict, text: str, kind: str, tags: list[str],
                     project: str, session_id: str, since: str, until: str) -> bool:
    if kind and row.get("kind") != kind:
        return False
    if project and row.get("project") != project:
        return False
    if session_id and row.get("session_id") != session_id:
        return False
    occurred = str(row.get("occurred_at") or "")
    if since and occurred < since:
        return False
    if until and occurred > until:
        return False
    if tags:
        row_tags = {t.lower() for t in _norm_tags(row.get("tags"))}
        if not row_tags.issuperset({t.lower() for t in tags}):
            return False
    if text:
        haystack = " ".join(str(row.get(f) or "") for f in _TEXT_FIELDS).lower()
        if text.lower() not in haystack:
            return False
    return True


def _connect_db() -> Optional[sqlite3.Connection]:
    """Open PocketBase's SQLite file for reading.

    Prefers mode=ro. That handle can't create the -shm sidecar a WAL database
    needs, which bites right after PocketBase exits with an un-checkpointed WAL
    still on disk, so fall back to a normal handle. We only ever SELECT.
    """
    if not PB_DB.exists():
        return None
    for uri, kwargs in ((f"file:{PB_DB}?mode=ro", {"uri": True}), (str(PB_DB), {})):
        try:
            conn = sqlite3.connect(uri, timeout=2.0, **kwargs)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            continue
    return None


def _table_exists() -> bool:
    """Has the migration actually landed? Answerable without the server."""
    conn = _connect_db()
    if conn is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (COLLECTION,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _query_sqlite(text: str, kind: str, tags: list[str], project: str,
                  session_id: str, since: str, until: str, limit: int) -> list[dict]:
    """Offline read straight out of PocketBase's SQLite file (read-only)."""
    conn = _connect_db()
    if conn is None:
        return []
    try:
        have = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (COLLECTION,)
        ).fetchone()
        if not have:
            return []
        rows = conn.execute(
            f"SELECT * FROM {COLLECTION} ORDER BY occurred_at DESC LIMIT 5000"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    out = []
    for raw in rows:
        row = dict(raw)
        for jf in ("tags", "metadata"):
            if isinstance(row.get(jf), str):
                try:
                    row[jf] = json.loads(row[jf])
                except json.JSONDecodeError:
                    pass
        if _matches_offline(row, text, kind, tags, project, session_id, since, until):
            row["_source"] = "sqlite"
            out.append(row)
        if len(out) >= limit:
            break
    return out


def query(text: str = "", *, kind: str = "", tags: Any = None, project: str = "",
          session_id: str = "", since: str = "", until: str = "", limit: int = 50,
          offline: Optional[bool] = None, include_spool: bool = True) -> list[dict]:
    """Ask the journal a question.

    Live path goes through PocketBase's filter API. If the server is down (or
    `offline=True`) it reads pb_data/data.db directly. Un-synced spool entries
    are merged in either way so recent memory is never invisible.
    """
    tags = _norm_tags(tags)
    since_iso = _pb_datetime(since) if since else ""
    until_iso = _pb_datetime(until) if until else ""

    rows: list[dict] = []
    used_live = False
    if not offline:
        pb = client()
        if pb.is_online():
            clauses = []
            if kind:
                clauses.append(f"kind={_q(kind)}")
            if project:
                clauses.append(f"project={_q(project)}")
            if session_id:
                clauses.append(f"session_id={_q(session_id)}")
            if since_iso:
                clauses.append(f"occurred_at>={_q(since_iso)}")
            if until_iso:
                clauses.append(f"occurred_at<={_q(until_iso)}")
            if text:
                clauses.append(f"(title~{_q(text)} || body~{_q(text)})")
            for tag in tags:
                clauses.append(f"tags~{_q(tag)}")
            got = pb.list(COLLECTION, " && ".join(clauses), limit=limit)
            if got is not None:
                used_live = True
                for row in got:
                    row["_source"] = "pocketbase"
                rows = got

    if not used_live:
        rows = _query_sqlite(text, kind, tags, project, session_id,
                             since_iso or since, until_iso or until, limit)

    if include_spool:
        stored = {r.get("entry_id") for r in rows}
        for entry in _spool_read():
            if entry.get("entry_id") in stored:
                continue
            if _matches_offline(entry, text, kind, tags, project, session_id,
                                since or since_iso, until or until_iso):
                entry = dict(entry)
                entry["_source"] = "spool"
                rows.append(entry)

    rows.sort(key=lambda r: str(r.get("occurred_at") or ""), reverse=True)
    return rows[:limit]


def recent(n: int = 20, **kwargs) -> list[dict]:
    return query(limit=n, **kwargs)


def stats() -> dict:
    """Counts by kind + overall health. Works online or off."""
    online = server_running()
    rows = query(limit=5000, offline=not online)
    by_kind: dict[str, int] = {}
    for row in rows:
        by_kind[row.get("kind") or "?"] = by_kind.get(row.get("kind") or "?", 0) + 1
    spool = _spool_read()
    return {
        "online": online,
        "base": PB_BASE,
        "db": str(PB_DB),
        "db_exists": PB_DB.exists(),
        "collection": COLLECTION,
        "entries": len(rows),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        "spool_pending": len(spool),
        "spool_path": str(SPOOL_PATH),
        "spool_dropped": spool_dropped(),
        "spool_caps": {"max_bytes": SPOOL_MAX_BYTES,
                       "max_entries": SPOOL_MAX_ENTRIES,
                       "max_age_days": SPOOL_MAX_AGE_DAYS},
        "oldest": rows[-1]["occurred_at"] if rows else None,
        "newest": rows[0]["occurred_at"] if rows else None,
    }


def doctor() -> dict:
    """What's wired, what's not, and what to run about it."""
    pb = PB()
    online = pb.is_online(recheck=True)
    checks = {
        "binary": {"ok": PB_BIN.exists(), "detail": str(PB_BIN),
                   "fix": "python3 build_os.py"},
        "migration": {"ok": (PB_MIGRATIONS / "1712270000_agent_journal.js").exists(),
                      "detail": str(PB_MIGRATIONS), "fix": "restore the migration file"},
        "server": {"ok": online, "detail": PB_BASE if online else pb.last_error,
                   "fix": "python3 pb_journal.py serve"},
        "collection": {"ok": pb.has_collection(COLLECTION) if online else _table_exists(),
                       "detail": COLLECTION if online else f"{COLLECTION} (checked in data.db)",
                       "fix": "python3 pb_journal.py serve  # applies pending migrations"},
        "spool_drained": {"ok": len(_spool_read()) == 0, "detail": str(SPOOL_PATH),
                          "fix": "python3 pb_journal.py sync"},
        # Anything here is memory the cap threw away because the store stayed
        # down too long. Not repairable after the fact, but it should be said
        # out loud rather than left as a silent gap in the record.
        "no_dropped_entries": {"ok": spool_dropped() == 0,
                               "detail": f"{spool_dropped()} entries dropped by the spool cap",
                               "fix": "keep PocketBase up (pb_journal.py launchd install); "
                                      f"reset the tally by deleting {_dropped_path()}"},
        # Not fatal on its own: the store works when started by hand. But an
        # unsupervised store is down more than it is up, and everything above
        # quietly degrades to spool only while it is.
        "supervised": {"ok": launchd_loaded(), "detail": LAUNCHD_LABEL,
                       "fix": "python3 pb_journal.py launchd install"},
    }
    checks["_healthy"] = all(c["ok"] for c in checks.values() if isinstance(c, dict))
    return checks


# ── CLI ───────────────────────────────────────────────────────────────

def _fmt_row(row: dict, width: int = 100) -> str:
    ts = str(row.get("occurred_at") or "")[:19].replace("T", " ")
    kind = (row.get("kind") or "?")[:10].ljust(10)
    tags = ",".join(_norm_tags(row.get("tags")))
    title = (row.get("title") or row.get("body") or "").replace("\n", " ")
    line = f"{ts}  {kind}  {title}"
    if len(line) > width:
        line = line[: width - 1] + "…"
    if tags:
        line += f"  [{tags}]"
    src = row.get("_source")
    if src == "spool":
        line += "  (unsynced)"
    return line


def _print_rows(rows: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return
    if not rows:
        print("(nothing matched)")
        return
    for row in rows:
        print(_fmt_row(row))
    print(f"\n{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")


def _resolve_since(value: str) -> str:
    """Accept '7d' / '24h' / an ISO date."""
    if not value:
        return ""
    val = value.strip()
    if val[-1:] in ("d", "h") and val[:-1].isdigit():
        delta = timedelta(days=int(val[:-1])) if val[-1] == "d" else timedelta(hours=int(val[:-1]))
        return (datetime.now(timezone.utc) - delta).isoformat()
    return val


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pb_journal",
        description="SimpleAgentOS's own PocketBase journal. Write it down, ask it back.",
    )
    parser.add_argument("--version", action="version", version=f"pb_journal {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="start the project's PocketBase (localhost)")
    p_serve.add_argument("--wait", type=float, default=10.0)

    sub.add_parser("stop", help="stop the PocketBase pb_journal started")

    p_launchd = sub.add_parser("launchd", help="keep PocketBase running via launchd (macOS)")
    p_launchd.add_argument("action", choices=("install", "uninstall", "status"))

    sub.add_parser("status", help="server + journal health")
    sub.add_parser("doctor", help="what's wired, what's broken, what to run")
    sub.add_parser("stats", help="entry counts by kind")

    p_sync = sub.add_parser("sync", help="drain the offline spool into PocketBase")
    p_sync.add_argument("-v", "--verbose", action="store_true")

    p_log = sub.add_parser("log", help="write a journal entry")
    p_log.add_argument("body", help="the thing to remember")
    p_log.add_argument("--kind", default="note", help=f"one of: {', '.join(sorted(KINDS))}")
    p_log.add_argument("--title", default="")
    p_log.add_argument("--tags", default="", help="comma-separated")
    p_log.add_argument("--actor", default="")
    p_log.add_argument("--source", default="cli")
    p_log.add_argument("--project", default="")
    p_log.add_argument("--session-id", default="")
    p_log.add_argument("--path-ref", default="")
    p_log.add_argument("--importance", type=float, default=0.5)
    p_log.add_argument("--metadata", default="", help="JSON object")

    p_query = sub.add_parser("query", help="search the journal")
    p_query.add_argument("text", nargs="?", default="")
    p_query.add_argument("--kind", default="")
    p_query.add_argument("--tags", default="")
    p_query.add_argument("--project", default="")
    p_query.add_argument("--session-id", default="")
    p_query.add_argument("--since", default="", help="ISO date, or 7d / 24h")
    p_query.add_argument("--until", default="")
    p_query.add_argument("-n", "--limit", type=int, default=50)
    p_query.add_argument("--offline", action="store_true", help="read data.db directly")

    p_recent = sub.add_parser("recent", help="most recent entries")
    p_recent.add_argument("-n", "--limit", type=int, default=20)
    p_recent.add_argument("--kind", default="")

    for p in (p_query, p_recent):
        p.add_argument("--json", action="store_true", dest="as_json")
    for p in (p_log,):
        p.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        return 0 if serve(wait=args.wait) else 1

    if args.cmd == "stop":
        return 0 if stop() else 1

    if args.cmd == "launchd":
        if args.action == "install":
            return 0 if launchd_install() else 1
        if args.action == "uninstall":
            return 0 if launchd_uninstall() else 1
        loaded = launchd_loaded()
        print(f"{LAUNCHD_LABEL}: {'loaded' if loaded else 'not loaded'}")
        print(f"  plist: {LAUNCHD_DEST} ({'present' if LAUNCHD_DEST.exists() else 'absent'})")
        print(f"  server: {'up' if server_running() else 'down'} at {PB_BASE}")
        return 0 if loaded else 1

    if args.cmd in ("status", "stats"):
        info = stats()
        print(json.dumps(info, indent=2, default=str))
        return 0

    if args.cmd == "doctor":
        report = doctor()
        for name, check in report.items():
            if name.startswith("_"):
                continue
            mark = "ok  " if check["ok"] else "FAIL"
            print(f"[{mark}] {name:<16} {check['detail']}")
            if not check["ok"]:
                print(f"         → {check['fix']}")
        print("\nhealthy" if report["_healthy"] else "\nneeds attention")
        return 0 if report["_healthy"] else 1

    if args.cmd == "sync":
        result = sync(verbose=args.verbose)
        print(json.dumps(result, indent=2))
        return 0 if result.get("remaining", 0) == 0 else 1

    if args.cmd == "log":
        metadata = {}
        if args.metadata:
            try:
                metadata = json.loads(args.metadata)
            except json.JSONDecodeError as exc:
                print(f"--metadata must be JSON: {exc}", file=sys.stderr)
                return 2
        entry = journal(
            args.body, kind=args.kind, title=args.title, tags=args.tags,
            actor=args.actor, source=args.source, project=args.project,
            session_id=args.session_id, path_ref=args.path_ref,
            importance=args.importance, metadata=metadata,
        )
        if getattr(args, "as_json", False):
            print(json.dumps(entry, indent=2, ensure_ascii=False, default=str))
        else:
            where = entry["_stored"]
            note = "" if where == "pocketbase" else "  (PocketBase offline, will sync)"
            print(f"logged {entry['entry_id']} → {where}{note}")
            if entry.get("_error"):
                print(f"  warn: {entry['_error']}", file=sys.stderr)
        return 0

    if args.cmd == "query":
        rows = query(
            args.text, kind=args.kind, tags=args.tags, project=args.project,
            session_id=args.session_id, since=_resolve_since(args.since),
            until=args.until, limit=args.limit, offline=args.offline,
        )
        _print_rows(rows, getattr(args, "as_json", False))
        return 0

    if args.cmd == "recent":
        rows = recent(args.limit, kind=args.kind)
        _print_rows(rows, getattr(args, "as_json", False))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
