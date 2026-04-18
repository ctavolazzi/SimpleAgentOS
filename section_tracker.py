"""
section_tracker.py — Persistent telemetry for Daily Note Harness operations.

Dual-writes every daily-note event to:
  1. PocketBase  — canonical live store at http://127.0.0.1:8090
  2. JSONL mirror — append-only log under the private vault repo

JSONL-first: if PocketBase is offline, JSONL still records. Catchup sync on
reconnect. Schema versioned (daily_ops/v1) for forward compatibility.

Zero external deps (stdlib only). Privacy: localhost + private vault repo only.

See: ~/Documents/Personal-Remote-Vault/2026-04-17_Section_Ops_Schema.md
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Paths + constants ─────────────────────────────────────────────────

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
JSONL_ROOT = VAULT_DIR / "System" / "40-49_telemetry" / "daily_ops"
SCHEMA_VERSION = "daily_ops/v1"

PB_BASE = os.environ.get("PB_BASE", "http://127.0.0.1:8090")
PB_ADMIN_EMAIL = os.environ.get("PB_ADMIN_EMAIL", "")
PB_ADMIN_PASSWORD = os.environ.get("PB_ADMIN_PASSWORD", "")
PB_TIMEOUT = 2.0  # seconds — keep short, offline path is fine

# Coalesce rapid writes to same (session, section) within this window. Prevents
# keystroke-level flooding: last hash wins, durations sum, byte-count recomputed.
DEBOUNCE_SECONDS = float(os.environ.get("DNH_DEBOUNCE_SECONDS", "5.0"))
# Hard per-minute ceiling per (session, section). Backstop for runaway loops.
MAX_OPS_PER_MINUTE_PER_SECTION = int(os.environ.get("DNH_MAX_OPS_PER_MINUTE", "30"))
PREVIEW_MAX_CHARS = 500
SUPPORTED_SCHEMAS = {"daily_ops/v1"}

# Secret patterns to scrub from previews before PB insert. Vault privacy is
# the real defense; this is second layer if PB data ever leaves the machine.
_SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-<redacted>"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "sk-<redacted>"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "ghp_<redacted>"),
    (re.compile(r"gho_[A-Za-z0-9]{30,}"), "gho_<redacted>"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"), "xox<redacted>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA<redacted>"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S{8,}"),
     r"\1=<redacted>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"),
     "<jwt-redacted>"),
]


def _scrub(text: str) -> str:
    """Redact common secret patterns from preview. Best-effort second layer."""
    if not text:
        return text
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out

# ── Enums (validated against these) ───────────────────────────────────

ACTORS = {"claude", "gemma", "user", "waft-daemon", "cron", "test", "system"}
MODELS = {"opus-4-7", "sonnet-4-6", "haiku-4-5", "gemma-2b", "gemma-4", "n/a"}
OPERATIONS = {"read", "write", "replace", "append", "skip", "overwrite",
              "extract", "frontmatter_update", "session_start", "session_end"}
SOURCES = {"spin_up", "wrap_up", "checkpoint", "manual", "test", "harness", "api"}
RESULTS = {"ok", "permission_error", "fs_error", "regex_error",
           "validation_error", "network_error"}
EXPERIMENT_STATUS = {"proposed", "running", "concluded", "abandoned"}
LINK_RELATIONS = {"expansion", "reference", "experiment", "recap", "architecture"}

# Known section names come from daily_note.py — import if available, else fallback
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from daily_note import SECTIONS as _SECTIONS
    KNOWN_SECTIONS = set(_SECTIONS.keys())
except Exception:
    KNOWN_SECTIONS = set()  # permissive if import fails (tests can mock)


# ── Helpers ───────────────────────────────────────────────────────────

# Monotonic counter to disambiguate ops that land the same microsecond.
_op_counter = 0
_op_counter_lock = threading.Lock()


def _next_op_counter() -> int:
    global _op_counter
    with _op_counter_lock:
        _op_counter += 1
        return _op_counter


def _now_iso() -> str:
    # isoformat includes microseconds; combined with monotonic counter in op_id
    # this eliminates practical collision risk.
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _uuid() -> str:
    return str(uuid.uuid4())


def _jsonl_path(note_date: Optional[str] = None) -> Path:
    d = note_date or _today_str()
    year_month = d[:7]
    return JSONL_ROOT / year_month / f"{d}.jsonl"


def _ensure_jsonl_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _jsonl_append(path: Path, record: dict) -> None:
    """Atomic-ish single-line append."""
    _ensure_jsonl_dir(path)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Validation ────────────────────────────────────────────────────────

class ValidationError(ValueError):
    pass


def _require(value: Any, name: str) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"{name} is required")


def _enum(value: str, allowed: set, name: str, permissive: bool = False) -> None:
    if value not in allowed:
        if permissive:
            return  # caller opted into lax mode
        raise ValidationError(f"{name}={value!r} not in {sorted(allowed)}")


def _is_filled(content: str) -> bool:
    """Heuristic: stripped content > 10 chars and not just template markers."""
    stripped = (content or "").strip()
    if len(stripped) < 10:
        return False
    # cheap template check
    for marker in ("TBD", "TODO", "None", "[[", "**Status:**"):
        if stripped == marker:
            return False
    return True


def _op_id(session_id: str, section_name: str, operation: str,
           occurred_at: str, counter: Optional[int] = None) -> str:
    """Deterministic op id. Counter disambiguates same-microsecond ops."""
    suffix = f"|{counter}" if counter is not None else ""
    return _hash(f"{session_id}|{section_name}|{operation}|{occurred_at}{suffix}")


# ── PocketBase thin HTTP client ───────────────────────────────────────

class PocketBaseClient:
    """Minimal PB client — no third-party deps. Best-effort; silent on failure."""

    def __init__(self, base: str = PB_BASE):
        self.base = base.rstrip("/")
        self._token: Optional[str] = None
        self._online: Optional[bool] = None  # lazy probe

    def is_online(self) -> bool:
        if self._online is not None:
            return self._online
        try:
            req = urllib.request.Request(f"{self.base}/api/health")
            with urllib.request.urlopen(req, timeout=PB_TIMEOUT) as resp:
                self._online = resp.status == 200
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
            self._online = False
        return self._online

    def _auth(self) -> Optional[str]:
        if self._token:
            return self._token
        if not (PB_ADMIN_EMAIL and PB_ADMIN_PASSWORD):
            return None
        try:
            body = json.dumps({
                "identity": PB_ADMIN_EMAIL,
                "password": PB_ADMIN_PASSWORD,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base}/api/admins/auth-with-password",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=PB_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self._token = data.get("token")
                return self._token
        except Exception:
            return None

    def create(self, collection: str, record: dict) -> Optional[dict]:
        if not self.is_online():
            return None
        try:
            body = json.dumps(record, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            token = self._auth()
            if token:
                headers["Authorization"] = token
            req = urllib.request.Request(
                f"{self.base}/api/collections/{collection}/records",
                data=body, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=PB_TIMEOUT) as resp:
                if 200 <= resp.status < 300:
                    return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # surface 4xx for caller debugging via attribute
            self._last_error = f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
            return None
        except Exception as e:
            self._last_error = str(e)
            return None
        return None


# ── Records ───────────────────────────────────────────────────────────

@dataclass
class SessionRecord:
    session_id: str
    actor: str
    model: str
    started_at: str
    ended_at: Optional[str] = None
    project_root: str = ""
    git_branch: str = ""
    commit_count: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class SnapshotRecord:
    note_date: str
    section_name: str
    content_hash: str
    content_preview: str
    word_count: int
    filled: bool
    captured_at: str
    session_id: str = ""


@dataclass
class OpRecord:
    op_id: str
    session_id: str
    note_date: str
    section_name: str
    operation: str
    actor: str
    source: str
    occurred_at: str
    result: str
    before_hash: str = ""
    after_hash: str = ""
    bytes_written: int = 0
    duration_ms: int = 0
    error_message: str = ""
    linked_doc_path: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class ExperimentRecord:
    experiment_id: str
    title: str
    hypothesis: str
    status: str
    started_at: str
    prediction: str = ""
    method: str = ""
    result: str = ""
    concluded_at: Optional[str] = None
    linked_note: str = ""
    linked_sections: list = field(default_factory=list)
    tags: list = field(default_factory=list)


@dataclass
class LinkedDocRecord:
    parent_note: str
    parent_section: str
    child_path: str
    relationship: str
    created_at: str
    summary: str = ""


# ── The tracker ───────────────────────────────────────────────────────

@dataclass
class _PendingOp:
    """In-buffer op awaiting flush. Fields accumulate across coalesced writes."""
    section: str
    operation: str
    before: str
    after: str
    first_seen: float   # monotonic
    last_seen: float    # monotonic
    occurred_at: str    # wall-clock ISO (of FIRST op in the burst)
    counter: int
    result: str
    error_message: str
    linked_doc_path: str
    duration_ms: int
    note_date: Optional[str]
    metadata: dict
    coalesced_count: int = 1


class OpRecorder:
    """
    Main facade. Dual-writes to JSONL + PocketBase with defense-in-depth.

    Preventable-error guards:
      - Debounce window (DEBOUNCE_SECONDS): coalesces rapid rewrites to same
        (session, section) into a single op. Prevents keystroke-flood.
      - Rate limit (MAX_OPS_PER_MINUTE_PER_SECTION): drops excess ops after
        ceiling, records counter in metadata.rate_limited.
      - Snapshot dedup: skips snapshot insert if hash unchanged from last one
        for the same (note_date, section).
      - Secret scrub: previews sanitized before PB insert.
      - atexit flush: pending coalesced ops + session_end written on interpreter exit.
      - op_id counter: eliminates collision risk from same-microsecond writes.

    Usage:
        rec = OpRecorder(actor="claude", model="opus-4-7", source="manual")
        sid = rec.start_session()
        rec.record_op(section="sitrep", operation="write", before="...", after="...")
        rec.end_session()  # flushes buffer + writes session_end envelope
    """

    def __init__(
        self,
        actor: str = "claude",
        model: str = "n/a",
        source: str = "manual",
        project_root: str = "",
        git_branch: str = "",
        pb: Optional[PocketBaseClient] = None,
        strict_sections: bool = False,
        jsonl_root: Optional[Path] = None,
        debounce_seconds: Optional[float] = None,
        max_ops_per_minute: Optional[int] = None,
        install_atexit: bool = True,
    ):
        _enum(actor, ACTORS, "actor")
        _enum(model, MODELS, "model")
        _enum(source, SOURCES, "source")
        self.actor = actor
        self.model = model
        self.source = source
        self.project_root = project_root or str(Path.cwd())
        self.git_branch = git_branch
        self.pb = pb or PocketBaseClient()
        self.strict_sections = strict_sections
        self._jsonl_root = jsonl_root or JSONL_ROOT
        self.session_id: Optional[str] = None
        self._started_at: Optional[str] = None
        # ── Hardening state ──
        self._debounce = debounce_seconds if debounce_seconds is not None else DEBOUNCE_SECONDS
        self._max_per_min = max_ops_per_minute if max_ops_per_minute is not None else MAX_OPS_PER_MINUTE_PER_SECTION
        self._buffer: dict[tuple, _PendingOp] = {}
        self._buffer_lock = threading.Lock()
        self._rate_windows: dict[tuple, deque] = {}  # (session, section) -> monotonic timestamps
        self._last_snapshot_hash: dict[tuple, str] = {}  # (note_date, section) -> hash
        if install_atexit:
            atexit.register(self._atexit_flush)

    def _jsonl_path(self, note_date: Optional[str] = None) -> Path:
        d = note_date or _today_str()
        return self._jsonl_root / d[:7] / f"{d}.jsonl"

    def _write(self, kind: str, record: dict, pb_collection: str, note_date: Optional[str] = None) -> dict:
        envelope = {"schema": SCHEMA_VERSION, "kind": kind, "ts": _now_iso(), "data": record}
        _jsonl_append(self._jsonl_path(note_date), envelope)
        pb_result = self.pb.create(pb_collection, record)
        return {
            "jsonl": str(self._jsonl_path(note_date)),
            "pb_id": (pb_result or {}).get("id"),
            "pb_online": self.pb.is_online(),
        }

    # ── Sessions ──────────────────────────────────────────────────────

    def start_session(self, session_id: Optional[str] = None,
                      metadata: Optional[dict] = None) -> str:
        self.session_id = session_id or _uuid()
        self._started_at = _now_iso()
        rec = SessionRecord(
            session_id=self.session_id,
            actor=self.actor,
            model=self.model,
            started_at=self._started_at,
            project_root=self.project_root,
            git_branch=self.git_branch,
            metadata=metadata or {"source": self.source},
        )
        self._write("session_start", asdict(rec), "daily_sessions")
        return self.session_id

    def end_session(self, commit_count: int = 0, metadata: Optional[dict] = None) -> None:
        if not self.session_id:
            raise ValidationError("no active session")
        # Flush any buffered ops BEFORE recording end so the session's record
        # is complete. Otherwise coalesced bursts leak past the boundary.
        self.flush()
        ended = _now_iso()
        rec = SessionRecord(
            session_id=self.session_id,
            actor=self.actor,
            model=self.model,
            started_at=self._started_at or ended,
            ended_at=ended,
            project_root=self.project_root,
            git_branch=self.git_branch,
            commit_count=commit_count,
            metadata=metadata or {},
        )
        envelope = asdict(rec)
        _jsonl_append(self._jsonl_path(),
                      {"schema": SCHEMA_VERSION, "kind": "session_end",
                       "ts": ended, "data": envelope})
        # Idempotent: subsequent end_session calls are no-ops
        self.session_id = None
        self._started_at = None

    # ── Ops ───────────────────────────────────────────────────────────

    # ── Rate limit ────────────────────────────────────────────────────

    def _rate_allow(self, section: str) -> bool:
        """Sliding 60s window per (session, section). Drop when saturated."""
        key = (self.session_id, section)
        window = self._rate_windows.setdefault(key, deque())
        now_m = time.monotonic()
        # evict stale
        while window and now_m - window[0] > 60.0:
            window.popleft()
        if len(window) >= self._max_per_min:
            return False
        window.append(now_m)
        return True

    # ── Coalescing buffer ─────────────────────────────────────────────

    def _flush_pending(self, key: tuple) -> Optional[dict]:
        """Emit buffered op for key. Caller holds _buffer_lock."""
        pending = self._buffer.pop(key, None)
        if pending is None:
            return None
        after_h = _hash(pending.after) if pending.after else ""
        before_h = _hash(pending.before) if pending.before else ""
        operation = pending.operation
        if operation == "write" and before_h and after_h and before_h == after_h:
            operation = "skip"
        meta = dict(pending.metadata or {})
        if pending.coalesced_count > 1:
            meta["coalesced_count"] = pending.coalesced_count
            meta["burst_span_ms"] = int((pending.last_seen - pending.first_seen) * 1000)
        op = OpRecord(
            op_id=_op_id(self.session_id, pending.section, operation,
                         pending.occurred_at, counter=pending.counter),
            session_id=self.session_id,
            note_date=pending.note_date or _today_str(),
            section_name=pending.section,
            operation=operation,
            actor=self.actor,
            source=self.source,
            occurred_at=pending.occurred_at,
            result=pending.result,
            before_hash=before_h,
            after_hash=after_h,
            bytes_written=len(pending.after.encode("utf-8")) if pending.after else 0,
            duration_ms=pending.duration_ms,
            error_message=pending.error_message,
            linked_doc_path=pending.linked_doc_path,
            metadata=meta,
        )
        return self._write("op", asdict(op), "section_operations", pending.note_date)

    def _flush_expired(self) -> list[dict]:
        """Flush any buffered ops older than debounce window."""
        results = []
        now_m = time.monotonic()
        with self._buffer_lock:
            expired = [k for k, p in self._buffer.items()
                       if now_m - p.last_seen >= self._debounce]
            for k in expired:
                r = self._flush_pending(k)
                if r is not None:
                    results.append(r)
        return results

    def flush(self) -> list[dict]:
        """Flush ALL pending ops regardless of debounce. Call on session_end."""
        results = []
        with self._buffer_lock:
            for k in list(self._buffer.keys()):
                r = self._flush_pending(k)
                if r is not None:
                    results.append(r)
        return results

    def _atexit_flush(self) -> None:
        """Best-effort on interpreter exit: flush buffer + end session if open."""
        try:
            self.flush()
            if self.session_id and self._started_at:
                # Only emit session_end if not already ended
                self.end_session(metadata={"source": "atexit"})
        except Exception:
            pass

    # ── Ops ───────────────────────────────────────────────────────────

    def record_op(
        self,
        section: str,
        operation: str,
        before: str = "",
        after: str = "",
        result: str = "ok",
        error_message: str = "",
        linked_doc_path: str = "",
        duration_ms: int = 0,
        note_date: Optional[str] = None,
        metadata: Optional[dict] = None,
        coalesce: bool = True,
    ) -> Optional[dict]:
        """
        Record an op. With coalesce=True (default), rapid writes to the same
        (session, section) within DEBOUNCE_SECONDS are merged: later `after`
        supersedes, durations summed, coalesced_count tracked.

        Errors and non-write operations always flush immediately.
        Returns the write-result dict if flushed now, else None (buffered).
        """
        if not self.session_id:
            self.start_session()
        _require(section, "section")
        _enum(operation, OPERATIONS, "operation")
        _enum(result, RESULTS, "result")
        if self.strict_sections and KNOWN_SECTIONS:
            _enum(section, KNOWN_SECTIONS, "section")

        # Rate limit — annotates saturation pressure without bypassing the
        # coalescing buffer. Coalescing is the primary throttle; this flag
        # surfaces in the merged op's metadata so downstream sees the spike.
        if not self._rate_allow(section):
            md = dict(metadata or {})
            md["rate_limited"] = True
            metadata = md

        # Errors and non-write ops bypass coalescing — always flush immediately
        # so failures are never hidden in a buffer. Debounce<=0 disables buffering
        # entirely (useful for tests and explicit flush-every-write mode).
        force_flush = (
            result != "ok"
            or operation not in {"write", "replace", "append", "overwrite"}
            or not coalesce
            or self._debounce <= 0
        )

        key = (self.session_id, section)
        occurred = _now_iso()
        now_m = time.monotonic()
        counter = _next_op_counter()

        if not force_flush:
            with self._buffer_lock:
                existing = self._buffer.get(key)
                if existing is not None and (now_m - existing.last_seen) < self._debounce:
                    # Coalesce: keep original `before` + occurred_at + counter,
                    # replace `after`, sum durations, bump count.
                    existing.after = after
                    existing.last_seen = now_m
                    existing.duration_ms += duration_ms
                    existing.coalesced_count += 1
                    if metadata:
                        existing.metadata.update(metadata)
                    return None  # buffered
                # New burst starts here
                self._buffer[key] = _PendingOp(
                    section=section,
                    operation=operation,
                    before=before,
                    after=after,
                    first_seen=now_m,
                    last_seen=now_m,
                    occurred_at=occurred,
                    counter=counter,
                    result=result,
                    error_message=error_message,
                    linked_doc_path=linked_doc_path,
                    duration_ms=duration_ms,
                    note_date=note_date,
                    metadata=dict(metadata or {}),
                )
            # Opportunistic flush of other expired buffers
            self._flush_expired()
            return None  # buffered

        # Immediate write path (error, non-write op, or coalesce=False)
        # Flush any pending buffer for this key first to preserve ordering
        with self._buffer_lock:
            self._flush_pending(key)

        before_h = _hash(before) if before else ""
        after_h = _hash(after) if after else ""
        if operation == "write" and before_h and after_h and before_h == after_h:
            operation = "skip"

        op = OpRecord(
            op_id=_op_id(self.session_id, section, operation, occurred, counter=counter),
            session_id=self.session_id,
            note_date=note_date or _today_str(),
            section_name=section,
            operation=operation,
            actor=self.actor,
            source=self.source,
            occurred_at=occurred,
            result=result,
            before_hash=before_h,
            after_hash=after_h,
            bytes_written=len(after.encode("utf-8")) if after else 0,
            duration_ms=duration_ms,
            error_message=error_message,
            linked_doc_path=linked_doc_path,
            metadata=metadata or {},
        )
        return self._write("op", asdict(op), "section_operations", note_date)

    # ── Snapshots ─────────────────────────────────────────────────────

    def record_snapshot(self, section: str, content: str,
                        note_date: Optional[str] = None,
                        force: bool = False) -> Optional[dict]:
        """
        Record a section snapshot. Dedupes against last recorded hash for
        (note_date, section) unless force=True — prevents snapshot churn when
        called repeatedly with unchanged content.
        """
        _require(section, "section")
        d = note_date or _today_str()
        h = _hash(content)
        key = (d, section)
        if not force and self._last_snapshot_hash.get(key) == h:
            return None  # deduped
        self._last_snapshot_hash[key] = h
        # Scrub preview before persisting — vault privacy is primary defense,
        # this is a belt-and-suspenders measure for PB/export paths.
        preview = _scrub(content[:PREVIEW_MAX_CHARS])
        snap = SnapshotRecord(
            note_date=d,
            section_name=section,
            content_hash=h,
            content_preview=preview,
            word_count=len(content.split()),
            filled=_is_filled(content),
            captured_at=_now_iso(),
            session_id=self.session_id or "",
        )
        return self._write("snapshot", asdict(snap), "section_snapshots", note_date)

    # ── Experiments ───────────────────────────────────────────────────

    def record_experiment(
        self,
        experiment_id: str,
        title: str,
        hypothesis: str,
        status: str = "proposed",
        prediction: str = "",
        method: str = "",
        linked_note: str = "",
        linked_sections: Optional[list] = None,
        tags: Optional[list] = None,
        allow_duplicate: bool = False,
    ) -> Optional[dict]:
        """
        Register an experiment. Dedupes by experiment_id within the local
        JSONL (any day) unless allow_duplicate=True. PB unique index is the
        strong guarantee; this is the JSONL-layer parallel so we don't emit
        duplicate envelopes while offline.
        """
        _require(experiment_id, "experiment_id")
        _require(title, "title")
        _require(hypothesis, "hypothesis")
        _enum(status, EXPERIMENT_STATUS, "status")
        if not allow_duplicate:
            # Check current day and yesterday for duplicate id (cheap scan).
            for d in (_today_str(), None):
                for rec in read_jsonl(d, kind="experiment", jsonl_root=self._jsonl_root):
                    if rec.get("data", {}).get("experiment_id") == experiment_id:
                        return None  # already registered
        exp = ExperimentRecord(
            experiment_id=experiment_id,
            title=title,
            hypothesis=hypothesis,
            status=status,
            started_at=_now_iso(),
            prediction=prediction,
            method=method,
            linked_note=linked_note or _today_str(),
            linked_sections=linked_sections or [],
            tags=tags or [],
        )
        return self._write("experiment", asdict(exp), "experiments")

    # ── Linked docs ───────────────────────────────────────────────────

    def record_linked_doc(
        self,
        parent_section: str,
        child_path: str,
        relationship: str = "expansion",
        summary: str = "",
        parent_note: Optional[str] = None,
    ) -> dict:
        _require(parent_section, "parent_section")
        _require(child_path, "child_path")
        _enum(relationship, LINK_RELATIONS, "relationship")
        link = LinkedDocRecord(
            parent_note=parent_note or _today_str(),
            parent_section=parent_section,
            child_path=child_path,
            relationship=relationship,
            created_at=_now_iso(),
            summary=summary,
        )
        return self._write("linked_doc", asdict(link), "linked_docs")


# ── Query helpers (local JSONL only — PB queries go through its own API) ──

def read_jsonl(note_date: Optional[str] = None, kind: Optional[str] = None,
               jsonl_root: Optional[Path] = None,
               include_unknown_schema: bool = False) -> list[dict]:
    """
    Read one day's JSONL, optionally filtered by kind.

    By default, records with a schema not in SUPPORTED_SCHEMAS are skipped —
    prevents a future schema change from crashing the reader. Set
    include_unknown_schema=True to see them anyway (for migration tooling).
    Malformed lines (bad JSON, truncated tail) are silently dropped.
    """
    root = jsonl_root or JSONL_ROOT
    d = note_date or _today_str()
    path = root / d[:7] / f"{d}.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # partial/truncated tail
        if not include_unknown_schema:
            schema = rec.get("schema")
            if schema and schema not in SUPPORTED_SCHEMAS:
                continue
        if kind and rec.get("kind") != kind:
            continue
        out.append(rec)
    return out


def stats(note_date: Optional[str] = None,
          jsonl_root: Optional[Path] = None) -> dict:
    """Local aggregate (no PB required)."""
    records = read_jsonl(note_date, jsonl_root=jsonl_root)
    ops = [r for r in records if r.get("kind") == "op"]
    sections: dict[str, int] = {}
    actors: dict[str, int] = {}
    results: dict[str, int] = {}
    for r in ops:
        d = r["data"]
        sections[d["section_name"]] = sections.get(d["section_name"], 0) + 1
        actors[d["actor"]] = actors.get(d["actor"], 0) + 1
        results[d["result"]] = results.get(d["result"], 0) + 1
    return {
        "date": note_date or _today_str(),
        "total_records": len(records),
        "total_ops": len(ops),
        "sessions": sum(1 for r in records if r.get("kind") == "session_start"),
        "experiments": sum(1 for r in records if r.get("kind") == "experiment"),
        "linked_docs": sum(1 for r in records if r.get("kind") == "linked_doc"),
        "snapshots": sum(1 for r in records if r.get("kind") == "snapshot"),
        "sections": sections,
        "actors": actors,
        "results": results,
    }


# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Section Ops Tracker CLI")
    p.add_argument("cmd", choices=["stats", "tail", "probe"])
    p.add_argument("--date", default=None)
    p.add_argument("--limit", type=int, default=20)
    args = p.parse_args()

    if args.cmd == "stats":
        print(json.dumps(stats(args.date), indent=2))
    elif args.cmd == "tail":
        records = read_jsonl(args.date)[-args.limit:]
        for r in records:
            print(json.dumps(r))
    elif args.cmd == "probe":
        pb = PocketBaseClient()
        print(json.dumps({
            "pb_online": pb.is_online(),
            "pb_base": PB_BASE,
            "jsonl_root": str(JSONL_ROOT),
            "jsonl_today_path": str(_jsonl_path()),
            "jsonl_today_exists": _jsonl_path().is_file(),
            "known_sections": sorted(KNOWN_SECTIONS),
        }, indent=2))
