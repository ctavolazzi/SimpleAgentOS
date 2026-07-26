"""
harness_log.py — Observability bridge for daily_note.py operations.

Logs every harness operation (write, extract, replace) to the existing
trail_log table in state.db via ranch.py's TrailLog class.

This module is optional — daily_note.py tries to import it, and if
unavailable (e.g., standalone use), logging is silently skipped.
daily_note.py remains zero-dependency.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure SimpleAgentOS modules are importable
_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from ranch import TrailLog, TrailMarker
from state_db import get_conn, _hash

_trail = None
_recorder = None  # section_tracker.OpRecorder singleton (or False if init failed)

# Map harness_log action names → section_tracker OPERATIONS set
_ACTION_TO_OP = {
    "write_section":      "write",
    "extract_section":    "extract",
    "replace_section":    "replace",
    "append_session_log": "append",
    "update_frontmatter": "frontmatter_update",
}


def _get_trail():
    """Lazy-init the TrailLog connection."""
    global _trail
    if _trail is None:
        _trail = TrailLog(get_conn())
    return _trail


def _get_recorder():
    """Lazy-init the section_tracker OpRecorder singleton. Best-effort.

    Returns None if section_tracker is unavailable or init fails — the bridge
    is strictly additive and must never block the primary trail_log path.
    """
    global _recorder
    if _recorder is False:
        return None
    if _recorder is None:
        try:
            import section_tracker as st
            _recorder = st.OpRecorder(install_atexit=True)
            _recorder.start_session()
        except Exception:
            _recorder = False
            return None
    return _recorder


def _now():
    return datetime.now(timezone.utc).isoformat()


def log_op(action, actor, target, result, content=None,
           duration_ms=0, command=None, error=None):
    """
    Log a harness operation to trail_log.

    Args:
        action: "write_section", "extract_section", "replace_section",
                "append_session_log", "update_frontmatter"
        actor: "claude", "gemma", "waft-daemon", "system"
        target: section name or file path
        result: "ok", "regex_error", "fs_error", "permission_error"
        content: the content written (used for byte count + hash)
        duration_ms: how long the operation took
        command: which slash command triggered this ("/checkpoint", etc.)
        error: error message string if result != "ok"
    """
    metadata = {"result": result}
    if command:
        metadata["command"] = command
    if error:
        metadata["error"] = error
    if content:
        metadata["bytes"] = len(content)

    marker = TrailMarker(
        action=action,
        actor=actor,
        target=target,
        result_hash=_hash(content) if content else None,
        backend_used=None,
        trace_id=None,
        duration_ms=duration_ms,
        timestamp=_now(),
    )
    # Telemetry is observation, never a gate. Under heavy concurrency (a slash
    # command fired in every open window at once) the trail's SQLite file can
    # raise "locking protocol"; letting that escape would report failure for a
    # note write that already succeeded. Same contract as the bridge below.
    try:
        trail_result = _get_trail().mark(None, marker, metadata)
    except Exception:                                    # noqa: BLE001
        trail_result = None

    # Additive bridge → section_tracker (best-effort; never blocks primary path)
    op_name = _ACTION_TO_OP.get(action)
    if op_name is not None:
        rec = _get_recorder()
        if rec is not None:
            try:
                section = "frontmatter" if action == "update_frontmatter" else target
                rec.record_op(
                    section=section,
                    operation=op_name,
                    after=content or "",
                    result=result,
                    error_message=error or "",
                    duration_ms=duration_ms,
                )
            except Exception:
                pass  # bridge failure must not break the caller

    return trail_result


def get_ops(limit=50):
    """Get recent operations from trail_log."""
    return _get_trail().get_trail(limit=limit)


def get_stats():
    """Get aggregate stats from trail_log."""
    return _get_trail().trail_stats()


def get_today_ops():
    """Get all operations from today."""
    today = datetime.now().strftime("%Y-%m-%d")
    trail = _get_trail()
    rows = trail.conn.execute(
        "SELECT * FROM trail_log WHERE created_at >= ? ORDER BY created_at",
        (today,)
    ).fetchall()
    return [dict(r) for r in rows]


def audit_summary():
    """Generate a human-readable audit summary for today."""
    ops = get_today_ops()
    if not ops:
        return {"date": datetime.now().strftime("%Y-%m-%d"), "total": 0,
                "ok": 0, "errors": 0, "sections": {}, "actors": {},
                "error_details": []}

    ok = 0
    errors = 0
    sections = {}
    actors = {}
    error_details = []

    for op in ops:
        meta = json.loads(op.get("metadata_json", "{}"))
        result = meta.get("result", "unknown")

        if result == "ok":
            ok += 1
        else:
            errors += 1
            error_details.append({
                "time": op["created_at"],
                "action": op["action"],
                "target": op["target"],
                "result": result,
                "error": meta.get("error", ""),
            })

        target = op.get("target", "unknown")
        sections[target] = sections.get(target, 0) + 1
        actor = op.get("actor", "unknown")
        actors[actor] = actors.get(actor, 0) + 1

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total": len(ops),
        "ok": ok,
        "errors": errors,
        "sections": sections,
        "actors": actors,
        "error_details": error_details,
    }


# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) < 2 or _sys.argv[1] == "summary":
        summary = audit_summary()
        print(f"Audit — {summary['date']}")
        print(f"  Operations: {summary['total']} ({summary['ok']} ok, {summary['errors']} errors)")
        if summary["sections"]:
            sec_str = ", ".join(f"{k} ({v}x)" for k, v in
                               sorted(summary["sections"].items(), key=lambda x: -x[1]))
            print(f"  Sections: {sec_str}")
        if summary["actors"]:
            act_str = ", ".join(f"{k} ({v})" for k, v in summary["actors"].items())
            print(f"  Actors: {act_str}")
        if summary["error_details"]:
            print(f"  Errors:")
            for e in summary["error_details"]:
                print(f"    - {e['time'][:19]} {e['action']} -> {e['target']}: {e['result']} {e['error']}")
    elif _sys.argv[1] == "recent":
        limit = int(_sys.argv[2]) if len(_sys.argv) > 2 else 10
        ops = get_ops(limit=limit)
        for op in ops:
            meta = json.loads(op.get("metadata_json", "{}"))
            result = meta.get("result", "?")
            print(f"  {op['created_at'][:19]}  {result:12s}  {op['action']:20s}  {op['target']}")
    elif _sys.argv[1] == "stats":
        print(json.dumps(get_stats(), indent=2))
    else:
        print("Usage:")
        print("  python harness_log.py summary  — today's audit summary")
        print("  python harness_log.py recent N — last N operations")
        print("  python harness_log.py stats    — aggregate stats")
