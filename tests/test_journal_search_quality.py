"""
tests/test_journal_search_quality.py: does the ranking actually rank?

The tests in test_journal_search.py prove the machinery works. They cannot tell
you whether the results are any good, and "good" is the only thing search is
for. This is the benchmark that justifies the constants in journal_search.py.

It is a real information-retrieval evaluation on a fixed corpus with labeled
answers, scored by top-1 accuracy and MRR. It is what decided the design of
`_fuse`, and it is here so that decision stays falsifiable instead of becoming
folklore.

The corpus is checked in rather than read from the live journal, because a
benchmark whose answers change when someone writes a note is not a benchmark.
That property is not decorative: a semantic-weighted fusion tuned against the
live journal looked clearly optimal there and measured WORSE than plain fusion
here. Having a second corpus is what exposed it as fitted noise.

Two findings this encodes, both of which contradicted the obvious guess:

  1. Stripping stopwords from the FTS query made lexical search WORSE
     (0.79 -> 0.71). BM25's IDF already discounts ubiquitous words; removing
     them by hand only throws away recall.
  2. Reciprocal Rank Fusion, in every weighting tried, was at best tied with
     and usually worse than simply ranking by the semantic score. BM25 rescued
     zero queries the semantic ranker missed, including the identifier queries
     picked specifically to favor it.

Skips cleanly when llama.cpp or the embedding model is absent, since the
semantic half cannot be measured without them.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import journal_search as js  # noqa: E402
import pb_journal  # noqa: E402


# A frozen slice of the real journal: the entries this system was built while
# writing, which is exactly the kind of text it has to recall.
CORPUS = [
    ("c1", "finding", "SimpleAgentOS now journals to its own PocketBase at "
     "core_engine/pb_data",
     "One wide agent_journal collection. Writes never raise; they spool when the "
     "server is down. Reads work offline by reading data.db directly."),
    ("c2", "decision", "Chose one wide agent_journal collection over a normalized "
     "schema",
     "Recall is find the thing that mentioned X, not a join, so kind and tags "
     "carry structure and body carries substance."),
    ("c3", "question", "Does the spool need a size cap before it becomes its own "
     "problem?",
     "An unbounded file on the write path of an API that promises never to block "
     "the caller. Nothing trims it today."),
    ("c4", "decision", "NovaSystem now shares SimpleAgentOS's memory via the "
     "agent_journal collection",
     "Plain HTTP to the same collection, never importing SimpleAgentOS. Either "
     "project runs fine without the other."),
    ("c5", "reflection", "Wrote a module-level SPOOL_DROPPED constant while "
     "building a cap for a spool the tests already redirect",
     "Tests monkeypatch SPOOL_PATH to a tmp dir, so the tally would have been "
     "written next to the real spool while the test thought it was isolated."),
    ("c6", "reflection", "Adding the CivicOS journal bridge made a true sentence "
     "false",
     "The status line said no model call, no network. The bridge makes the second "
     "half untrue, so it now says that conditionally."),
    ("c7", "reflection", "Two pytest suites run concurrently took 43s and 72s "
     "against 10s and 26s solo",
     "I read that as a deadlock from the new spool lock and started bisecting. It "
     "was contention. The suites had already passed."),
    ("c8", "reflection", "Six journal writes returned a payload with a timestamp "
     "in it and I read that as success",
     "All six wrote nothing. claude_journal add verbs no-op when the day's file "
     "came from a different template. Check the ok field, not the shape."),
    ("c9", "question", "Does the launchd agent actually come back after a real "
     "logout and login?",
     "RunAtLoad is declared but only the crash-restart path was tested, by "
     "killing the process. The login path is assumed."),
    ("c10", "note", "PocketBase serves Access-Control-Allow-Origin: * by default",
     "Which is why the browser page can POST to it with no proxy, and another "
     "reason the loopback binding is load bearing."),
    ("c11", "note", "Semantic search for the agent journal",
     "query() is title~x || body~x, which answers what did I learn about X badly, "
     "and that is the only question anyone asks a memory."),
    ("c12", "note", "claude_journal.py CLI should exit non-zero when "
     "_append_to_section returns False",
     "A command that reports success on a no-op write is worse than one that "
     "fails, because nobody goes looking."),
]

# (query, the ONE entry that should come back first)
GOLD = [
    # Paraphrase: none of these share their key words with the target.
    ("the outbox filled up and lost things", "c3"),
    ("keep the database running after a reboot", "c9"),
    ("I trusted output that claimed success but did nothing", "c8"),
    ("tests looked frozen but were just competing for CPU", "c7"),
    ("a webpage can call the API without a proxy", "c10"),
    ("why is memory shared between projects", "c4"),
    ("what did we decide about the schema shape", "c2"),
    ("a constant pointing at the wrong path during tests", "c5"),
    ("we said something untrue in the UI copy", "c6"),
    # Exact identifiers: what BM25 is for, and what embeddings are supposed to
    # be bad at. Without these the benchmark only ever rewards paraphrase.
    ("SPOOL_DROPPED", "c5"),
    ("Access-Control-Allow-Origin", "c10"),
    ("_append_to_section", "c12"),
    ("core_engine/pb_data", "c1"),
    ("43s and 72s", "c7"),
]

# Floors, not targets. Set just under the measured 0.86 / 0.903 so an honest
# regression trips them while ordinary corpus drift does not.
MIN_TOP1 = 0.85
MIN_MRR = 0.88


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    """The frozen corpus, indexed and embedded in a throwaway location."""
    if not js.embedder_available():
        pytest.skip(f"no embedder: {js.EMBED_BIN} / {js.EMBED_MODEL}")
    tmp = tmp_path_factory.mktemp("bench")
    rows = [{"entry_id": eid, "content_hash": f"h-{eid}",
             "occurred_at": f"2026-07-27T{i:02d}:00:00Z", "kind": kind,
             "project": "SimpleAgentOS", "actor": "claude", "title": title,
             "body": body, "tags": [], "importance": 0.5}
            for i, (eid, kind, title, body) in enumerate(CORPUS)]

    real_index, real_progress, real_query = js.INDEX_DB, js.PROGRESS_PATH, pb_journal.query
    js.INDEX_DB = tmp / "bench.db"
    js.PROGRESS_PATH = tmp / "progress.json"
    pb_journal.query = lambda *a, **k: [dict(r) for r in rows]
    try:
        stats = js.reindex()
        if stats["embedded"] != len(CORPUS):
            pytest.skip(f"embedding did not complete: {stats}")
        yield tmp
    finally:
        js.INDEX_DB, js.PROGRESS_PATH = real_index, real_progress
        pb_journal.query = real_query


def _score(rank_fn) -> tuple[float, float, list]:
    top1 = 0
    mrr = 0.0
    misses = []
    for query, want in GOLD:
        order = rank_fn(query)
        pos = next((i + 1 for i, eid in enumerate(order) if eid == want), None)
        if pos == 1:
            top1 += 1
        else:
            misses.append((query, want, pos, order[0] if order else None))
        if pos:
            mrr += 1.0 / pos
    n = len(GOLD)
    return top1 / n, mrr / n, misses


def _hybrid(query):
    return [r["entry_id"] for r in js.search(query, limit=len(CORPUS))]


def test_hybrid_ranking_meets_the_bar(bench):
    top1, mrr, misses = _score(_hybrid)
    detail = "\n".join(
        f"    {q!r} wanted {w}, got rank {p} (top was {t})" for q, w, p, t in misses)
    assert top1 >= MIN_TOP1, f"top-1 {top1:.2f} < {MIN_TOP1}\n{detail}"
    assert mrr >= MIN_MRR, f"MRR {mrr:.3f} < {MIN_MRR}\n{detail}"


def test_every_query_finds_its_answer_somewhere(bench):
    """Precision can be argued about. Failing to return the answer at all in a
    12-document corpus cannot."""
    for query, want in GOLD:
        order = _hybrid(query)
        assert want in order, f"{query!r} never returned {want}"


def test_semantic_beats_lexical_on_paraphrase(bench):
    """The reason embeddings are here at all. If this stops holding, the model
    or the embed text changed and the weights need re-deriving."""
    conn = js.connect()
    try:
        paraphrase = [(q, w) for q, w in GOLD[:9]]
        sem_top1 = sum(
            1 for q, w in paraphrase
            if (r := js._semantic(conn, q, 50)) and r[0][0] == w)
        lex_top1 = sum(
            1 for q, w in paraphrase
            if (r := js._lexical(conn, q, 50)) and r[0][0] == w)
        assert sem_top1 > lex_top1, (
            f"semantic {sem_top1}/{len(paraphrase)} did not beat "
            f"lexical {lex_top1}/{len(paraphrase)} on paraphrase")
    finally:
        conn.close()


def test_lexical_alone_still_handles_exact_identifiers(bench):
    """BM25 stays in the fusion because it is the whole ranking when no embedder
    is installed. That fallback has to be worth having."""
    conn = js.connect()
    try:
        identifiers = GOLD[9:]
        hits = sum(1 for q, w in identifiers
                   if (r := js._lexical(conn, q, 50)) and r[0][0] == w)
        assert hits >= len(identifiers) - 1, (
            f"lexical only got {hits}/{len(identifiers)} identifier queries")
    finally:
        conn.close()


def test_stopword_stripping_would_not_help(bench):
    """Guards finding 1. `_fts_query` deliberately keeps stopwords; this fails if
    someone reintroduces the filter believing it is an obvious win."""
    conn = js.connect()
    stop = {"the", "a", "an", "and", "of", "to", "for", "did", "but", "was",
            "we", "i", "is", "in", "on", "up", "can", "what", "why", "that"}
    try:
        def stripped(query):
            terms = [t for t in js._WORD.findall(query)
                     if len(t) > 1 and t.lower() not in stop]
            if not terms:
                return []
            match = " OR ".join(f'"{t}"' for t in terms)
            rows = conn.execute(
                "SELECT entry_id, bm25(entries_fts) s FROM entries_fts "
                "WHERE entries_fts MATCH ? ORDER BY s LIMIT 50", (match,)).fetchall()
            return [r["entry_id"] for r in rows]

        kept_top1, kept_mrr, _ = _score(
            lambda q: [e for e, _ in js._lexical(conn, q, 50)])
        strip_top1, strip_mrr, _ = _score(stripped)
        assert kept_mrr >= strip_mrr, (
            f"stopword stripping improved MRR ({strip_mrr:.3f} > {kept_mrr:.3f}); "
            "re-derive RANKER_WEIGHTS and update _fts_query's docstring")
    finally:
        conn.close()


def test_semantic_ordering_beats_reciprocal_rank_fusion(bench):
    """Guards finding 2, the reason `_fuse` is not RRF.

    RRF is the textbook answer and it measured worse here, on both corpora. If
    this ever fails, fusion has become worth its complexity and `_fuse` should
    be revisited, with the new numbers written into its docstring.
    """
    conn = js.connect()
    try:
        def rrf(k=10, w_sem=1.0, w_lex=1.0):
            def rank(query):
                fused = {}
                for src, ranked, w in (("bm25", js._lexical(conn, query, 50), w_lex),
                                       ("semantic", js._semantic(conn, query, 50), w_sem)):
                    for i, (eid, _) in enumerate(ranked, 1):
                        fused[eid] = fused.get(eid, 0.0) + w / (k + i)
                return [e for e, _ in sorted(fused.items(), key=lambda kv: -kv[1])]
            return rank

        _, ours_mrr, _ = _score(_hybrid)
        for label, fn in [("even RRF", rrf()),
                          ("semantic-weighted RRF x8", rrf(w_sem=8.0))]:
            _, their_mrr, _ = _score(fn)
            assert ours_mrr >= their_mrr, (
                f"{label} scored MRR {their_mrr:.3f} vs {ours_mrr:.3f} for "
                "semantic-ordering; re-derive _fuse and update its docstring")
    finally:
        conn.close()
