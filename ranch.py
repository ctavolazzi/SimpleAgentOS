"""
ranch.py — The Ranch: orchestration layer for SimpleAgentOS.

Maps the frontier-to-railroad metaphor onto real infrastructure:

  WILD WEST    → RANCH          → TRAIN STATION
  ─────────────────────────────────────────────
  Wild Horse   → Stable         → Locomotive
  Lasso        → Corral         → Depot
  Cowboy       → Foreman        → Engineer
  Trail        → Trail Log      → Railroad Tie
  Campfire     → Watering Hole  → Telegraph

Each component is a real, working piece of infrastructure.
"""

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

import state_db as db
from brain import Brain, BrainResult


def _now():
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════
# STABLE — Model health checking and readiness validation
# "Don't ride a sick horse"
# ═══════════════════════════════════════════════════════════════════

class HorseStatus(Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    SICK = "sick"         # Responds but errors
    LAME = "lame"         # Too slow to be useful
    WILD = "wild"         # Untested
    RETIRED = "retired"   # Deliberately disabled


@dataclass
class StableEntry:
    """A horse in the stable — a registered model with health metadata."""
    backend_name: str
    status: HorseStatus = HorseStatus.WILD
    last_check: str | None = None
    avg_latency_ms: int = 0
    avg_tok_per_sec: float = 0.0
    total_rides: int = 0      # Total calls made
    total_bucks: int = 0      # Total errors
    reliability: float = 1.0  # success rate


class Stable:
    """Manages model health and readiness. The blacksmith tests each horse."""

    def __init__(self, brain: Brain, conn: sqlite3.Connection):
        self.brain = brain
        self.conn = conn
        self.horses: dict[str, StableEntry] = {}
        self._init_from_backends()

    def _init_from_backends(self):
        for b in self.brain.list_backends():
            name = b["name"]
            # Check trace history to initialize stats
            traces = self.brain.get_traces(limit=100, backend_name=name)
            total = len(traces)
            errors = sum(1 for t in traces if t.get("error"))
            avg_lat = sum(t.get("latency_ms", 0) for t in traces) / max(total, 1)

            self.horses[name] = StableEntry(
                backend_name=name,
                status=HorseStatus.HEALTHY if total > 0 and errors == 0 else HorseStatus.WILD,
                total_rides=total,
                total_bucks=errors,
                avg_latency_ms=int(avg_lat),
                reliability=1.0 - (errors / max(total, 1)),
            )

    async def check_horse(self, backend_name: str) -> StableEntry:
        """The blacksmith tests a horse — send a tiny probe and measure response."""
        entry = self.horses.get(backend_name) or StableEntry(backend_name=backend_name)

        try:
            result = await self.brain.call(
                backend_name,
                messages=[{"role": "user", "content": "Say OK"}],
                max_tokens=3,
                stream=False,
            )

            entry.last_check = _now()
            if result.error:
                entry.status = HorseStatus.SICK
                entry.total_bucks += 1
            elif result.latency_ms > 30000:  # > 30s for 3 tokens = lame
                entry.status = HorseStatus.LAME
            else:
                entry.status = HorseStatus.HEALTHY
                entry.avg_latency_ms = result.latency_ms

            entry.total_rides += 1
            entry.reliability = 1.0 - (entry.total_bucks / max(entry.total_rides, 1))

        except Exception as e:
            entry.status = HorseStatus.SICK
            entry.last_check = _now()
            entry.total_bucks += 1

        self.horses[backend_name] = entry
        return entry

    async def check_all(self) -> dict[str, StableEntry]:
        """Blacksmith rounds — test every horse in the stable."""
        for name in list(self.horses.keys()):
            await self.check_horse(name)
        return self.horses

    def get_best_horse(self, exclude: list[str] | None = None) -> str | None:
        """Pick the healthiest, fastest horse available."""
        candidates = [
            (name, h) for name, h in self.horses.items()
            if h.status == HorseStatus.HEALTHY
            and (not exclude or name not in exclude)
        ]
        if not candidates:
            return None
        # Sort by reliability desc, then latency asc
        candidates.sort(key=lambda x: (-x[1].reliability, x[1].avg_latency_ms))
        return candidates[0][0]

    def roster(self) -> list[dict]:
        """Full stable roster for the dashboard."""
        return [
            {
                "name": h.backend_name,
                "status": h.status.value,
                "last_check": h.last_check,
                "avg_latency_ms": h.avg_latency_ms,
                "total_rides": h.total_rides,
                "total_bucks": h.total_bucks,
                "reliability": round(h.reliability, 3),
            }
            for h in self.horses.values()
        ]


# ═══════════════════════════════════════════════════════════════════
# CORRAL — Capability sandboxing and execution boundaries
# "The fence that keeps the horse from trampling the garden"
# ═══════════════════════════════════════════════════════════════════

class Corral:
    """Enforces capability boundaries. Before any action, check the corral."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def is_allowed(self, capability_id: str) -> bool:
        """Check if a capability is enabled."""
        caps = db.get_capabilities(self.conn)
        for c in caps:
            if c["id"] == capability_id:
                return c["enabled"]
        return False

    def gate(self, capability_id: str) -> bool:
        """Gate check — returns True if allowed, logs if blocked."""
        allowed = self.is_allowed(capability_id)
        if not allowed:
            print(f"  [CORRAL] Blocked: {capability_id} (disabled)", flush=True)
        return allowed

    def all_enabled(self) -> list[str]:
        """List all enabled capability IDs."""
        caps = db.get_capabilities(self.conn)
        return [c["id"] for c in caps if c["enabled"]]


# ═══════════════════════════════════════════════════════════════════
# TRAIL LOG — Structured record of the path taken
# "Every step on the trail gets marked"
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TrailMarker:
    """A single point on the trail — an action the system took."""
    action: str           # what happened
    actor: str            # who did it (cowboy name / agent id)
    target: str | None    # what it was done to (file, model, etc)
    result_hash: str      # hash of the output
    backend_used: str | None  # which horse was ridden
    trace_id: str | None  # link to brain trace
    duration_ms: int
    timestamp: str = field(default_factory=_now)


class TrailLog:
    """Records the full journey. Every action is a marker on the trail."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._ensure_table()

    def _ensure_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trail_log (
                id          TEXT PRIMARY KEY,
                session_id  TEXT,
                action      TEXT NOT NULL,
                actor       TEXT NOT NULL,
                target      TEXT,
                result_hash TEXT,
                backend_used TEXT,
                trace_id    TEXT,
                duration_ms INTEGER DEFAULT 0,
                metadata_json TEXT DEFAULT '{}',
                created_at  TEXT NOT NULL
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trail_session ON trail_log(session_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trail_action ON trail_log(action)")
        self.conn.commit()

    def mark(self, session_id: str | None, marker: TrailMarker, metadata: dict | None = None) -> str:
        import uuid
        mid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO trail_log (id, session_id, action, actor, target, result_hash, backend_used, trace_id, duration_ms, metadata_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mid, session_id, marker.action, marker.actor, marker.target,
             marker.result_hash, marker.backend_used, marker.trace_id,
             marker.duration_ms, json.dumps(metadata or {}), marker.timestamp)
        )
        self.conn.commit()
        return mid

    def get_trail(self, session_id: str | None = None, limit: int = 50) -> list[dict]:
        if session_id:
            rows = self.conn.execute(
                "SELECT * FROM trail_log WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trail_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def trail_stats(self) -> dict:
        """How far have we ridden?"""
        total = self.conn.execute("SELECT COUNT(*) as c FROM trail_log").fetchone()["c"]
        actions = self.conn.execute(
            "SELECT action, COUNT(*) as c FROM trail_log GROUP BY action ORDER BY c DESC"
        ).fetchall()
        return {
            "total_markers": total,
            "actions": {r["action"]: r["c"] for r in actions},
        }


# ═══════════════════════════════════════════════════════════════════
# FOREMAN — The orchestrator (bridge from ranch to train station)
# "Assigns cowboys to horses to jobs"
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Job:
    """A unit of work to be done."""
    id: str
    name: str
    description: str
    capability_required: str
    input_data: dict
    priority: int = 0       # higher = more urgent
    status: str = "queued"  # queued, running, complete, failed
    assigned_backend: str | None = None
    result: str | None = None
    trace_id: str | None = None
    created_at: str = field(default_factory=_now)


class Foreman:
    """
    The Foreman assigns jobs to the best available horse.
    This is the bridge from cowboy work to engineering:
    - Checks corral (is the capability allowed?)
    - Checks stable (is a horse healthy?)
    - Assigns job to best available backend
    - Records everything on the trail log
    """

    def __init__(self, brain: Brain, stable: Stable, corral: Corral,
                 trail: TrailLog, conn: sqlite3.Connection):
        self.brain = brain
        self.stable = stable
        self.corral = corral
        self.trail = trail
        self.conn = conn
        self.job_queue: list[Job] = []

    def submit_job(self, job: Job) -> str:
        """Submit a job to the foreman's queue."""
        self.job_queue.append(job)
        self.job_queue.sort(key=lambda j: -j.priority)
        print(f"  [FOREMAN] Job submitted: {job.name} (cap={job.capability_required})", flush=True)
        return job.id

    async def execute_job(self, job: Job, session_id: str | None = None,
                          on_token=None) -> Job:
        """Execute a single job through the full ranch pipeline."""
        import time, hashlib

        # 1. Corral check
        if not self.corral.gate(job.capability_required):
            job.status = "failed"
            job.result = f"Capability '{job.capability_required}' is disabled"
            return job

        # 2. Pick the best horse (or use pre-assigned)
        backend = job.assigned_backend or self.stable.get_best_horse()
        if not backend:
            job.status = "failed"
            job.result = "No healthy backend available"
            return job

        job.assigned_backend = backend
        job.status = "running"

        # 3. Ride
        t0 = time.monotonic()
        messages = job.input_data.get("messages", [])
        max_tokens = job.input_data.get("max_tokens", 256)

        result = await self.brain.call(
            backend, messages, max_tokens=max_tokens,
            session_id=session_id, stream=True, on_token=on_token
        )

        duration = int((time.monotonic() - t0) * 1000)

        # 4. Record on trail
        self.trail.mark(session_id, TrailMarker(
            action=job.name,
            actor="foreman",
            target=job.input_data.get("file"),
            result_hash=result.content_hash,
            backend_used=backend,
            trace_id=result.trace_id,
            duration_ms=duration,
        ))

        # 5. Update horse stats
        horse = self.stable.horses.get(backend)
        if horse:
            horse.total_rides += 1
            if result.error:
                horse.total_bucks += 1
            horse.reliability = 1.0 - (horse.total_bucks / max(horse.total_rides, 1))

        # 6. Finalize
        job.status = "complete" if not result.error else "failed"
        job.result = result.text
        job.trace_id = result.trace_id
        return job

    async def run_queue(self, session_id: str | None = None):
        """Process all queued jobs."""
        while self.job_queue:
            job = self.job_queue.pop(0)
            await self.execute_job(job, session_id)

    def queue_status(self) -> dict:
        return {
            "queued": len(self.job_queue),
            "jobs": [{"id": j.id, "name": j.name, "status": j.status, "priority": j.priority}
                     for j in self.job_queue],
        }


# ═══════════════════════════════════════════════════════════════════
# RANCH — The full assembly (wraps everything together)
# ═══════════════════════════════════════════════════════════════════

class Ranch:
    """
    The Ranch is the complete working system.

    Usage:
        ranch = Ranch(conn)
        ranch.brain.register("local-gemma4", ...)
        await ranch.stable.check_all()
        job = Job(id="1", name="analyze", capability_required="analyze",
                  input_data={"messages": [...]})
        result = await ranch.foreman.execute_job(job)
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.brain = Brain(conn)
        self.stable = Stable(self.brain, conn)
        self.corral = Corral(conn)
        self.trail = TrailLog(conn)
        self.foreman = Foreman(self.brain, self.stable, self.corral, self.trail, conn)

    def full_status(self) -> dict:
        return {
            "stable": self.stable.roster(),
            "corral": self.corral.all_enabled(),
            "trail": self.trail.trail_stats(),
            "queue": self.foreman.queue_status(),
            "brain_usage": self.brain.get_usage_summary(),
        }
