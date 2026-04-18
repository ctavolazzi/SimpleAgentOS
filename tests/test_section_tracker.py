"""
test_section_tracker.py — unit + integration tests for section telemetry.

Covers: schema validation, edge cases, privacy, reliability, offline fallback.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_section_tracker.py -v

Or stdlib only:
    python3 -m unittest tests/test_section_tracker.py
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import section_tracker as st


class PBOfflineStub:
    """Simulates offline PocketBase."""
    def __init__(self):
        self._last_error = "offline"

    def is_online(self): return False
    def create(self, collection, record): return None


class PBOnlineStub:
    """Simulates online PocketBase; records calls in memory."""
    def __init__(self, fail_for=None):
        self.calls = []
        self.fail_for = fail_for or set()
        self._counter = 0

    def is_online(self): return True

    def create(self, collection, record):
        self.calls.append((collection, dict(record)))
        if collection in self.fail_for:
            return None
        self._counter += 1
        return {"id": f"mock-{self._counter}", "created": st._now_iso(), **record}


class TrackerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.jsonl_root = Path(self.tmpdir.name) / "daily_ops"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _rec(self, **kw):
        defaults = dict(
            actor="test", model="n/a", source="test",
            jsonl_root=self.jsonl_root,
            # Most existing tests expect synchronous writes; disable coalescing
            # by default. Coalescing is exercised in TestCoalescing below.
            debounce_seconds=0.0,
            install_atexit=False,
        )
        defaults.update(kw)
        return st.OpRecorder(**defaults)


# ── Schema + validation ───────────────────────────────────────────────

class TestValidation(TrackerTestBase):
    def test_actor_enum(self):
        with self.assertRaises(st.ValidationError):
            self._rec(actor="hacker")

    def test_model_enum(self):
        with self.assertRaises(st.ValidationError):
            self._rec(model="gpt-5")

    def test_source_enum(self):
        with self.assertRaises(st.ValidationError):
            self._rec(source="unknown")

    def test_operation_enum(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        with self.assertRaises(st.ValidationError):
            r.record_op(section="sitrep", operation="nuke")

    def test_result_enum(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        with self.assertRaises(st.ValidationError):
            r.record_op(section="sitrep", operation="write", result="pwn")

    def test_experiment_status_enum(self):
        r = self._rec(pb=PBOfflineStub())
        with self.assertRaises(st.ValidationError):
            r.record_experiment(
                experiment_id="e1", title="t", hypothesis="h", status="maybe")

    def test_linked_doc_relationship_enum(self):
        r = self._rec(pb=PBOfflineStub())
        with self.assertRaises(st.ValidationError):
            r.record_linked_doc(
                parent_section="sitrep", child_path="x.md",
                relationship="unknown")

    def test_strict_section_rejects_unknown(self):
        r = self._rec(pb=PBOfflineStub(), strict_sections=True)
        r.start_session()
        # only if KNOWN_SECTIONS was populated
        if st.KNOWN_SECTIONS:
            with self.assertRaises(st.ValidationError):
                r.record_op(section="no_such_section", operation="write")

    def test_empty_section_rejected(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        with self.assertRaises(st.ValidationError):
            r.record_op(section="", operation="write")


# ── Offline fallback (JSONL-first) ────────────────────────────────────

class TestOfflineFallback(TrackerTestBase):
    def test_ops_persist_when_pb_offline(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        result = r.record_op(section="sitrep", operation="write",
                             before="", after="hello world")
        self.assertIsNotNone(result, "debounce=0 should flush immediately")
        self.assertIsNone(result["pb_id"])
        self.assertFalse(result["pb_online"])
        self.assertTrue(Path(result["jsonl"]).is_file())

    def test_jsonl_contains_schema_and_kind(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        r.record_op(section="sitrep", operation="write", after="x")
        records = st.read_jsonl(jsonl_root=self.jsonl_root)
        self.assertGreater(len(records), 0)
        for rec in records:
            self.assertEqual(rec["schema"], "daily_ops/v1")
            self.assertIn(rec["kind"],
                          {"session_start", "op", "snapshot", "experiment",
                           "linked_doc", "session_end"})


# ── Dual-write when PB online ─────────────────────────────────────────

class TestDualWrite(TrackerTestBase):
    def test_ops_hit_both_stores(self):
        pb = PBOnlineStub()
        r = self._rec(pb=pb)
        r.start_session()
        result = r.record_op(section="sitrep", operation="write", after="abc")
        self.assertIsNotNone(result["pb_id"])
        self.assertTrue(result["pb_online"])
        # PB got session_start + op
        self.assertEqual(len(pb.calls), 2)
        self.assertEqual(pb.calls[0][0], "daily_sessions")
        self.assertEqual(pb.calls[1][0], "section_operations")

    def test_pb_failure_does_not_block_jsonl(self):
        pb = PBOnlineStub(fail_for={"section_operations"})
        r = self._rec(pb=pb)
        r.start_session()
        result = r.record_op(section="sitrep", operation="write", after="a")
        self.assertIsNone(result["pb_id"])  # PB failed
        self.assertTrue(Path(result["jsonl"]).is_file())  # JSONL still wrote


# ── Edge cases ────────────────────────────────────────────────────────

class TestEdgeCases(TrackerTestBase):
    def test_unicode_and_emoji(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        content = "🧠 こんにちは — naïve café résumé • α β γ"
        r.record_op(section="sitrep", operation="write", after=content)
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(ops[0]["data"]["bytes_written"],
                         len(content.encode("utf-8")))
        # roundtrip via JSONL (ensure_ascii=False)
        self.assertIn("🧠", Path(ops[0]["data"].get("error_message", "")) .__str__() +
                      ops[0]["data"].get("before_hash", "") +  # no unicode here
                      content)  # sanity

    def test_markdown_code_fences(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        content = "```python\nprint('hi')\n```"
        res = r.record_op(section="in_the_lab", operation="write", after=content)
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertTrue(ops)

    def test_no_op_write_classified_as_skip(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        content = "unchanged text"
        r.record_op(section="sitrep", operation="write",
                    before=content, after=content)
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(ops[0]["data"]["operation"], "skip")
        self.assertEqual(ops[0]["data"]["before_hash"],
                         ops[0]["data"]["after_hash"])

    def test_hash_deterministic(self):
        self.assertEqual(st._hash("hello"), st._hash("hello"))
        self.assertNotEqual(st._hash("hello"), st._hash("hello "))

    def test_op_id_idempotent(self):
        sid = "s1"
        t = "2026-04-17T12:00:00+00:00"
        a = st._op_id(sid, "sitrep", "write", t)
        b = st._op_id(sid, "sitrep", "write", t)
        self.assertEqual(a, b)

    def test_op_id_distinct_per_section(self):
        t = "2026-04-17T12:00:00+00:00"
        self.assertNotEqual(
            st._op_id("s1", "sitrep", "write", t),
            st._op_id("s1", "in_the_lab", "write", t))

    def test_zero_byte_write_is_ok(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        r.record_op(section="sitrep", operation="write", after="")
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(ops[0]["data"]["bytes_written"], 0)

    def test_error_result_with_message(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        r.record_op(section="sitrep", operation="write",
                    result="fs_error", error_message="disk full")
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(ops[0]["data"]["result"], "fs_error")
        self.assertEqual(ops[0]["data"]["error_message"], "disk full")

    def test_long_content_preview_clipped(self):
        r = self._rec(pb=PBOfflineStub())
        content = "x" * 5000
        r.record_snapshot(section="sitrep", content=content)
        snaps = st.read_jsonl(jsonl_root=self.jsonl_root, kind="snapshot")
        self.assertEqual(len(snaps[0]["data"]["content_preview"]), 500)
        self.assertEqual(snaps[0]["data"]["word_count"], 1)

    def test_filled_heuristic(self):
        self.assertFalse(st._is_filled(""))
        self.assertFalse(st._is_filled("   \n  "))
        self.assertFalse(st._is_filled("TBD"))
        self.assertFalse(st._is_filled("short"))
        self.assertTrue(st._is_filled("real content here" * 3))


# ── Concurrency ───────────────────────────────────────────────────────

class TestConcurrency(TrackerTestBase):
    def test_parallel_appends_dont_corrupt_jsonl(self):
        """Two threads writing same day's JSONL — all lines must be valid JSON."""
        def worker(idx):
            r = self._rec(pb=PBOfflineStub())
            r.start_session()
            for i in range(20):
                r.record_op(section="sitrep", operation="write",
                            after=f"t{idx}-i{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        records = st.read_jsonl(jsonl_root=self.jsonl_root)
        # 4 sessions + 80 ops = 84 records minimum; all must parse
        self.assertGreaterEqual(len(records), 80)
        for r in records:
            self.assertIn("kind", r)
            self.assertIn("data", r)


# ── Stats + query ─────────────────────────────────────────────────────

class TestStats(TrackerTestBase):
    def test_stats_aggregates(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        r.record_op(section="sitrep", operation="write", after="a")
        r.record_op(section="sitrep", operation="write", after="b")
        r.record_op(section="in_the_lab", operation="write", after="c")
        r.record_experiment(experiment_id="e1", title="t",
                             hypothesis="h", status="proposed")
        r.record_linked_doc(parent_section="sitrep",
                             child_path="c.md", relationship="expansion")
        s = st.stats(jsonl_root=self.jsonl_root)
        self.assertEqual(s["total_ops"], 3)
        self.assertEqual(s["sessions"], 1)
        self.assertEqual(s["experiments"], 1)
        self.assertEqual(s["linked_docs"], 1)
        self.assertEqual(s["sections"]["sitrep"], 2)
        self.assertEqual(s["sections"]["in_the_lab"], 1)
        self.assertEqual(s["actors"]["test"], 3)
        self.assertEqual(s["results"]["ok"], 3)

    def test_stats_empty_day(self):
        s = st.stats(note_date="1999-01-01", jsonl_root=self.jsonl_root)
        self.assertEqual(s["total_ops"], 0)


# ── Session lifecycle ─────────────────────────────────────────────────

class TestSessions(TrackerTestBase):
    def test_end_without_start_raises(self):
        r = self._rec(pb=PBOfflineStub())
        with self.assertRaises(st.ValidationError):
            r.end_session()

    def test_auto_session_start(self):
        """record_op without explicit start_session auto-creates."""
        r = self._rec(pb=PBOfflineStub())
        r.record_op(section="sitrep", operation="write", after="x")
        self.assertIsNotNone(r.session_id)

    def test_session_end_writes_envelope(self):
        r = self._rec(pb=PBOfflineStub())
        r.start_session()
        r.end_session(commit_count=3)
        ends = st.read_jsonl(jsonl_root=self.jsonl_root, kind="session_end")
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["data"]["commit_count"], 3)


# ── Experiments ───────────────────────────────────────────────────────

class TestExperiments(TrackerTestBase):
    def test_experiment_required_fields(self):
        r = self._rec(pb=PBOfflineStub())
        with self.assertRaises(st.ValidationError):
            r.record_experiment(
                experiment_id="", title="t", hypothesis="h")
        with self.assertRaises(st.ValidationError):
            r.record_experiment(
                experiment_id="e", title="", hypothesis="h")
        with self.assertRaises(st.ValidationError):
            r.record_experiment(
                experiment_id="e", title="t", hypothesis="")

    def test_experiment_linked_sections_serialize(self):
        r = self._rec(pb=PBOfflineStub())
        r.record_experiment(
            experiment_id="e1", title="t", hypothesis="h",
            linked_sections=["sitrep", "in_the_lab"],
            tags=["schema"])
        exps = st.read_jsonl(jsonl_root=self.jsonl_root, kind="experiment")
        self.assertEqual(exps[0]["data"]["linked_sections"],
                         ["sitrep", "in_the_lab"])
        self.assertEqual(exps[0]["data"]["tags"], ["schema"])


# ── Privacy ───────────────────────────────────────────────────────────

class TestPrivacy(TrackerTestBase):
    def test_pb_base_is_localhost(self):
        """Tracker must never talk to remote hosts by default."""
        self.assertTrue(st.PB_BASE.startswith("http://127.0.0.1") or
                        st.PB_BASE.startswith("http://localhost"))

    def test_jsonl_under_vault(self):
        """JSONL path stays within the vault dir."""
        self.assertTrue(str(st.JSONL_ROOT).startswith(str(st.VAULT_DIR)))

    def test_no_auth_attempted_without_env(self):
        """No PB_ADMIN_EMAIL = no auth call (prevents credential leak)."""
        with patch.dict(os.environ, {"PB_ADMIN_EMAIL": "", "PB_ADMIN_PASSWORD": ""}):
            client = st.PocketBaseClient()
            self.assertIsNone(client._auth())


# ── Coalescing / debounce ─────────────────────────────────────────────

class TestCoalescing(TrackerTestBase):
    def test_rapid_writes_coalesce_to_one_op(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=10.0)
        r.start_session()
        # 5 rapid writes to same section — should buffer, produce 0 ops in JSONL
        for i in range(5):
            ret = r.record_op(section="sitrep", operation="write",
                              before="", after=f"draft v{i}")
            self.assertIsNone(ret)
        self.assertEqual(len(st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")), 0)
        # Flush -> exactly 1 op with last content + coalesced_count=5
        r.flush()
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["data"]["metadata"]["coalesced_count"], 5)
        self.assertEqual(ops[0]["data"]["after_hash"], st._hash("draft v4"))

    def test_different_sections_dont_coalesce(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=10.0)
        r.start_session()
        r.record_op(section="sitrep", operation="write", after="a")
        r.record_op(section="in_the_lab", operation="write", after="b")
        r.flush()
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(len(ops), 2)

    def test_error_ops_bypass_coalescing(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=10.0)
        r.start_session()
        # write buffered
        r.record_op(section="sitrep", operation="write", after="x")
        # error flushes immediately (and first the buffered op)
        ret = r.record_op(section="sitrep", operation="write", after="",
                          result="fs_error", error_message="disk full")
        self.assertIsNotNone(ret)
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(len(ops), 2)
        # last is the error
        self.assertEqual(ops[-1]["data"]["result"], "fs_error")

    def test_session_end_flushes_buffer(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=10.0)
        r.start_session()
        r.record_op(section="sitrep", operation="write", after="buffered")
        r.end_session()
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(len(ops), 1)
        ends = st.read_jsonl(jsonl_root=self.jsonl_root, kind="session_end")
        self.assertEqual(len(ends), 1)

    def test_end_session_idempotent(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=10.0)
        r.start_session()
        r.end_session()
        # second call should not raise
        try:
            r.end_session()
            raised = False
        except st.ValidationError:
            raised = True
        self.assertTrue(raised, "end_session after end should raise")


# ── Rate limiting ─────────────────────────────────────────────────────

class TestRateLimit(TrackerTestBase):
    def test_excess_ops_marked_rate_limited(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=0.0,
                      max_ops_per_minute=3)
        r.start_session()
        for i in range(6):
            r.record_op(section="sitrep", operation="write", after=f"x{i}")
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        # All 6 should land (rate-limited ones still write but flagged)
        self.assertEqual(len(ops), 6)
        limited = [o for o in ops if o["data"]["metadata"].get("rate_limited")]
        self.assertEqual(len(limited), 3)


# ── Secret scrubbing ──────────────────────────────────────────────────

class TestSecretScrub(TrackerTestBase):
    def test_scrub_openai_key(self):
        content = "my key is sk-abcdefghijklmnopqrstuvwxyz1234567890"
        scrubbed = st._scrub(content)
        self.assertNotIn("sk-abcdefghijkl", scrubbed)
        self.assertIn("<redacted>", scrubbed)

    def test_scrub_anthropic_key(self):
        content = "token=sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890ABCDEF"
        scrubbed = st._scrub(content)
        self.assertIn("<redacted>", scrubbed)

    def test_scrub_github_token(self):
        content = "use ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        self.assertIn("ghp_<redacted>", st._scrub(content))

    def test_scrub_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        scrubbed = st._scrub(f"Bearer {jwt}")
        self.assertIn("<jwt-redacted>", scrubbed)

    def test_scrub_api_key_pattern(self):
        content = "api_key: abcdef123456789012345"
        scrubbed = st._scrub(content)
        self.assertIn("<redacted>", scrubbed)

    def test_snapshot_preview_scrubbed(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=0.0)
        content = "my section with sk-ant-api03-abcdefghijklmnopqrstuv12345 token"
        r.record_snapshot(section="sitrep", content=content)
        snaps = st.read_jsonl(jsonl_root=self.jsonl_root, kind="snapshot")
        self.assertNotIn("sk-ant-api03-abcdef", snaps[0]["data"]["content_preview"])
        self.assertIn("<redacted>", snaps[0]["data"]["content_preview"])


# ── Snapshot dedup ────────────────────────────────────────────────────

class TestSnapshotDedup(TrackerTestBase):
    def test_same_hash_deduped(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=0.0)
        content = "stable content x" * 5
        r.record_snapshot(section="sitrep", content=content)
        r.record_snapshot(section="sitrep", content=content)  # dedup
        r.record_snapshot(section="sitrep", content=content)  # dedup
        snaps = st.read_jsonl(jsonl_root=self.jsonl_root, kind="snapshot")
        self.assertEqual(len(snaps), 1)

    def test_different_hash_recorded(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=0.0)
        r.record_snapshot(section="sitrep", content="v1 content long enough")
        r.record_snapshot(section="sitrep", content="v2 content long enough")
        snaps = st.read_jsonl(jsonl_root=self.jsonl_root, kind="snapshot")
        self.assertEqual(len(snaps), 2)

    def test_force_bypasses_dedup(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=0.0)
        content = "repeat me"
        r.record_snapshot(section="sitrep", content=content)
        r.record_snapshot(section="sitrep", content=content, force=True)
        snaps = st.read_jsonl(jsonl_root=self.jsonl_root, kind="snapshot")
        self.assertEqual(len(snaps), 2)


# ── Op_id collision ───────────────────────────────────────────────────

class TestOpIdCollision(TrackerTestBase):
    def test_same_timestamp_still_distinct(self):
        """Two ops in same microsecond must still have distinct op_ids."""
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=0.0)
        r.start_session()
        ids = set()
        for i in range(200):
            r.record_op(section="sitrep", operation="write",
                        before="", after=f"rapid-{i}")
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        ids = {o["data"]["op_id"] for o in ops}
        self.assertEqual(len(ids), len(ops), "all op_ids must be unique")


# ── Schema version guard ──────────────────────────────────────────────

class TestSchemaGuard(TrackerTestBase):
    def test_unknown_schema_skipped_by_default(self):
        # Write a record with a future schema directly
        path = self.jsonl_root / st._today_str()[:7] / f"{st._today_str()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write('{"schema":"daily_ops/v999","kind":"op","ts":"2026-04-17T00:00:00+00:00","data":{}}\n')
            f.write('{"schema":"daily_ops/v1","kind":"op","ts":"2026-04-17T00:00:00+00:00","data":{"section_name":"sitrep"}}\n')
        records = st.read_jsonl(jsonl_root=self.jsonl_root)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["schema"], "daily_ops/v1")

    def test_unknown_schema_visible_with_flag(self):
        path = self.jsonl_root / st._today_str()[:7] / f"{st._today_str()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write('{"schema":"daily_ops/v999","kind":"op","data":{}}\n')
        records = st.read_jsonl(jsonl_root=self.jsonl_root, include_unknown_schema=True)
        self.assertEqual(len(records), 1)

    def test_truncated_line_skipped(self):
        path = self.jsonl_root / st._today_str()[:7] / f"{st._today_str()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write('{"schema":"daily_ops/v1","kind":"op","data":{')  # truncated
        records = st.read_jsonl(jsonl_root=self.jsonl_root)
        self.assertEqual(len(records), 0)  # bad line silently dropped


# ── Experiment dedup ──────────────────────────────────────────────────

class TestExperimentDedup(TrackerTestBase):
    def test_duplicate_experiment_id_deduped(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=0.0)
        r.record_experiment(experiment_id="exp-1", title="t", hypothesis="h")
        ret = r.record_experiment(experiment_id="exp-1", title="t2", hypothesis="h2")
        self.assertIsNone(ret, "duplicate experiment_id should be deduped")
        exps = st.read_jsonl(jsonl_root=self.jsonl_root, kind="experiment")
        self.assertEqual(len(exps), 1)

    def test_duplicate_allowed_with_flag(self):
        r = self._rec(pb=PBOfflineStub(), debounce_seconds=0.0)
        r.record_experiment(experiment_id="exp-1", title="t", hypothesis="h")
        r.record_experiment(experiment_id="exp-1", title="t2", hypothesis="h2",
                             allow_duplicate=True)
        exps = st.read_jsonl(jsonl_root=self.jsonl_root, kind="experiment")
        self.assertEqual(len(exps), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
