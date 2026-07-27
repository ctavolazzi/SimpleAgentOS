"""
tests/test_journal_search.py: the journal's search index.

Two things matter here. First, the index is derived data and must never be
load bearing: every degraded path (no embedder, no index, no numpy, a broken
llama.cpp) has to return rows rather than raise. Second, the index is a cache
keyed on content_hash, so re-indexing unchanged entries must not re-embed them.

Every test points INDEX_DB at tmp_path. The real index lives next to the real
journal, and a test run that rebuilds it, or worse embeds into it, would make
the developer's own search results depend on whether pytest ran.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import journal_search as js  # noqa: E402
import pb_journal  # noqa: E402


ROWS = [
    {"entry_id": "e1", "content_hash": "h1", "occurred_at": "2026-07-27T10:00:00Z",
     "kind": "finding", "project": "SimpleAgentOS", "actor": "claude",
     "title": "The spool had no cap and grew without limit",
     "body": "An unbounded outbox on the write path of a never-blocks API.",
     "tags": ["spool", "bug"], "importance": 0.8},
    {"entry_id": "e2", "content_hash": "h2", "occurred_at": "2026-07-27T11:00:00Z",
     "kind": "decision", "project": "NovaSystem", "actor": "claude",
     "title": "PocketBase runs under launchd now",
     "body": "RunAtLoad and KeepAlive so the store survives a crash.",
     "tags": ["launchd"], "importance": 0.6},
    {"entry_id": "e3", "content_hash": "h3", "occurred_at": "2026-07-27T12:00:00Z",
     "kind": "note", "project": "CivicOS", "actor": "claude",
     "title": "Access-Control-Allow-Origin is * by default",
     "body": "Which is why the browser can POST with no proxy in front.",
     "tags": ["cors"], "importance": 0.5},
]


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Search pointed at a disposable index, fed from a fixed row set.

    Pointing INDEX_DB somewhere disposable is the whole isolation story: the
    real index is a file next to the real journal, and tests must not touch it.
    """
    monkeypatch.setattr(js, "INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr(js, "PROGRESS_PATH", tmp_path / "progress.json")
    monkeypatch.setattr(pb_journal, "query", lambda *a, **k: [dict(r) for r in ROWS])
    monkeypatch.setattr(js, "embedder_available", lambda: False)
    return tmp_path


@pytest.fixture
def idx(wired):
    """`wired`, with the index actually built.

    Building it matters: `search()` falls back to pb_journal's substring path
    when no index file exists, and a test that forgot to index would quietly
    assert against the very code path it meant to replace, and pass.
    """
    js.reindex()
    return wired


# ── Indexing ──────────────────────────────────────────────────────────

def test_index_populates_entries_and_fts(wired):
    stats = js.reindex()
    assert stats["scanned"] == 3
    assert stats["indexed"] == 3
    conn = js.connect()
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM entries_fts").fetchone()["c"] == 3
    conn.close()


def test_reindex_is_incremental(wired):
    js.reindex()
    again = js.reindex()
    # Same content_hash means nothing changed, so nothing is rewritten.
    assert again["indexed"] == 0
    assert again["skipped"] == 3


def test_changed_content_hash_reindexes_that_entry(wired, monkeypatch):
    js.reindex()
    changed = [dict(r) for r in ROWS]
    changed[0]["content_hash"] = "h1-changed"
    changed[0]["title"] = "The spool is capped at 8 MiB now"
    monkeypatch.setattr(pb_journal, "query", lambda *a, **k: changed)
    stats = js.reindex()
    assert stats["indexed"] == 1
    assert stats["skipped"] == 2
    hits = js.search("capped 8 MiB", limit=5)
    assert any(h["entry_id"] == "e1" for h in hits)


def test_reindex_does_not_duplicate_fts_rows(wired, monkeypatch):
    """An upsert that only INSERTed would silently double every entry's weight
    in BM25 on the second run, which is the kind of bug that shows up as
    'search got worse' months later."""
    js.reindex()
    changed = [dict(r) for r in ROWS]
    changed[0]["content_hash"] = "h1-changed"
    monkeypatch.setattr(pb_journal, "query", lambda *a, **k: changed)
    js.reindex()
    conn = js.connect()
    n = conn.execute("SELECT COUNT(*) c FROM entries_fts WHERE entry_id='e1'").fetchone()["c"]
    conn.close()
    assert n == 1


# ── Lexical ranking ───────────────────────────────────────────────────

def test_search_finds_by_word_not_substring(idx):
    """The whole point. `query()` needed the literal characters; BM25 tokenizes,
    so a word order that never appears verbatim still matches."""
    hits = js.search("limit grew spool", limit=5)
    assert hits and hits[0]["entry_id"] == "e1"


def test_search_stems(idx):
    """porter tokenizer: 'running' should reach 'runs'."""
    hits = js.search("running under launchd", limit=5)
    assert any(h["entry_id"] == "e2" for h in hits)


def test_fts_query_quotes_terms():
    """Unquoted user text is an injection surface into FTS5's query grammar."""
    assert js._fts_query('spool cap') == '"spool" OR "cap"'
    assert js._fts_query("") == ""


def test_search_survives_fts_metacharacters(idx):
    """A stray quote or NEAR used to be a syntax error, not a search."""
    for nasty in ['spool " cap', "NEAR(a b)", "*", 'title~"x"', "a AND OR b"]:
        assert isinstance(js.search(nasty, limit=3), list)


def test_filters_apply(idx):
    assert all(h["project"] == "CivicOS"
               for h in js.search("browser proxy origin", project="CivicOS"))
    assert all(h["kind"] == "decision"
               for h in js.search("launchd pocketbase", kind="decision"))


def test_tags_round_trip_as_a_list(idx):
    js.reindex()
    hit = next(h for h in js.search("spool limit", limit=5) if h["entry_id"] == "e1")
    assert hit["tags"] == ["spool", "bug"]


# ── Degradation ───────────────────────────────────────────────────────

def test_search_without_index_falls_back_to_pb_journal(tmp_path, monkeypatch):
    """No index file at all still has to answer, using the old substring path."""
    monkeypatch.setattr(js, "INDEX_DB", tmp_path / "absent.db")
    monkeypatch.setattr(pb_journal, "query",
                        lambda *a, **k: [{"entry_id": "e1", "title": "fallback"}])
    hits = js.search("anything")
    assert hits and hits[0]["_via"] == {"substring": {}}


def test_index_without_embedder_is_lexical_only(wired):
    stats = js.reindex()
    assert stats["embedded"] == 0
    assert any("lexical only" in e for e in stats["errors"])
    # And still searchable.
    assert js.search("spool limit", limit=3)


def test_broken_embedder_never_raises(wired, monkeypatch):
    monkeypatch.setattr(js, "embedder_available", lambda: True)
    monkeypatch.setattr(js, "_embed_batch",
                        lambda batch: (_ for _ in ()).throw(RuntimeError("boom")))
    stats = js.reindex()
    assert stats["embedded"] == 0
    assert js.search("spool limit", limit=3)


def test_misaligned_embedder_output_is_discarded(wired, monkeypatch):
    """A short batch means we cannot tell which vector belongs to which entry.
    Storing them anyway would silently mis-key the index forever."""
    monkeypatch.setattr(js, "embedder_available", lambda: True)
    monkeypatch.setattr(js, "_embed_batch", lambda batch: [[0.1] * 384])  # 1 for N
    assert js.embed_texts(["a", "b", "c"]) == []


def test_semantic_ranking_when_vectors_exist(wired, monkeypatch):
    """A fake embedder that puts the query next to e3 must rank e3 first, even
    though the query shares no words with it."""
    vecs = {"h1": [1.0, 0.0, 0.0], "h2": [0.0, 1.0, 0.0], "h3": [0.0, 0.0, 1.0]}
    order = ["h1", "h2", "h3"]
    monkeypatch.setattr(js, "embedder_available", lambda: True)
    monkeypatch.setattr(js, "embed_texts",
                        lambda texts, progress=None: [vecs[h] for h in order[:len(texts)]])
    js.reindex()
    monkeypatch.setattr(js, "embed_one", lambda text: [0.0, 0.0, 1.0])
    hits = js.search("zzzz", limit=3, lexical=False)
    assert hits[0]["entry_id"] == "e3"


def test_dimension_mismatch_is_ignored_not_crashed(wired, monkeypatch):
    """Swapping the embedding model changes the dimension. Old vectors must be
    skipped rather than blowing up the search path."""
    monkeypatch.setattr(js, "embedder_available", lambda: True)
    monkeypatch.setattr(js, "embed_texts",
                        lambda texts, progress=None: [[0.5] * 8 for _ in texts])
    js.reindex()
    monkeypatch.setattr(js, "embed_one", lambda text: [0.5] * 384)  # different dim
    assert isinstance(js.search("spool", limit=3), list)


# ── Vector cache ──────────────────────────────────────────────────────

def test_vectors_are_cached_by_content_hash(wired, monkeypatch):
    calls = []

    def fake(texts, progress=None):
        calls.append(len(texts))
        return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr(js, "embedder_available", lambda: True)
    monkeypatch.setattr(js, "embed_texts", fake)
    js.reindex()
    assert calls == [3]
    stats = js.reindex()
    # Nothing changed, so nothing is embedded a second time.
    assert calls == [3]
    assert stats["reused"] == 3


def test_identical_content_embeds_once(wired, monkeypatch):
    """content_hash, not entry_id, is the cache key: two entries carrying the
    same text should cost one embedding, not two."""
    dupes = [dict(r) for r in ROWS]
    dupes.append({**ROWS[0], "entry_id": "e4"})  # same content_hash h1
    monkeypatch.setattr(pb_journal, "query", lambda *a, **k: dupes)
    seen = []
    monkeypatch.setattr(js, "embedder_available", lambda: True)
    monkeypatch.setattr(js, "embed_texts",
                        lambda texts, progress=None: (seen.append(len(texts)),
                                                      [[0.1] * 8 for _ in texts])[1])
    js.reindex()
    assert seen == [3]  # four entries, three distinct hashes


def test_rebuild_keeps_vectors(wired, monkeypatch):
    monkeypatch.setattr(js, "embedder_available", lambda: True)
    monkeypatch.setattr(js, "embed_texts",
                        lambda texts, progress=None: [[0.2] * 8 for _ in texts])
    js.reindex()
    stats = js.reindex(rebuild=True)
    # Rebuilding the text index has no reason to throw away expensive vectors.
    assert stats["embedded"] == 0
    assert stats["reused"] == 3


# ── Embedding input hygiene ───────────────────────────────────────────

def test_flatten_collapses_newlines():
    """llama-embedding reads one text per LINE. A newline inside a body would
    split one entry into several vectors and misalign every later result."""
    assert "\n" not in js._flatten("a\nb\r\nc\td")
    assert js._flatten("a\nb") == "a b"


def test_flatten_never_returns_empty():
    assert js._flatten("") == "(empty)"
    assert js._flatten("   \n  ") == "(empty)"


def test_flatten_truncates(monkeypatch):
    monkeypatch.setattr(js, "EMBED_MAX_CHARS", 10)
    assert len(js._flatten("x" * 500)) == 10


# ── Fusion ────────────────────────────────────────────────────────────

def test_semantic_outranks_bm25_when_they_disagree():
    """Measured, not assumed: see _fuse's docstring. Semantic decides order."""
    fused = js._fuse(lexical=[("a", 9.0)], semantic=[("b", 0.9)])
    assert [eid for eid, _ in fused] == ["b", "a"]


def test_fuse_records_both_rankers_for_an_agreed_hit():
    fused = dict(js._fuse(lexical=[("a", 1.0), ("b", 0.5)],
                          semantic=[("a", 0.9), ("c", 0.4)]))
    assert set(fused["a"]["via"]) == {"bm25", "semantic"}
    assert set(fused["c"]["via"]) == {"semantic"}
    assert set(fused["b"]["via"]) == {"bm25"}


def test_fuse_backfills_entries_semantic_could_not_see():
    """An entry indexed but not yet embedded has no vector, so the semantic
    ranker cannot return it at all. Lexical is what keeps it findable."""
    fused = js._fuse(lexical=[("unembedded", 4.0)], semantic=[("a", 0.9)])
    assert [eid for eid, _ in fused] == ["a", "unembedded"]


def test_fuse_preserves_semantic_order():
    fused = js._fuse(lexical=[("c", 9.0)], semantic=[("a", 0.9), ("b", 0.8), ("c", 0.1)])
    assert [eid for eid, _ in fused] == ["a", "b", "c"]


def test_lexical_alone_ranks_when_there_is_no_semantic_side():
    fused = js._fuse(lexical=[("a", 9.0), ("b", 3.0)], semantic=[])
    assert [eid for eid, _ in fused] == ["a", "b"]


def test_search_reports_which_ranker_matched(idx):
    hits = js.search("spool limit", limit=3)
    assert hits and "bm25" in hits[0]["_via"]


# ── Progress ──────────────────────────────────────────────────────────

def test_progress_writes_readable_json(tmp_path, monkeypatch):
    monkeypatch.setattr(js, "PROGRESS_PATH", tmp_path / "p.json")
    p = js.Progress(path=tmp_path / "p.json", total=10, phase="embedding")
    p.update(done=5)
    got = json.loads((tmp_path / "p.json").read_text())
    assert got["percent"] == 50.0
    assert got["running"] is True
    p.finish(message="ok")
    got = json.loads((tmp_path / "p.json").read_text())
    assert got["running"] is False and got["phase"] == "done"


def test_progress_failure_never_breaks_indexing(wired, monkeypatch):
    """Losing the progress display must not be able to fail the run."""
    monkeypatch.setattr(js, "PROGRESS_PATH", Path("/nonexistent/dir/p.json"))
    stats = js.reindex()
    assert stats["indexed"] == 3


def test_read_progress_with_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(js, "PROGRESS_PATH", tmp_path / "missing.json")
    assert js.read_progress()["phase"] == "idle"


# ── Status ────────────────────────────────────────────────────────────

def test_status_reports_coverage(wired, monkeypatch):
    monkeypatch.setattr(js, "embedder_available", lambda: True)
    monkeypatch.setattr(js, "embed_texts",
                        lambda texts, progress=None: [[0.3] * 8 for _ in texts])
    js.reindex()
    info = js.status()
    assert info["entries"] == 3 and info["vectors"] == 3
    assert info["coverage"] == 1.0


def test_status_without_index(tmp_path, monkeypatch):
    monkeypatch.setattr(js, "INDEX_DB", tmp_path / "none.db")
    assert js.status()["index_exists"] is False
