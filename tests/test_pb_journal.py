"""
tests/test_pb_journal.py: the OS's PocketBase journal.

The point of these tests is the contract that matters at 2am: journaling must
never raise, never lose an entry, and stay queryable when PocketBase is down.
The live-server tests skip cleanly if PocketBase isn't running.
"""

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pb_journal  # noqa: E402


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """A pb_journal pointed at an empty spool with PocketBase unreachable."""
    spool = tmp_path / "spool.jsonl"
    monkeypatch.setattr(pb_journal, "SPOOL_PATH", spool)
    monkeypatch.setattr(pb_journal, "PB_DB", tmp_path / "nonexistent.db")

    class Dead(pb_journal.PB):
        def is_online(self, recheck: bool = False) -> bool:
            return False

    monkeypatch.setattr(pb_journal, "_client", Dead())
    return spool


@pytest.fixture
def live(monkeypatch):
    """Real PocketBase, pointed at the scratch collection, or skip.

    These tests write real records. They must never write them into
    `agent_journal`. That collection is the OS's actual memory, and a test
    run that leaves 15 `pytest-...` rows in it buries the entries worth
    recalling. Everything here goes to `agent_journal_test` and is deleted on
    teardown, so the real journal is untouched whether the run passes or not.
    """
    if not pb_journal.server_running():
        pytest.skip("PocketBase not running (python3 pb_journal.py serve)")

    pb_journal.client(fresh=True)
    pb = pb_journal.client()
    if not pb.has_collection(pb_journal.TEST_COLLECTION):
        pytest.skip(
            f"{pb_journal.TEST_COLLECTION} missing. Restart PocketBase to apply "
            "pb_migrations (python3 pb_journal.py stop && python3 pb_journal.py serve)"
        )

    monkeypatch.setattr(pb_journal, "COLLECTION", pb_journal.TEST_COLLECTION)
    yield pb

    # Scratch means scratch: drop everything, including rows a failed earlier
    # run left behind. deleteRule is "" on this collection only.
    while True:
        rows = pb.list(pb_journal.TEST_COLLECTION, limit=500) or []
        if not rows:
            break
        for row in rows:
            if not pb.delete(pb_journal.TEST_COLLECTION, row["id"]):
                return  # server went away mid-teardown; nothing worth failing over


# ── Entry construction ────────────────────────────────────────────────

def test_build_entry_derives_title_from_body():
    entry = pb_journal.build_entry("first line here\nsecond line", kind="note")
    assert entry["title"] == "first line here"
    assert entry["kind"] == "note"
    assert entry["entry_id"]
    assert entry["content_hash"]


def test_build_entry_truncates_long_title():
    entry = pb_journal.build_entry("x" * 400)
    assert len(entry["title"]) <= 121
    assert entry["title"].endswith("…")
    assert entry["body"] == "x" * 400  # body is never truncated


def test_entry_ids_are_unique_for_identical_bodies():
    a = pb_journal.build_entry("same text", kind="note")
    b = pb_journal.build_entry("same text", kind="note")
    assert a["entry_id"] != b["entry_id"]
    assert a["content_hash"] == b["content_hash"]  # …but dedupe-able by content


@pytest.mark.parametrize("raw,expected", [
    ("a,b,c", ["a", "b", "c"]),
    (["a", " b "], ["a", "b"]),
    ("", []),
    (None, []),
    ("a, ,b", ["a", "b"]),
])
def test_tag_normalization(raw, expected):
    assert pb_journal._norm_tags(raw) == expected


def test_pb_datetime_format():
    out = pb_journal._pb_datetime("2026-07-26T23:11:31.123456+00:00")
    assert out == "2026-07-26 23:11:31.123Z"


def test_pb_datetime_assumes_utc_for_naive():
    assert pb_journal._pb_datetime("2026-07-26T10:00:00").endswith("Z")


def test_filter_quoting_escapes_quotes():
    assert pb_journal._q('say "hi"') == '"say \\"hi\\""'


# ── Offline durability ────────────────────────────────────────────────

def test_journal_spools_when_pocketbase_is_down(offline):
    entry = pb_journal.journal("offline entry", kind="event", tags=["t"])
    assert entry["_stored"] == "spool"
    lines = offline.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["body"] == "offline entry"


def test_journal_never_raises_on_a_broken_client(offline, monkeypatch):
    class Exploding(pb_journal.PB):
        def is_online(self, recheck: bool = False) -> bool:
            raise RuntimeError("network stack on fire")

    monkeypatch.setattr(pb_journal, "_client", Exploding())
    with pytest.raises(RuntimeError):
        pb_journal.client().is_online()  # sanity: the fixture really does explode

    # journal() itself is allowed to propagate only if we let it. It must not.
    try:
        entry = pb_journal.journal("still needs to land")
    except Exception as exc:  # pragma: no cover - this is the failure we're testing
        pytest.fail(f"journal() raised {exc!r}; it must always be safe to call")
    assert entry["_stored"] == "spool"


def test_query_reads_unsynced_spool_entries(offline):
    pb_journal.journal("needle in the spool", kind="finding", tags=["alpha"])
    pb_journal.journal("unrelated", kind="note")

    assert len(pb_journal.query("needle")) == 1
    assert len(pb_journal.query(kind="finding")) == 1
    assert len(pb_journal.query(tags=["alpha"])) == 1
    assert len(pb_journal.query(tags=["nope"])) == 0
    assert len(pb_journal.query()) == 2


# ── Spool caps ────────────────────────────────────────────────────────

def test_spool_trim_drops_oldest_over_entry_cap(offline, monkeypatch):
    monkeypatch.setattr(pb_journal, "SPOOL_MAX_ENTRIES", 3)
    monkeypatch.setattr(pb_journal, "SPOOL_MAX_AGE_DAYS", 0)  # age cap off
    for i in range(6):
        pb_journal.journal(f"entry {i}", occurred_at=f"2026-07-2{i}T00:00:00+00:00")

    assert pb_journal._spool_trim() == 3
    bodies = [e["body"] for e in pb_journal._spool_read()]
    assert bodies == ["entry 3", "entry 4", "entry 5"], "newest survive, oldest go"
    assert pb_journal.spool_dropped() == 3


def test_spool_trim_drops_entries_past_the_age_cap(offline, monkeypatch):
    monkeypatch.setattr(pb_journal, "SPOOL_MAX_AGE_DAYS", 7)
    monkeypatch.setattr(pb_journal, "SPOOL_MAX_ENTRIES", 0)  # count cap off
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    pb_journal.journal("ancient", occurred_at=old)
    pb_journal.journal("recent", occurred_at=fresh)

    assert pb_journal._spool_trim() == 1
    assert [e["body"] for e in pb_journal._spool_read()] == ["recent"]


def test_spool_trim_is_a_noop_under_the_caps(offline):
    pb_journal.journal("well within limits")
    assert pb_journal._spool_trim() == 0
    assert len(pb_journal._spool_read()) == 1
    assert pb_journal.spool_dropped() == 0


def test_oversize_spool_trims_on_append(offline, monkeypatch):
    """The cap has to fire on the write path, not only when someone asks."""
    monkeypatch.setattr(pb_journal, "SPOOL_MAX_BYTES", 200)
    monkeypatch.setattr(pb_journal, "SPOOL_MAX_ENTRIES", 2)
    monkeypatch.setattr(pb_journal, "SPOOL_MAX_AGE_DAYS", 0)
    for i in range(10):
        pb_journal.journal(f"entry {i}")  # must not raise

    assert len(pb_journal._spool_read()) <= 2
    assert pb_journal.spool_dropped() > 0


def test_dropped_tally_follows_a_redirected_spool(offline, tmp_path):
    """The tally must land next to the spool it belongs to, not the real one."""
    assert pb_journal._dropped_path().parent == tmp_path
    assert not (pb_journal.STATE_DIR / "journal_spool.dropped").exists()


def test_doctor_flags_dropped_entries(offline, monkeypatch):
    monkeypatch.setattr(pb_journal, "SPOOL_MAX_ENTRIES", 1)
    monkeypatch.setattr(pb_journal, "SPOOL_MAX_AGE_DAYS", 0)
    pb_journal.journal("a")
    pb_journal.journal("b")
    pb_journal._spool_trim()
    assert pb_journal.doctor()["no_dropped_entries"]["ok"] is False


def test_query_text_match_is_case_insensitive(offline):
    pb_journal.journal("PocketBase Is Running")
    assert len(pb_journal.query("pocketbase")) == 1


def test_query_tag_filter_requires_all_tags(offline):
    pb_journal.journal("both", tags=["a", "b"])
    pb_journal.journal("one", tags=["a"])
    assert len(pb_journal.query(tags=["a", "b"])) == 1
    assert len(pb_journal.query(tags=["a"])) == 2


def test_query_respects_limit_and_orders_newest_first(offline):
    for i in range(5):
        pb_journal.journal(f"entry {i}", occurred_at=f"2026-07-2{i}T00:00:00+00:00")
    rows = pb_journal.query(limit=3)
    assert len(rows) == 3
    assert rows[0]["body"] == "entry 4"


def test_since_filter(offline):
    pb_journal.journal("old", occurred_at="2026-01-01T00:00:00+00:00")
    pb_journal.journal("new", occurred_at="2026-07-01T00:00:00+00:00")
    rows = pb_journal.query(since="2026-06-01T00:00:00+00:00")
    assert [r["body"] for r in rows] == ["new"]


def test_torn_spool_line_does_not_poison_the_read(offline):
    pb_journal.journal("good entry")
    with open(offline, "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")
    assert len(pb_journal.query()) == 1


def test_sync_is_a_noop_with_an_empty_spool(offline):
    result = pb_journal.sync()
    assert result["pending"] == 0 and result["synced"] == 0


def test_sync_leaves_the_spool_intact_when_offline(offline):
    pb_journal.journal("keep me")
    result = pb_journal.sync()
    assert result["synced"] == 0
    assert result["remaining"] == 1
    assert len(pb_journal._spool_read()) == 1


# ── Offline SQLite read path ──────────────────────────────────────────

def test_offline_query_reads_a_wal_database(tmp_path, monkeypatch):
    """The post-shutdown case: a WAL db with no -shm sidecar must stay readable."""
    db = tmp_path / "data.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE agent_journal (id TEXT, entry_id TEXT, occurred_at TEXT, "
        "kind TEXT, title TEXT, body TEXT, tags TEXT, project TEXT, session_id TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_journal VALUES (?,?,?,?,?,?,?,?,?)",
        ("1", "e1", "2026-07-26 00:00:00.000Z", "finding", "t", "sqlite body",
         '["x"]', "SimpleAgentOS", ""),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(pb_journal, "PB_DB", db)
    monkeypatch.setattr(pb_journal, "SPOOL_PATH", tmp_path / "spool.jsonl")

    assert pb_journal._table_exists() is True
    rows = pb_journal._query_sqlite("sqlite", "", [], "", "", "", "", 10)
    assert len(rows) == 1
    assert rows[0]["tags"] == ["x"]  # json field decoded


def test_offline_query_on_a_missing_db_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(pb_journal, "PB_DB", tmp_path / "nope.db")
    assert pb_journal._table_exists() is False
    assert pb_journal._query_sqlite("", "", [], "", "", "", "", 10) == []


# ── Live PocketBase ───────────────────────────────────────────────────

def test_live_roundtrip(live):
    marker = f"pytest-{uuid.uuid4().hex[:12]}"
    entry = pb_journal.journal(
        f"live roundtrip {marker}", kind="event", tags=["pytest", marker],
        source="pytest",
    )
    assert entry["_stored"] == "pocketbase", entry.get("_error")

    rows = pb_journal.query(marker)
    assert len(rows) == 1
    assert rows[0]["_source"] == "pocketbase"
    assert rows[0]["kind"] == "event"
    assert marker in rows[0]["tags"]


def test_live_and_offline_paths_agree(live):
    marker = f"pytest-{uuid.uuid4().hex[:12]}"
    pb_journal.journal(f"agreement check {marker}", kind="note", source="pytest")
    online_rows = pb_journal.query(marker, offline=False)
    offline_rows = pb_journal.query(marker, offline=True)
    assert len(online_rows) == len(offline_rows) == 1
    assert online_rows[0]["entry_id"] == offline_rows[0]["entry_id"]


def test_live_collection_exists(live):
    assert live.has_collection(pb_journal.COLLECTION) is True
    assert live.has_collection("definitely_not_a_collection") is False


def test_stats_shape(live):
    info = pb_journal.stats()
    assert info["online"] is True
    assert info["collection"] == pb_journal.COLLECTION
    assert isinstance(info["by_kind"], dict)
    assert info["entries"] >= 0


def test_live_tests_do_not_touch_the_real_journal(live):
    """The guard against the bug this fixture exists to fix.

    If COLLECTION ever stops being redirected, this test starts writing into
    the OS's real memory. Assert the redirect rather than trusting it.
    """
    assert pb_journal.COLLECTION == pb_journal.TEST_COLLECTION
    before = live.list("agent_journal", limit=500) or []
    pb_journal.journal("this must not reach agent_journal", kind="note", source="pytest")
    after = live.list("agent_journal", limit=500) or []
    assert len(after) == len(before)


def test_real_journal_refuses_deletes(live):
    """`agent_journal` has deleteRule null on purpose: memory is append only.

    Only the scratch twin allows deletes, which is what makes the teardown in
    the `live` fixture safe to point at a collection and empty it.
    """
    assert live.delete("agent_journal", "nonexistent0000") is False


# ── Supervision ───────────────────────────────────────────────────────

def test_doctor_reports_supervision():
    report = pb_journal.doctor()
    assert "supervised" in report
    assert isinstance(report["supervised"]["ok"], bool)
    assert report["supervised"]["fix"].endswith("launchd install")


def test_launchd_paths_are_consistent():
    assert pb_journal.LAUNCHD_SRC.name == f"{pb_journal.LAUNCHD_LABEL}.plist"
    assert pb_journal.LAUNCHD_DEST.name == pb_journal.LAUNCHD_SRC.name
    assert pb_journal.LAUNCHD_SRC.exists(), "plist ships in core_engine/"
    # The plist must point at loopback: agent_journal's access rules are public.
    assert "--http=127.0.0.1:8090" in pb_journal.LAUNCHD_SRC.read_text()


# ── Harness wiring ────────────────────────────────────────────────────

def test_claude_journal_mirror_is_best_effort(monkeypatch):
    """A blown-up pb_journal must not break claude_journal's markdown write."""
    import claude_journal

    def explode(*a, **k):
        raise RuntimeError("pb down")

    monkeypatch.setattr(pb_journal, "journal", explode)
    claude_journal._remember("text", "note", "thread", "2026-07-26")  # must not raise
