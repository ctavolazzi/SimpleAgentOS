"""
state_db.py — SQLite persistence layer for SimpleAgentOS.

Every entity gets: UUID, timestamp, content hash.
Zero external dependencies (stdlib only).
"""

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / ".self_explorer" / "state.db"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _uuid() -> str:
    return str(uuid.uuid4())


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection):
    """Create all tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            started_at    TEXT NOT NULL,
            ended_at      TEXT,
            steps         INTEGER DEFAULT 0,
            files_explored TEXT DEFAULT '[]',
            status        TEXT DEFAULT 'running',
            config_json   TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS journal (
            id            TEXT PRIMARY KEY,
            session_id    TEXT NOT NULL,
            step          INTEGER NOT NULL,
            type          TEXT NOT NULL,
            content       TEXT NOT NULL,
            content_hash  TEXT NOT NULL,
            file          TEXT,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS capabilities (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            description   TEXT DEFAULT '',
            locked        INTEGER DEFAULT 0,
            enabled       INTEGER DEFAULT 1,
            category      TEXT DEFAULT 'extension',
            endpoint      TEXT,
            config_json   TEXT DEFAULT '{}',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ui_versions (
            id            TEXT PRIMARY KEY,
            version       INTEGER NOT NULL,
            content_hash  TEXT NOT NULL,
            zone_html     TEXT NOT NULL,
            session_id    TEXT,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS metadata (
            key           TEXT PRIMARY KEY,
            value         TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS thought_chains (
            id            TEXT PRIMARY KEY,
            session_id    TEXT,
            trigger_hash  TEXT NOT NULL,
            trigger_text  TEXT NOT NULL,
            file          TEXT,
            thought       TEXT NOT NULL,
            thought_hash  TEXT NOT NULL,
            compressed    TEXT,
            keywords      TEXT DEFAULT '',
            created_at    TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS dream_context (
            id            TEXT PRIMARY KEY,
            source_chain  TEXT NOT NULL,
            summary       TEXT NOT NULL,
            summary_hash  TEXT NOT NULL,
            relevance_keys TEXT DEFAULT '',
            tokens_est    INTEGER DEFAULT 0,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (source_chain) REFERENCES thought_chains(id)
        );

        CREATE INDEX IF NOT EXISTS idx_journal_session ON journal(session_id);
        CREATE INDEX IF NOT EXISTS idx_journal_type ON journal(type);
        CREATE INDEX IF NOT EXISTS idx_ui_versions_session ON ui_versions(session_id);
        CREATE INDEX IF NOT EXISTS idx_thought_trigger ON thought_chains(trigger_hash);
        CREATE INDEX IF NOT EXISTS idx_thought_file ON thought_chains(file);
        CREATE INDEX IF NOT EXISTS idx_dream_keys ON dream_context(relevance_keys);
    """)
    conn.commit()


# ── Session operations ─────────────────────────────────────────────

def create_session(conn, config=None) -> str:
    sid = _uuid()
    conn.execute(
        "INSERT INTO sessions (id, started_at, config_json) VALUES (?, ?, ?)",
        (sid, _now(), json.dumps(config or {}))
    )
    conn.commit()
    return sid


def end_session(conn, session_id, steps, files_explored):
    conn.execute(
        "UPDATE sessions SET ended_at=?, steps=?, files_explored=?, status='complete' WHERE id=?",
        (_now(), steps, json.dumps(files_explored), session_id)
    )
    conn.commit()


def get_session(conn, session_id) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(conn, limit=20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Journal operations ─────────────────────────────────────────────

def add_journal_entry(conn, session_id, step, entry_type, content, file=None) -> dict:
    eid = _uuid()
    chash = _hash(content)
    now = _now()
    conn.execute(
        "INSERT INTO journal (id, session_id, step, type, content, content_hash, file, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (eid, session_id, step, entry_type, content, chash, file, now)
    )
    conn.commit()
    return {"id": eid, "session_id": session_id, "step": step, "type": entry_type,
            "content": content, "content_hash": chash, "file": file, "timestamp": now}


def get_journal(conn, session_id, limit=100, offset=0) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM journal WHERE session_id=? ORDER BY created_at ASC LIMIT ? OFFSET ?",
        (session_id, limit, offset)
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_journal(conn, limit=200) -> list[dict]:
    """All entries across all sessions, newest first."""
    rows = conn.execute(
        "SELECT * FROM journal ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def journal_count(conn, session_id) -> int:
    row = conn.execute("SELECT COUNT(*) as c FROM journal WHERE session_id=?", (session_id,)).fetchone()
    return row["c"]


# ── Capability operations ──────────────────────────────────────────

def seed_capabilities(conn, caps_list: list[dict]):
    """Upsert capabilities from a list (e.g., from capabilities.json migration)."""
    now = _now()
    for cap in caps_list:
        existing = conn.execute("SELECT id FROM capabilities WHERE id=?", (cap["id"],)).fetchone()
        if existing:
            # Only update non-locked fields if it already exists
            conn.execute(
                "UPDATE capabilities SET description=?, enabled=?, config_json=?, updated_at=? WHERE id=? AND locked=0",
                (cap.get("description", ""), int(cap.get("enabled", True)),
                 json.dumps(cap.get("config", {})), now, cap["id"])
            )
        else:
            conn.execute(
                "INSERT INTO capabilities (id, name, description, locked, enabled, category, endpoint, config_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cap["id"], cap["name"], cap.get("description", ""),
                 int(cap.get("locked", False)), int(cap.get("enabled", True)),
                 cap.get("category", "extension"), cap.get("endpoint"),
                 json.dumps(cap.get("config", {})), now, now)
            )
    conn.commit()


def get_capabilities(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM capabilities ORDER BY category, name").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["locked"] = bool(d["locked"])
        d["enabled"] = bool(d["enabled"])
        d["config"] = json.loads(d.get("config_json", "{}"))
        del d["config_json"]
        result.append(d)
    return result


def toggle_capability(conn, cap_id) -> dict | None:
    row = conn.execute("SELECT * FROM capabilities WHERE id=?", (cap_id,)).fetchone()
    if not row:
        return None
    if row["locked"]:
        return {"error": f"'{cap_id}' is locked"}
    new_state = not bool(row["enabled"])
    conn.execute(
        "UPDATE capabilities SET enabled=?, updated_at=? WHERE id=?",
        (int(new_state), _now(), cap_id)
    )
    conn.commit()
    return {"id": cap_id, "enabled": new_state}


def add_capability(conn, cap: dict) -> dict:
    now = _now()
    cap_id = cap.get("id", f"custom_{_uuid()[:8]}")
    conn.execute(
        "INSERT INTO capabilities (id, name, description, locked, enabled, category, endpoint, config_json, created_at, updated_at) VALUES (?,?,?,0,?,?,?,?,?,?)",
        (cap_id, cap.get("name", "Custom"), cap.get("description", ""),
         int(cap.get("enabled", True)), cap.get("category", "user"),
         cap.get("endpoint"), json.dumps(cap.get("config", {})), now, now)
    )
    conn.commit()
    return {"id": cap_id, "name": cap["name"]}


# ── UI version operations ─────────────────────────────────────────

def save_ui_version(conn, version, zone_html, session_id=None) -> str:
    vid = _uuid()
    chash = _hash(zone_html)
    conn.execute(
        "INSERT INTO ui_versions (id, version, content_hash, zone_html, session_id, created_at) VALUES (?,?,?,?,?,?)",
        (vid, version, chash, zone_html, session_id, _now())
    )
    conn.commit()
    return vid


def get_ui_versions(conn, limit=20) -> list[dict]:
    rows = conn.execute(
        "SELECT id, version, content_hash, session_id, created_at FROM ui_versions ORDER BY version DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_ui_version_html(conn, version_id) -> str | None:
    row = conn.execute("SELECT zone_html FROM ui_versions WHERE id=?", (version_id,)).fetchone()
    return row["zone_html"] if row else None


# ── Metadata operations ────────────────────────────────────────────

def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
        (key, str(value), _now(), str(value), _now())
    )
    conn.commit()


def get_meta(conn, key) -> str | None:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# ── Thought chain operations ───────────────────────────────────────

def _extract_keywords(text: str, max_kw: int = 10) -> str:
    """Cheap keyword extraction — no ML, just word frequency minus stopwords."""
    import re
    stops = {"the","a","an","is","are","was","were","be","been","being","have","has",
             "had","do","does","did","will","would","could","should","may","might",
             "shall","can","need","dare","ought","this","that","it","i","me","my",
             "we","our","you","your","he","his","she","her","they","them","their",
             "and","or","but","if","then","else","when","at","by","for","with",
             "about","against","between","through","during","before","after","above",
             "below","to","from","in","on","of","not","no","so","as","into","up"}
    words = re.findall(r'[a-z_]+', text.lower())
    freq = {}
    for w in words:
        if w not in stops and len(w) > 2:
            freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq, key=freq.get, reverse=True)[:max_kw]
    return ",".join(ranked)


def store_thought(conn, session_id, trigger_text, thought, file=None, compressed=None) -> dict:
    """Store a thought chain. Returns the entry with dedup info."""
    tid = _uuid()
    trigger_h = _hash(trigger_text)
    thought_h = _hash(thought)
    keywords = _extract_keywords(thought)

    # Check for prior similar thought (same trigger hash = same question)
    prior = conn.execute(
        "SELECT id, thought, compressed, created_at FROM thought_chains WHERE trigger_hash=? ORDER BY created_at DESC LIMIT 1",
        (trigger_h,)
    ).fetchone()

    entry = {
        "id": tid,
        "trigger_hash": trigger_h,
        "thought_hash": thought_h,
        "keywords": keywords,
        "is_novel": prior is None,
        "prior_id": dict(prior)["id"] if prior else None,
    }

    conn.execute(
        "INSERT INTO thought_chains (id, session_id, trigger_hash, trigger_text, file, thought, thought_hash, compressed, keywords, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tid, session_id, trigger_h, trigger_text[:500], file, thought, thought_h, compressed, keywords, _now())
    )
    conn.commit()
    return entry


def recall_prior_thoughts(conn, file=None, keywords=None, limit=3) -> list[dict]:
    """Retrieve compressed prior thoughts relevant to the current context.
    Uses file match first, then keyword overlap as fallback."""
    results = []

    # Strategy 1: Same file explored before
    if file:
        rows = conn.execute(
            "SELECT tc.id, tc.thought, tc.compressed, tc.file, tc.keywords, tc.created_at, "
            "dc.summary FROM thought_chains tc LEFT JOIN dream_context dc ON dc.source_chain=tc.id "
            "WHERE tc.file=? ORDER BY tc.created_at DESC LIMIT ?",
            (file, limit)
        ).fetchall()
        results.extend([dict(r) for r in rows])

    # Strategy 2: Keyword overlap (cheap TF-IDF proxy)
    if keywords and len(results) < limit:
        kw_list = keywords.split(",") if isinstance(keywords, str) else keywords
        for kw in kw_list[:5]:
            if len(results) >= limit:
                break
            rows = conn.execute(
                "SELECT tc.id, tc.thought, tc.compressed, tc.file, tc.keywords, tc.created_at, "
                "dc.summary FROM thought_chains tc LEFT JOIN dream_context dc ON dc.source_chain=tc.id "
                "WHERE tc.keywords LIKE ? AND tc.id NOT IN ({}) ORDER BY tc.created_at DESC LIMIT ?".format(
                    ",".join(f"'{r['id']}'" for r in results) or "''"
                ),
                (f"%{kw}%", limit - len(results))
            ).fetchall()
            results.extend([dict(r) for r in rows])

    return results[:limit]


def store_dream(conn, source_chain_id, summary, relevance_keys="") -> str:
    """Store a compressed dream context from a thought chain."""
    did = _uuid()
    conn.execute(
        "INSERT INTO dream_context (id, source_chain, summary, summary_hash, relevance_keys, tokens_est, created_at) VALUES (?,?,?,?,?,?,?)",
        (did, source_chain_id, summary, _hash(summary), relevance_keys, len(summary.split()), _now())
    )
    conn.commit()
    return did


def get_dream_context(conn, limit=5) -> list[dict]:
    """Get the most recent dream summaries for injection into prompts."""
    rows = conn.execute(
        "SELECT * FROM dream_context ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def thought_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) as c FROM thought_chains").fetchone()["c"]


def novel_thought_ratio(conn) -> float:
    """What fraction of thoughts are genuinely novel vs re-treading?"""
    total = thought_count(conn)
    if total == 0:
        return 1.0
    # Count unique trigger hashes
    unique = conn.execute("SELECT COUNT(DISTINCT trigger_hash) as c FROM thought_chains").fetchone()["c"]
    return unique / total


# ── Stats ──────────────────────────────────────────────────────────

def get_stats(conn) -> dict:
    """Aggregate stats across all sessions."""
    sessions = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
    entries = conn.execute("SELECT COUNT(*) as c FROM journal").fetchone()["c"]
    ui_mods = conn.execute("SELECT COUNT(*) as c FROM ui_versions").fetchone()["c"]
    caps = conn.execute("SELECT COUNT(*) as c FROM capabilities WHERE enabled=1").fetchone()["c"]
    files_row = conn.execute(
        "SELECT COUNT(DISTINCT file) as c FROM journal WHERE file IS NOT NULL"
    ).fetchone()
    thoughts = thought_count(conn)
    dreams = conn.execute("SELECT COUNT(*) as c FROM dream_context").fetchone()["c"]
    return {
        "total_sessions": sessions,
        "total_journal_entries": entries,
        "total_ui_modifications": ui_mods,
        "active_capabilities": caps,
        "unique_files_explored": files_row["c"],
        "total_thoughts": thoughts,
        "total_dreams": dreams,
        "novelty_ratio": round(novel_thought_ratio(conn), 2),
    }
