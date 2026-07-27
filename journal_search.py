#!/usr/bin/env python3
"""
journal_search.py: real recall over the agent journal.

`pb_journal.query()` matches with `title~"x" || body~"x"`. That is a substring
scan. It cannot rank, it cannot stem, it splits on nothing, and it misses any
phrasing that is not literally the characters you typed. "What did I learn
about X" is the only question anyone ever asks a memory, and substring answers
it badly.

This adds two better ways to ask, and fuses them:

  lexical   FTS5 with BM25 ranking. Stems, tokenizes, ranks by term rarity.
            Built into SQLite, so it needs nothing installed and works offline.
  semantic  Embeddings from the llama.cpp build already in this workspace.
            Finds "the outbox filled up" when you searched "spool had no cap".

The expectation going in was that these are complementary, that BM25 wins on
names and identifiers while embeddings win on paraphrase, and that the answer is
Reciprocal Rank Fusion. That was built first and then measured, and the
measurement disagreed: ranking by semantic score alone beat every fusion
weighting tried, on two corpora, including on the identifier queries chosen to
favor BM25. So semantic ranks and lexical backfills. See `_fuse` for the numbers
and tests/test_journal_search_quality.py for the benchmark that produced them.

Everything degrades rather than fails. No embedding model means lexical only.
No FTS5 index means `pb_journal.query()`, which is where we started. The index
is a derived cache: delete it and it rebuilds, and it is never the system of
record.

Vectors are keyed on `content_hash`, the field pb_journal has populated on every
entry since day one and never used. Identical content embeds once no matter how
many entries carry it, and re-indexing only pays for what actually changed.

Usage:
  python3 journal_search.py index                 # incremental
  python3 journal_search.py index --rebuild       # from scratch
  python3 journal_search.py search "spool cap"
  python3 journal_search.py search "x" --lexical  # no embeddings
  python3 journal_search.py status
  python3 journal_search.py progress-serve        # browser progress bar
"""

import array
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pb_journal

__version__ = "0.0.1"

# ── Paths + config ────────────────────────────────────────────────────

STATE_DIR = pb_journal.STATE_DIR
INDEX_DB = Path(os.environ.get("PB_JOURNAL_INDEX_DB", STATE_DIR / "journal_index.db"))
PROGRESS_PATH = Path(os.environ.get("PB_JOURNAL_INDEX_PROGRESS",
                                    STATE_DIR / "journal_index_progress.json"))

# The llama.cpp build already sitting in ~/Code. Both are overridable, and both
# are checked for existence before use: a missing binary or model is a reason to
# fall back to lexical search, never a reason to raise.
_LLAMA_ROOT = Path(os.environ.get("LLAMA_CPP_ROOT", Path.home() / "Code" / "llama.cpp"))
EMBED_BIN = Path(os.environ.get("PB_JOURNAL_EMBED_BIN",
                                _LLAMA_ROOT / "build" / "bin" / "llama-embedding"))
EMBED_MODEL = Path(os.environ.get(
    "PB_JOURNAL_EMBED_MODEL",
    _LLAMA_ROOT / "models" / "embedding" / "bge-small-en-v1.5-q8_0.gguf"))

# bge-small-en-v1.5 is a 512-token BERT. Feeding it more than it can attend to
# wastes time and silently truncates anyway, so truncate deliberately instead.
EMBED_MAX_CHARS = int(os.environ.get("PB_JOURNAL_EMBED_MAX_CHARS", "1800"))
EMBED_BATCH = int(os.environ.get("PB_JOURNAL_EMBED_BATCH", "32"))
EMBED_TIMEOUT = float(os.environ.get("PB_JOURNAL_EMBED_TIMEOUT", "300"))

PROGRESS_HOST = os.environ.get("PB_JOURNAL_PROGRESS_HOST", "127.0.0.1")
PROGRESS_PORT = int(os.environ.get("PB_JOURNAL_PROGRESS_PORT", "8099"))

try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Index storage ─────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    entry_id     TEXT PRIMARY KEY,
    content_hash TEXT,
    occurred_at  TEXT,
    kind         TEXT,
    project      TEXT,
    actor        TEXT,
    title        TEXT,
    body         TEXT,
    tags         TEXT,
    importance   REAL,
    indexed_at   TEXT
);
CREATE INDEX IF NOT EXISTS entries_hash ON entries(content_hash);
CREATE INDEX IF NOT EXISTS entries_when ON entries(occurred_at);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    entry_id UNINDEXED,
    title,
    body,
    tags,
    tokenize = 'porter unicode61'
);

-- Keyed on content_hash, not entry_id: the same text embeds once however many
-- entries carry it, and an unchanged entry is never re-embedded on reindex.
CREATE TABLE IF NOT EXISTS vectors (
    content_hash TEXT PRIMARY KEY,
    model        TEXT,
    dim          INTEGER,
    vec          BLOB,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the index, creating it if absent. The index is derived data, so
    creating it on demand is always safe."""
    path = Path(path or INDEX_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def _pack(vec) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


# ── Embedding via llama.cpp ───────────────────────────────────────────

def embedder_available() -> bool:
    return EMBED_BIN.is_file() and os.access(EMBED_BIN, os.X_OK) and EMBED_MODEL.is_file()


def embedder_info() -> dict:
    return {
        "available": embedder_available(),
        "binary": str(EMBED_BIN),
        "binary_ok": EMBED_BIN.is_file(),
        "model": str(EMBED_MODEL),
        "model_ok": EMBED_MODEL.is_file(),
        "model_name": EMBED_MODEL.stem,
        "batch": EMBED_BATCH,
    }


def _flatten(text: str) -> str:
    """llama-embedding's `-f` reads one text per LINE, so any newline inside a
    body would silently split one entry into several vectors and knock every
    later result out of alignment with its entry. Collapse all whitespace, and
    never emit an empty line."""
    flat = re.sub(r"\s+", " ", (text or "")).strip()
    return flat[:EMBED_MAX_CHARS] or "(empty)"


def embed_texts(texts: list[str], progress: Optional[Callable] = None) -> list[list[float]]:
    """Embed a list of texts, in batches, one llama.cpp process per batch.

    Returns [] if the embedder is unavailable or fails. Callers treat an empty
    result as "no semantic side this run", never as an error.
    """
    if not texts or not embedder_available():
        return []
    out: list[list[float]] = []
    total = len(texts)
    for start in range(0, total, EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        # _embed_batch already swallows its own failures, but this is the call
        # that stands between a broken model and `search()` raising in a
        # caller's face. Guard it here too rather than trust one layer.
        try:
            vecs = _embed_batch(batch)
        except Exception:  # noqa: BLE001
            return []
        if len(vecs) != len(batch):
            # A short or misaligned batch means we cannot trust which vector
            # belongs to which text. Returning partial data would poison the
            # index quietly, so drop the whole run and stay lexical.
            return []
        out.extend(vecs)
        if progress:
            progress(len(out), total)
    return out


def _embed_batch(batch: list[str]) -> list[list[float]]:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as fh:
            tmp = Path(fh.name)
            fh.write("\n".join(_flatten(t) for t in batch) + "\n")
        proc = subprocess.run(
            [str(EMBED_BIN), "-m", str(EMBED_MODEL), "-f", str(tmp),
             "--embd-output-format", "json", "--pooling", "mean",
             "-ngl", "99", "--no-warmup"],
            capture_output=True, text=True, timeout=EMBED_TIMEOUT,
        )
        if proc.returncode != 0:
            return []
        payload = json.loads(proc.stdout)
        data = sorted(payload.get("data", []), key=lambda d: d.get("index", 0))
        return [list(d["embedding"]) for d in data]
    except Exception:  # noqa: BLE001
        return []
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def embed_one(text: str) -> Optional[list[float]]:
    vecs = embed_texts([text])
    return vecs[0] if vecs else None


def _embed_text_for(row: dict) -> str:
    """What actually gets embedded. Title carries most of the signal per token,
    tags carry the topic, body carries the substance."""
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    parts = [row.get("title") or "", " ".join(tags), row.get("body") or ""]
    return " \n ".join(p for p in parts if p)


# ── Progress reporting ────────────────────────────────────────────────

class Progress:
    """Writes indexing progress to a JSON file so something else can watch it.

    The browser page polls that file. Writes are atomic (temp + replace) so a
    poll never reads a half-written object, and every failure is swallowed:
    losing the progress display must never be able to fail the index run.
    """

    def __init__(self, path: Optional[Path] = None, total: int = 0, phase: str = "starting"):
        self.path = Path(path or PROGRESS_PATH)
        self.started = time.time()
        self.total = total
        self.done = 0
        self.phase = phase
        self.message = ""
        self.running = True
        self.error: Optional[str] = None
        self.model = EMBED_MODEL.stem if embedder_available() else None
        self.write()

    def update(self, done: Optional[int] = None, total: Optional[int] = None,
               phase: Optional[str] = None, message: Optional[str] = None) -> None:
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total
        if phase is not None:
            self.phase = phase
        if message is not None:
            self.message = message
        self.write()

    def finish(self, message: str = "", error: Optional[str] = None) -> None:
        self.running = False
        self.error = error
        if message:
            self.message = message
        if not error and self.total:
            self.done = self.total
        self.phase = "error" if error else "done"
        self.write()

    def snapshot(self) -> dict:
        elapsed = time.time() - self.started
        rate = (self.done / elapsed) if elapsed > 0 and self.done else 0.0
        remaining = max(0, self.total - self.done)
        eta = (remaining / rate) if rate > 0 and self.running else 0.0
        pct = (100.0 * self.done / self.total) if self.total else (0.0 if self.running else 100.0)
        return {
            "phase": self.phase,
            "done": self.done,
            "total": self.total,
            "percent": round(pct, 1),
            "elapsed": round(elapsed, 2),
            "eta": round(eta, 2),
            "rate": round(rate, 2),
            "running": self.running,
            "error": self.error,
            "message": self.message,
            "model": self.model,
            "started_at": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
            "updated_at": _now_iso(),
        }

    def write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass


def read_progress() -> dict:
    try:
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"phase": "idle", "done": 0, "total": 0, "percent": 0.0,
                "running": False, "message": "no index run recorded yet",
                "elapsed": 0.0, "eta": 0.0, "rate": 0.0, "error": None,
                "model": None}


# ── Indexing ──────────────────────────────────────────────────────────

def reindex(rebuild: bool = False, embed: bool = True, limit: int = 100000,
            progress: Optional[Progress] = None, quiet: bool = True) -> dict:
    """Pull the journal into the local index.

    Incremental by default: an entry already indexed at the same content_hash is
    skipped, and a vector already present for that hash is never recomputed.
    """
    own_progress = progress is None
    prog = progress or Progress(total=0, phase="reading")
    stats = {"scanned": 0, "indexed": 0, "embedded": 0, "reused": 0,
             "skipped": 0, "rebuilt": rebuild, "errors": []}
    try:
        if rebuild and INDEX_DB.exists():
            # Vectors are expensive and content-addressed, so a rebuild of the
            # text index has no reason to throw them away. Carry them over.
            saved = _save_vectors()
            INDEX_DB.unlink()
            conn = connect()
            _restore_vectors(conn, saved)
        else:
            conn = connect()

        prog.update(phase="reading", message="reading journal")
        rows = pb_journal.query(limit=limit)
        stats["scanned"] = len(rows)
        prog.update(total=len(rows), phase="indexing",
                    message=f"indexing {len(rows)} entries")

        needed: dict[str, str] = {}   # content_hash -> text to embed
        for i, row in enumerate(rows, 1):
            entry_id = row.get("entry_id")
            if not entry_id:
                stats["skipped"] += 1
                continue
            chash = row.get("content_hash") or pb_journal._hash(
                f"{row.get('kind')}|{row.get('title')}|{row.get('body')}")
            existing = conn.execute(
                "SELECT content_hash FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
            if existing is None or existing["content_hash"] != chash:
                _upsert(conn, row, chash)
                stats["indexed"] += 1
            else:
                stats["skipped"] += 1
            has_vec = conn.execute(
                "SELECT 1 FROM vectors WHERE content_hash=?", (chash,)).fetchone()
            if has_vec:
                stats["reused"] += 1
            else:
                needed.setdefault(chash, _embed_text_for(row))
            if i % 25 == 0 or i == len(rows):
                prog.update(done=i)
        conn.commit()

        if embed and needed and embedder_available():
            hashes = list(needed)
            texts = [needed[h] for h in hashes]
            prog.update(done=0, total=len(texts), phase="embedding",
                        message=f"embedding {len(texts)} new entries with {EMBED_MODEL.stem}")
            vecs = embed_texts(texts, progress=lambda d, t: prog.update(done=d, total=t))
            if vecs:
                now = _now_iso()
                conn.executemany(
                    "INSERT OR REPLACE INTO vectors(content_hash,model,dim,vec,created_at) "
                    "VALUES(?,?,?,?,?)",
                    [(h, EMBED_MODEL.stem, len(v), _pack(v), now)
                     for h, v in zip(hashes, vecs)])
                stats["embedded"] = len(vecs)
                _meta_set(conn, "embed_model", EMBED_MODEL.stem)
                _meta_set(conn, "embed_dim", str(len(vecs[0])))
            else:
                stats["errors"].append("embedding produced no vectors; index is lexical only")
        elif embed and needed and not embedder_available():
            stats["errors"].append("no embedder available; index is lexical only")

        _meta_set(conn, "last_index", _now_iso())
        _meta_set(conn, "entry_count", str(stats["scanned"]))
        conn.commit()
        conn.close()
        if own_progress:
            prog.finish(message=(f"indexed {stats['indexed']}, embedded "
                                 f"{stats['embedded']}, reused {stats['reused']}"))
    except Exception as exc:  # noqa: BLE001
        stats["errors"].append(f"{type(exc).__name__}: {exc}")
        if own_progress:
            prog.finish(error=str(exc))
    return stats


def _upsert(conn: sqlite3.Connection, row: dict, chash: str) -> None:
    entry_id = row["entry_id"]
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tag_text = " ".join(str(t) for t in tags)
    conn.execute("DELETE FROM entries WHERE entry_id=?", (entry_id,))
    conn.execute("DELETE FROM entries_fts WHERE entry_id=?", (entry_id,))
    conn.execute(
        "INSERT INTO entries(entry_id,content_hash,occurred_at,kind,project,actor,"
        "title,body,tags,importance,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (entry_id, chash, row.get("occurred_at") or "", row.get("kind") or "",
         row.get("project") or "", row.get("actor") or "", row.get("title") or "",
         row.get("body") or "", json.dumps(tags),
         float(row.get("importance") or 0.5), _now_iso()))
    conn.execute(
        "INSERT INTO entries_fts(entry_id,title,body,tags) VALUES(?,?,?,?)",
        (entry_id, row.get("title") or "", row.get("body") or "", tag_text))


def _save_vectors() -> list[tuple]:
    try:
        conn = connect()
        rows = conn.execute(
            "SELECT content_hash,model,dim,vec,created_at FROM vectors").fetchall()
        out = [tuple(r) for r in rows]
        conn.close()
        return out
    except Exception:  # noqa: BLE001
        return []


def _restore_vectors(conn: sqlite3.Connection, saved: list[tuple]) -> None:
    if not saved:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO vectors(content_hash,model,dim,vec,created_at) "
        "VALUES(?,?,?,?,?)", saved)
    conn.commit()


# ── Searching ─────────────────────────────────────────────────────────

_WORD = re.compile(r"[0-9A-Za-z_]+")


def _fts_query(text: str) -> str:
    """Turn free text into an FTS5 MATCH expression.

    Every term is quoted, because unquoted user input is an injection surface
    into FTS5's own query syntax: a stray `"` or `*` or `NEAR` is a syntax error
    at best. Terms are OR-ed for recall, and BM25 does the ranking, so a
    document matching more of the rare terms still comes out on top.

    Stopwords are deliberately NOT stripped. Filtering them looked obviously
    right and measured worse: lexical-only accuracy fell from 0.79 to 0.71 on
    the benchmark in tests/test_journal_search_quality.py. BM25's IDF term
    already discounts words that appear everywhere, so removing them by hand
    discards recall to solve a problem the ranking function had solved.
    """
    terms = [t for t in _WORD.findall(text or "") if len(t) > 1]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


def _lexical(conn: sqlite3.Connection, text: str, limit: int) -> list[tuple[str, float]]:
    match = _fts_query(text)
    if not match:
        return []
    try:
        rows = conn.execute(
            "SELECT entry_id, bm25(entries_fts) AS score FROM entries_fts "
            "WHERE entries_fts MATCH ? ORDER BY score LIMIT ?", (match, limit)).fetchall()
    except sqlite3.Error:
        return []
    # bm25() is negative, more negative is better. Flip it so bigger is better.
    return [(r["entry_id"], -float(r["score"])) for r in rows]


def _semantic(conn: sqlite3.Connection, text: str, limit: int) -> list[tuple[str, float]]:
    try:
        qvec = embed_one(text)
    except Exception:  # noqa: BLE001
        return []
    if not qvec:
        return []
    rows = conn.execute(
        "SELECT e.entry_id AS entry_id, v.vec AS vec FROM entries e "
        "JOIN vectors v ON v.content_hash = e.content_hash").fetchall()
    if not rows:
        return []
    ids = [r["entry_id"] for r in rows]
    if _np is not None:
        mat = _np.frombuffer(b"".join(bytes(r["vec"]) for r in rows), dtype=_np.float32)
        mat = mat.reshape(len(rows), -1)
        q = _np.asarray(qvec, dtype=_np.float32)
        if mat.shape[1] != q.shape[0]:
            return []
        # bge vectors come out L2-normalized, so a dot product is the cosine.
        # Normalize anyway: a model swap should not silently skew ranking.
        mat = mat / (_np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
        q = q / (float(_np.linalg.norm(q)) + 1e-12)
        sims = mat @ q
        order = _np.argsort(-sims)[:limit]
        return [(ids[i], float(sims[i])) for i in order]
    scored = []
    for r in rows:
        v = _unpack(bytes(r["vec"]))
        if len(v) != len(qvec):
            continue
        dot = sum(a * b for a, b in zip(v, qvec))
        na = sum(a * a for a in v) ** 0.5 or 1e-12
        nb = sum(b * b for b in qvec) ** 0.5 or 1e-12
        scored.append((r["entry_id"], dot / (na * nb)))
    scored.sort(key=lambda kv: -kv[1])
    return scored[:limit]


def _fuse(lexical: list[tuple[str, float]],
          semantic: list[tuple[str, float]]) -> list[tuple[str, dict]]:
    """Combine the two rankings: semantic orders, lexical backfills.

    The obvious design here is Reciprocal Rank Fusion, and it was the first
    thing built. Measuring it is what killed it. On two corpora, scored over 14
    labeled queries by top-1 and MRR (tests/test_journal_search_quality.py):

                        live corpus        frozen corpus
        semantic only   0.93 / 0.964       0.86 / 0.903
        even fusion     0.86 / 0.907       0.86 / 0.879
        BM25 only       0.71 / 0.815       0.79 / 0.829

    Semantic alone was best or tied on both. Fusion never beat it. Worse, when a
    weighted RRF was tuned to the live corpus (semantic at 8x, which looked
    convincingly optimal there) it came out BELOW even fusion on the frozen one.
    That constant was fitting noise: at this corpus size the whole spread
    between weightings is one query moving one rank.

    So there is no tuned weight here, because none survived contact with a
    second corpus. Semantic ranks. Lexical is kept for the two jobs it is
    genuinely better at than nothing:

      - it is the entire ranking when no embedder is installed, and
      - it finds entries that have no vector yet, so something written since the
        last index is still recallable rather than invisible.

    Re-run the benchmark as the journal grows. Embedding quality degrades with
    corpus size and lexical does not, so this call is expected to change, and it
    should change on evidence rather than on the intuition that fusing is tidier.
    """
    fused: list[tuple[str, dict]] = []
    seen: set[str] = set()
    lex_rank = {eid: (i, s) for i, (eid, s) in enumerate(lexical, 1)}

    for rank, (entry_id, score) in enumerate(semantic, 1):
        via = {"semantic": {"rank": rank, "score": round(score, 4)}}
        if entry_id in lex_rank:
            i, s = lex_rank[entry_id]
            via["bm25"] = {"rank": i, "score": round(s, 4)}
        fused.append((entry_id, {"score": round(1.0 / rank, 6), "via": via}))
        seen.add(entry_id)

    # Anything lexical found that semantic could not see. With a fully embedded
    # index this is empty; it is how a not-yet-embedded entry stays findable.
    for rank, (entry_id, score) in enumerate(lexical, 1):
        if entry_id in seen:
            continue
        fused.append((entry_id, {
            "score": round(1.0 / (len(semantic) + rank), 6),
            "via": {"bm25": {"rank": rank, "score": round(score, 4)}}}))
    return fused


def search(text: str, limit: int = 10, *, lexical: bool = True, semantic: bool = True,
           kind: str = "", project: str = "", pool: int = 50) -> list[dict]:
    """Ask the journal a question and get ranked answers.

    Falls back through three tiers, in order: hybrid (both rankers), whichever
    single ranker is available, and finally `pb_journal.query()` if there is no
    index at all. A caller always gets rows back.
    """
    if not INDEX_DB.exists():
        rows = pb_journal.query(text, kind=kind, project=project, limit=limit)
        for r in rows:
            r["_via"] = {"substring": {}}
            r["_score"] = 0.0
        return rows

    conn = connect()
    try:
        lex = _lexical(conn, text, pool) if lexical else []
        sem = _semantic(conn, text, pool) if semantic else []
        if not lex and not sem:
            return []

        ordered = _fuse(lex, sem)

        out: list[dict] = []
        for entry_id, meta in ordered:
            row = conn.execute("SELECT * FROM entries WHERE entry_id=?",
                               (entry_id,)).fetchone()
            if row is None:
                continue
            rec = dict(row)
            if kind and rec.get("kind") != kind:
                continue
            if project and rec.get("project") != project:
                continue
            try:
                rec["tags"] = json.loads(rec.get("tags") or "[]")
            except json.JSONDecodeError:
                rec["tags"] = []
            rec["_score"] = round(meta["score"], 6)
            rec["_via"] = meta["via"]
            rec["_source"] = "index"
            out.append(rec)
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def status() -> dict:
    info = {
        "index_db": str(INDEX_DB),
        "index_exists": INDEX_DB.exists(),
        "embedder": embedder_info(),
        "numpy": _np is not None,
        "entries": 0, "vectors": 0, "coverage": 0.0,
        "last_index": None, "embed_model": None, "embed_dim": None,
        "journal_entries": None, "stale": None,
    }
    if not INDEX_DB.exists():
        return info
    conn = connect()
    try:
        info["entries"] = conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
        info["vectors"] = conn.execute("SELECT COUNT(*) c FROM vectors").fetchone()["c"]
        covered = conn.execute(
            "SELECT COUNT(*) c FROM entries e JOIN vectors v "
            "ON v.content_hash = e.content_hash").fetchone()["c"]
        info["coverage"] = round(covered / info["entries"], 3) if info["entries"] else 0.0
        info["last_index"] = _meta_get(conn, "last_index") or None
        info["embed_model"] = _meta_get(conn, "embed_model") or None
        info["embed_dim"] = _meta_get(conn, "embed_dim") or None
    finally:
        conn.close()
    try:
        live = len(pb_journal.query(limit=100000))
        info["journal_entries"] = live
        info["stale"] = live != info["entries"]
    except Exception:  # noqa: BLE001
        pass
    return info


# ── Browser progress bar ──────────────────────────────────────────────

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Journal index progress</title>
<style>
  :root{
    --cream:#f5f0e6; --paper:#faf8f3; --chocolate:#4a2c2a; --choc-mid:#6b4423;
    --burnt:#e07b3c; --rust:#c2410c; --teal:#0d9488; --line:#d4c4a8;
    --muted:#7a6b5d; --ink:#3a312b;
  }
  *{box-sizing:border-box}
  body{margin:0;padding:2.5rem 1.25rem;background:var(--cream);color:var(--ink);
    font:16px/1.7 "Outfit",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  .wrap{max-width:700px;margin:0 auto}
  h1{font-size:1.5rem;font-weight:800;margin:0 0 .25rem;color:var(--chocolate);
    letter-spacing:-.01em}
  .sub{color:var(--muted);font-size:.9rem;margin:0 0 1.75rem}
  .card{background:var(--paper);border:2px solid var(--chocolate);
    box-shadow:4px 4px 0 var(--chocolate);padding:1.5rem;margin-bottom:1.25rem}
  .phase{display:inline-block;font:600 .7rem/1 "JetBrains Mono",ui-monospace,monospace;
    text-transform:uppercase;letter-spacing:.08em;padding:.4rem .6rem;
    border:2px solid var(--chocolate);background:var(--burnt);color:#fff}
  .phase[data-p="done"]{background:var(--teal)}
  .phase[data-p="error"]{background:var(--rust)}
  .phase[data-p="idle"]{background:var(--line);color:var(--chocolate)}
  .bar{height:34px;border:2px solid var(--chocolate);background:var(--cream);
    margin:1.1rem 0 .6rem;position:relative;overflow:hidden}
  .fill{height:100%;width:0;background:var(--burnt);
    transition:width .35s cubic-bezier(.4,0,.2,1)}
  .fill.done{background:var(--teal)}
  .fill.error{background:var(--rust)}
  .fill.indeterminate{width:100%;opacity:.28;
    background:repeating-linear-gradient(45deg,var(--burnt) 0 12px,transparent 12px 24px);
    animation:slide 1s linear infinite}
  @keyframes slide{from{background-position:0 0}to{background-position:34px 0}}
  .pct{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    font:700 .85rem/1 "JetBrains Mono",ui-monospace,monospace;color:var(--chocolate)}
  .msg{font-size:.9rem;color:var(--muted);min-height:1.5rem}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1px;
    background:var(--line);border:2px solid var(--chocolate)}
  .cell{background:var(--paper);padding:.85rem 1rem}
  .k{font:600 .65rem/1 "JetBrains Mono",ui-monospace,monospace;color:var(--muted);
    text-transform:uppercase;letter-spacing:.07em}
  .v{font:700 1.35rem/1.3 "JetBrains Mono",ui-monospace,monospace;color:var(--chocolate);
    margin-top:.3rem;word-break:break-all}
  .v small{font-size:.7rem;color:var(--muted);font-weight:400}
  .foot{font-size:.8rem;color:var(--muted);text-align:center}
  code{font-family:"JetBrains Mono",ui-monospace,monospace;font-size:.85em;
    background:var(--cream);border:1px solid var(--line);padding:.1rem .35rem}
  .err{color:var(--rust);font-weight:600}
  @media (prefers-color-scheme:dark){
    :root{--cream:#241c19;--paper:#2e2521;--ink:#efe6da;--chocolate:#e8dccb;
      --line:#4a3d34;--muted:#a8998a}
    .card{box-shadow:4px 4px 0 #14100e}
    .pct{color:var(--ink)}
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Journal index</h1>
  <p class="sub">Live progress for <code>journal_search.py index</code>. Polls twice a second.</p>

  <div class="card">
    <span class="phase" id="phase" data-p="idle">idle</span>
    <div class="bar"><div class="fill" id="fill"></div><div class="pct" id="pct">0%</div></div>
    <div class="msg" id="msg">waiting for an index run</div>
  </div>

  <div class="grid">
    <div class="cell"><div class="k">Done</div><div class="v" id="done">0<small> / 0</small></div></div>
    <div class="cell"><div class="k">Elapsed</div><div class="v" id="elapsed">0.0<small>s</small></div></div>
    <div class="cell"><div class="k">ETA</div><div class="v" id="eta">-</div></div>
    <div class="cell"><div class="k">Rate</div><div class="v" id="rate">-</div></div>
  </div>

  <div class="card" style="margin-top:1.25rem">
    <div class="k">Embedding model</div>
    <div class="v" id="model" style="font-size:1rem">none</div>
  </div>

  <p class="foot">Run <code>python3 journal_search.py index</code> in a terminal to start.</p>
</div>
<script>
const $ = id => document.getElementById(id);
const secs = s => s >= 60 ? `${Math.floor(s/60)}m ${Math.round(s%60)}s` : `${s.toFixed(1)}s`;

async function tick(){
  let d;
  try { d = await (await fetch('progress.json?t=' + Date.now())).json(); }
  catch { $('msg').textContent = 'progress server unreachable'; return; }

  const phase = d.phase || 'idle';
  $('phase').textContent = phase;
  $('phase').dataset.p = phase;

  const fill = $('fill');
  fill.className = 'fill' + (phase === 'done' ? ' done' : phase === 'error' ? ' error' : '');
  // A running phase with no known total gets a barber pole, not a fake number.
  if (d.running && !d.total){
    fill.classList.add('indeterminate');
    $('pct').textContent = '';
  } else {
    fill.style.width = (d.percent || 0) + '%';
    $('pct').textContent = Math.round(d.percent || 0) + '%';
  }

  $('done').innerHTML = `${d.done||0}<small> / ${d.total||0}</small>`;
  $('elapsed').innerHTML = secs(d.elapsed||0).replace(/([a-z]+)/g,'<small>$1</small>');
  $('eta').innerHTML = (d.running && d.eta) ? secs(d.eta).replace(/([a-z]+)/g,'<small>$1</small>') : '-';
  $('rate').innerHTML = d.rate ? `${d.rate}<small>/s</small>` : '-';
  $('model').textContent = d.model || 'none (lexical only)';
  $('msg').innerHTML = d.error
    ? `<span class="err">${d.error}</span>`
    : (d.message || '');
  document.title = d.running ? `${Math.round(d.percent||0)}% - Journal index` : 'Journal index';
}
tick(); setInterval(tick, 500);
</script>
</body>
</html>
"""


def progress_serve(host: str = PROGRESS_HOST, port: int = PROGRESS_PORT,
                   background: bool = False):
    """Serve the progress page. Loopback only, like everything else here.

    Deliberately decoupled from the indexer: the page reads whatever the last
    (or current) run wrote, so you can open it first and then start an index,
    or open it afterwards to see how the last run went.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                body = _PAGE.encode("utf-8")
                ctype = "text/html; charset=utf-8"
            elif path == "/progress.json":
                body = json.dumps(read_progress()).encode("utf-8")
                ctype = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the terminal readable
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, url
    print(f"Progress bar at {url}   (ctrl-c to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return httpd, url


# ── CLI ───────────────────────────────────────────────────────────────

def _fmt(row: dict, width: int = 96) -> str:
    when = (row.get("occurred_at") or "")[:16].replace("T", " ")
    kind = (row.get("kind") or "?")[:10]
    proj = (row.get("project") or "?")[:14]
    title = (row.get("title") or "").strip() or (row.get("body") or "").strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) > width:
        title = title[:width - 1] + "…"
    via = "+".join(sorted(row.get("_via") or {})) or "-"
    return f"  {when:16}  {proj:14}  {kind:10}  [{via:14}]  {title}"


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Semantic and lexical recall over the agent journal.")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("index", help="build or refresh the search index")
    pi.add_argument("--rebuild", action="store_true",
                    help="drop the text index and rebuild (vectors are kept)")
    pi.add_argument("--no-embed", action="store_true", help="lexical index only")
    pi.add_argument("--serve", action="store_true",
                    help="also serve the browser progress bar while indexing")
    pi.add_argument("--json", action="store_true")

    ps = sub.add_parser("search", help="ask the journal a question")
    ps.add_argument("text", nargs="+")
    ps.add_argument("-n", "--limit", type=int, default=10)
    ps.add_argument("--lexical", action="store_true", help="BM25 only")
    ps.add_argument("--semantic", action="store_true", help="embeddings only")
    ps.add_argument("--kind", default="")
    ps.add_argument("--project", default="")
    ps.add_argument("--json", action="store_true")

    pt = sub.add_parser("status", help="index size, vector coverage, embedder")
    pt.add_argument("--json", action="store_true")

    pp = sub.add_parser("progress-serve", help="serve the browser progress bar")
    pp.add_argument("--port", type=int, default=PROGRESS_PORT)
    pp.add_argument("--host", default=PROGRESS_HOST)

    args = p.parse_args(argv)

    if args.cmd == "index":
        httpd = None
        if args.serve:
            httpd, url = progress_serve(background=True)
            print(f"Progress bar at {url}")
        stats = reindex(rebuild=args.rebuild, embed=not args.no_embed)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(f"\n  scanned  {stats['scanned']}")
            print(f"  indexed  {stats['indexed']}   (skipped {stats['skipped']} unchanged)")
            print(f"  embedded {stats['embedded']}  (reused {stats['reused']} cached vectors)")
            for e in stats["errors"]:
                print(f"  ! {e}")
            print()
        if httpd is not None:
            print("Progress bar still serving. Ctrl-c to stop.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                print()
        return 1 if stats["errors"] and not stats["indexed"] else 0

    if args.cmd == "search":
        text = " ".join(args.text)
        lex = not args.semantic
        sem = not args.lexical
        rows = search(text, limit=args.limit, lexical=lex, semantic=sem,
                      kind=args.kind, project=args.project)
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        elif not rows:
            print(f"\n  nothing matched {text!r}\n")
        else:
            print(f"\n  {len(rows)} result(s) for {text!r}\n")
            for r in rows:
                print(_fmt(r))
            print()
        return 0

    if args.cmd == "status":
        info = status()
        if args.json:
            print(json.dumps(info, indent=2))
            return 0
        emb = info["embedder"]
        print(f"\n  index      {info['index_db']}")
        print(f"  entries    {info['entries']}"
              + (f"   (journal has {info['journal_entries']})"
                 if info["journal_entries"] is not None else ""))
        print(f"  vectors    {info['vectors']}   coverage {info['coverage'] * 100:.0f}%")
        print(f"  model      {info['embed_model'] or '-'}"
              f"   dim {info['embed_dim'] or '-'}")
        print(f"  embedder   {'ready' if emb['available'] else 'unavailable'}"
              f"   {emb['model_name']}")
        if not emb["available"]:
            if not emb["binary_ok"]:
                print(f"             missing binary: {emb['binary']}")
            if not emb["model_ok"]:
                print(f"             missing model:  {emb['model']}")
        print(f"  last index {info['last_index'] or 'never'}")
        if info["stale"]:
            print("  ! index is stale, run: python3 journal_search.py index")
        print()
        return 0

    if args.cmd == "progress-serve":
        progress_serve(host=args.host, port=args.port)
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
