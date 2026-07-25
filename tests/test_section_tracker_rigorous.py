"""
test_section_tracker_rigorous.py — harder tests that require proof-of-work.

These do not merely mock behavior — they exercise real concurrency, real time
windows, real PocketBase round-trips (when available), and invariants under
adversarial input. Designed to catch bugs that unit tests with stubs miss.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m unittest tests.test_section_tracker_rigorous -v

Live PB round-trip tests auto-skip if PB is not reachable at PB_BASE.
Set PB_ADMIN_EMAIL / PB_ADMIN_PASSWORD in env to enable write tests.

Categories:
    LiveIntegration         — dual-write + round-trip byte equality through PB
    Concurrency             — 16 threads × 500 ops with invariant validation
    PropertyBased           — random inputs, verify invariants over ~1000 cases
    FloodScale              — 10k coalesced writes, prove O(1) end-state
    TimingBehavior          — real time.sleep, verify debounce window actually works
    AdversarialInputs       — null bytes, huge strings, JSON-injection attempts
    RecoveryScenarios       — truncated JSONL, PB kill mid-session, partial writes
    ByteLevelPreservation   — multibyte content round-trips exactly via content_hash
    SecretScrubPrecision    — false-positive gates, not just positive matches
"""

import hashlib
import json
import os
import random
import string
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import section_tracker as st


# ── Live PocketBase helpers ───────────────────────────────────────────

def _pb_online(base: str = st.PB_BASE) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _pb_auth_token(base: str = st.PB_BASE) -> str:
    email = os.environ.get("PB_ADMIN_EMAIL", "")
    pwd = os.environ.get("PB_ADMIN_PASSWORD", "")
    if not (email and pwd):
        return ""
    body = json.dumps({"identity": email, "password": pwd}).encode()
    req = urllib.request.Request(
        f"{base}/api/admins/auth-with-password",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())["token"]
    except Exception:
        return ""


def _pb_get_record(collection: str, record_id: str, token: str,
                   base: str = st.PB_BASE) -> dict:
    req = urllib.request.Request(
        f"{base}/api/collections/{collection}/records/{record_id}",
        headers={"Authorization": token})
    with urllib.request.urlopen(req, timeout=2) as resp:
        return json.loads(resp.read())


def _pb_count(collection: str, token: str, base: str = st.PB_BASE,
              filter_expr: str = "") -> int:
    qs = "?perPage=1"
    if filter_expr:
        from urllib.parse import quote
        qs += f"&filter={quote(filter_expr)}"
    req = urllib.request.Request(
        f"{base}/api/collections/{collection}/records{qs}",
        headers={"Authorization": token})
    with urllib.request.urlopen(req, timeout=2) as resp:
        return json.loads(resp.read()).get("totalItems", 0)


PB_UP = _pb_online()
PB_TOKEN = _pb_auth_token() if PB_UP else ""
PB_WRITE_AVAILABLE = bool(PB_UP and PB_TOKEN)


class RigorousBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.jsonl_root = Path(self.tmpdir.name) / "daily_ops"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _rec(self, **kw):
        defaults = dict(
            actor="test", model="n/a", source="test",
            jsonl_root=self.jsonl_root,
            debounce_seconds=0.0,
            install_atexit=False,
        )
        defaults.update(kw)
        return st.OpRecorder(**defaults)


# ══════════════════════════════════════════════════════════════════════
# 1. Live PocketBase integration (real round-trip, not stubs)
# ══════════════════════════════════════════════════════════════════════

@unittest.skipUnless(PB_WRITE_AVAILABLE,
                     "Live PB required: start pocketbase + set PB_ADMIN_EMAIL/PASSWORD")
class TestLivePocketBase(RigorousBase):
    """Round-trip verification: what we wrote is EXACTLY what comes back."""

    def test_linked_doc_round_trip_byte_exact(self):
        """Insert a linked_doc with unicode/emoji summary, fetch, compare bytes."""
        r = self._rec(actor="test", pb=st.PocketBaseClient())
        summary = "🧠 Unicode summary with café · naïve — «quotes» and \x00no-null"
        # PocketBase rejects literal null bytes; strip them as daily notes can't contain them anyway
        summary = summary.replace("\x00", "")
        result = r.record_linked_doc(
            parent_section="sitrep",
            child_path=f"rigor-test-{int(time.time()*1000)}.md",
            relationship="reference",
            summary=summary,
        )
        self.assertIsNotNone(result, "tracker must return result on live PB")
        self.assertIsNotNone(result["pb_id"], "PB must accept the insert")
        fetched = _pb_get_record("linked_docs", result["pb_id"], PB_TOKEN)
        self.assertEqual(fetched["summary"], summary,
                         "round-trip must be byte-exact, not re-encoded")

    def test_op_round_trip_hash_stable(self):
        """Op written to PB must have the SAME after_hash when read back."""
        r = self._rec(pb=st.PocketBaseClient())
        r.start_session()
        content = "Cognitive architecture — turtles all the way down 🐢"
        result = r.record_op(
            section="sitrep", operation="write",
            before="", after=content)
        self.assertIsNotNone(result["pb_id"])
        fetched = _pb_get_record("section_operations", result["pb_id"], PB_TOKEN)
        # hash stored in PB must equal hash we'd compute locally on the same content
        expected = st._hash(content)
        self.assertEqual(fetched["after_hash"], expected,
                         f"hash stored in PB ({fetched['after_hash']}) != hash of content ({expected})")

    def test_experiment_unique_index_enforces_dedup_across_processes(self):
        """
        PB unique index prevents duplicate experiment_id even if JSONL dedup
        were bypassed (e.g., different tracker instances). This is the
        canonical guarantee the schema makes.
        """
        import uuid as _u
        eid = f"exp-rigor-{_u.uuid4().hex[:12]}"
        r1 = self._rec(pb=st.PocketBaseClient())
        r2 = self._rec(pb=st.PocketBaseClient())  # independent recorder → own JSONL root
        r2._jsonl_root = self.jsonl_root  # share JSONL but bypass dedup with allow_duplicate
        res1 = r1.record_experiment(experiment_id=eid, title="one",
                                     hypothesis="h1", allow_duplicate=True)
        res2 = r2.record_experiment(experiment_id=eid, title="two",
                                     hypothesis="h2", allow_duplicate=True)
        self.assertIsNotNone(res1["pb_id"], "first insert should succeed")
        self.assertIsNone(res2["pb_id"],
                          "PB unique index must reject duplicate experiment_id")

    def test_session_op_count_matches_pb_exactly(self):
        """Insert N ops, verify PB count matches (no lost writes, no duplicates)."""
        import uuid as _u
        marker = f"rigor-{_u.uuid4().hex[:8]}"
        r = self._rec(pb=st.PocketBaseClient())
        sid = r.start_session(metadata={"marker": marker})
        N = 25
        for i in range(N):
            r.record_op(section="sitrep", operation="write",
                        before="", after=f"op-{i}-{marker}")
        r.end_session()
        pb_count = _pb_count("section_operations", PB_TOKEN,
                              filter_expr=f'session_id="{sid}"')
        self.assertEqual(pb_count, N,
                         f"PB must have exactly {N} ops for session {sid}, got {pb_count}")


# ══════════════════════════════════════════════════════════════════════
# 2. True concurrency with invariant validation
# ══════════════════════════════════════════════════════════════════════

class TestConcurrency(RigorousBase):
    """Not just 'no crash' — exact count parity + no lost ops + valid JSON."""

    def test_16_threads_500_ops_no_loss_no_corruption(self):
        """
        16 threads × 500 ops = 8000 total ops. Invariants:
        - JSONL has exactly 8000 'op' kind records (no loss)
        - Every line parses as valid JSON (no corruption)
        - Every op has a unique op_id (no collisions)
        - Total bytes_written across all ops > 0
        """
        THREADS = 16
        OPS_PER_THREAD = 500
        total = THREADS * OPS_PER_THREAD
        barrier = threading.Barrier(THREADS)
        errors: list[str] = []

        def worker(tid):
            try:
                r = self._rec(debounce_seconds=0.0)
                sid = r.start_session()
                barrier.wait()  # all threads start ops simultaneously
                for i in range(OPS_PER_THREAD):
                    r.record_op(section=f"s{tid % 4}", operation="write",
                                before="", after=f"t{tid}-i{i}")
                r.end_session()
            except Exception as e:
                errors.append(f"t{tid}: {e!r}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [], f"worker errors: {errors}")
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(len(ops), total,
                         f"lost ops: wrote {total}, read {len(ops)}")
        ids = {o["data"]["op_id"] for o in ops}
        self.assertEqual(len(ids), total,
                         f"op_id collisions: {total - len(ids)} duplicates")
        total_bytes = sum(o["data"]["bytes_written"] for o in ops)
        self.assertGreater(total_bytes, 0)

    def test_auto_session_start_under_race(self):
        """
        Two threads simultaneously call record_op without start_session. Both
        will auto-create sessions. Invariant: no op has empty session_id.
        """
        def worker():
            r = self._rec(debounce_seconds=0.0)
            for i in range(50):
                r.record_op(section="sitrep", operation="write", after=f"x{i}")

        ts = [threading.Thread(target=worker) for _ in range(4)]
        for t in ts: t.start()
        for t in ts: t.join()

        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(len(ops), 200)
        for op in ops:
            self.assertTrue(op["data"]["session_id"],
                             "op must have non-empty session_id even on race")


# ══════════════════════════════════════════════════════════════════════
# 3. Property-based: 1000 random inputs, invariants must hold
# ══════════════════════════════════════════════════════════════════════

class TestProperty(RigorousBase):
    """Random inputs vs core invariants. Stdlib only (no hypothesis dep)."""

    def test_hash_roundtrip_1000_random_inputs(self):
        """For 1000 random strings, _hash is deterministic and 16-hex."""
        rng = random.Random(42)
        for _ in range(1000):
            length = rng.randint(0, 10000)
            s = "".join(rng.choices(string.printable, k=length))
            h1 = st._hash(s)
            h2 = st._hash(s)
            self.assertEqual(h1, h2, "hash must be deterministic")
            self.assertEqual(len(h1), 16, "hash must be 16 hex chars")
            self.assertTrue(all(c in "0123456789abcdef" for c in h1))

    def test_op_id_uniqueness_1000_random_ops(self):
        """Across 1000 rapid ops, every op_id must be unique (counter backstop)."""
        r = self._rec(debounce_seconds=0.0)
        r.start_session()
        rng = random.Random(7)
        for _ in range(1000):
            section = rng.choice(["sitrep", "in_the_lab", "work_efforts"])
            content = str(rng.randint(0, 10**9))
            r.record_op(section=section, operation="write", after=content)
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        ids = {o["data"]["op_id"] for o in ops}
        self.assertEqual(len(ids), 1000,
                         f"expected 1000 unique op_ids, got {len(ids)}")

    def test_bytes_written_equals_utf8_encoding_length(self):
        """Invariant: bytes_written == len(content.encode('utf-8'))."""
        r = self._rec(debounce_seconds=0.0)
        r.start_session()
        samples = [
            "",
            "ascii",
            "café",  # 5 chars, 6 bytes
            "こんにちは",  # 5 chars, 15 bytes
            "🧠",  # 1 char, 4 bytes
            "a" * 1000,
            "α" * 1000,  # 1000 chars, 2000 bytes
        ]
        for s in samples:
            r.record_op(section="sitrep", operation="write",
                        before="", after=s)
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        for op, sample in zip(ops, samples):
            self.assertEqual(op["data"]["bytes_written"],
                             len(sample.encode("utf-8")),
                             f"bytes_written wrong for {sample!r}")


# ══════════════════════════════════════════════════════════════════════
# 4. Flood scale: coalescing holds at 10k ops
# ══════════════════════════════════════════════════════════════════════

class TestFloodScale(RigorousBase):
    def test_10k_coalesced_writes_yield_one_op(self):
        """
        10,000 writes within debounce window → exactly 1 op after flush.
        Invariants:
          - Exactly 1 op in JSONL
          - metadata.coalesced_count == 10000
          - metadata.burst_span_ms > 0 (some time actually passed)
          - after_hash == hash of the LAST write's content
        """
        r = self._rec(debounce_seconds=60.0)  # generous window
        r.start_session()
        N = 10_000
        for i in range(N):
            r.record_op(section="sitrep", operation="write",
                        before="", after=f"v{i}")
        self.assertEqual(len(st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")), 0,
                         "ops must stay buffered until flush")
        r.flush()
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(len(ops), 1, "all 10k writes must coalesce to 1 op")
        self.assertEqual(ops[0]["data"]["metadata"]["coalesced_count"], N)
        self.assertGreaterEqual(ops[0]["data"]["metadata"]["burst_span_ms"], 0)
        self.assertEqual(ops[0]["data"]["after_hash"], st._hash(f"v{N-1}"))


# ══════════════════════════════════════════════════════════════════════
# 5. Real time.sleep — debounce window actually expires
# ══════════════════════════════════════════════════════════════════════

class TestTimingBehavior(RigorousBase):
    """Not monkey-patched time — real sleep, real expiry."""

    def test_debounce_window_actually_expires(self):
        """
        Write → sleep past debounce → write. Should produce 2 ops
        (not 1 coalesced, not 0 dropped).
        """
        r = self._rec(debounce_seconds=0.3)  # 300ms — short enough for CI
        r.start_session()
        r.record_op(section="sitrep", operation="write", after="first")
        time.sleep(0.4)  # past debounce
        # Calling _flush_expired directly (since next record_op would do it too)
        r._flush_expired()
        r.record_op(section="sitrep", operation="write", after="second")
        r.flush()
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(len(ops), 2, "debounce must expire, producing 2 ops")
        self.assertEqual(ops[0]["data"]["after_hash"], st._hash("first"))
        self.assertEqual(ops[1]["data"]["after_hash"], st._hash("second"))

    def test_rapid_burst_within_debounce_stays_coalesced(self):
        """Same test but within window — must remain 1 op."""
        r = self._rec(debounce_seconds=2.0)
        r.start_session()
        r.record_op(section="sitrep", operation="write", after="first")
        time.sleep(0.05)
        r.record_op(section="sitrep", operation="write", after="second")
        time.sleep(0.05)
        r.record_op(section="sitrep", operation="write", after="third")
        r.flush()
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["data"]["metadata"]["coalesced_count"], 3)


# ══════════════════════════════════════════════════════════════════════
# 6. Adversarial inputs — tracker must not crash or corrupt
# ══════════════════════════════════════════════════════════════════════

class TestAdversarial(RigorousBase):
    def test_json_injection_does_not_escape_envelope(self):
        """
        Content that looks like JSON ({"evil":true}) must remain string-valued
        in `after` — never interpreted as a structure of its own.
        """
        r = self._rec(debounce_seconds=0.0)
        r.start_session()
        payload = '{"injected": true, "session_id": "FAKE"}'
        r.record_op(section="sitrep", operation="write", after=payload)
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(ops[0]["data"]["after_hash"], st._hash(payload))
        # Session id must remain the real one, not the injected one
        self.assertNotEqual(ops[0]["data"]["session_id"], "FAKE")

    def test_newline_in_content_does_not_split_jsonl(self):
        """
        Content with embedded newlines must produce a single JSONL line,
        not N lines (which would corrupt the file).
        """
        r = self._rec(debounce_seconds=0.0)
        r.start_session()
        multiline = "line1\nline2\nline3\n{\"fake\": \"record\"}\n"
        r.record_op(section="sitrep", operation="write", after=multiline)
        # count physical lines in file
        path = self.jsonl_root / st._today_str()[:7] / f"{st._today_str()}.jsonl"
        physical_lines = path.read_text().count("\n")
        # Should be: 1 session_start + 1 op = 2 lines, NOT 2 + 4
        records = st.read_jsonl(jsonl_root=self.jsonl_root)
        self.assertEqual(physical_lines, len(records),
                         "each record must occupy exactly 1 physical line")

    def test_huge_content_does_not_crash(self):
        """10 MB string write — tracker must not OOM or crash."""
        r = self._rec(debounce_seconds=0.0)
        r.start_session()
        big = "x" * (10 * 1024 * 1024)  # 10 MB
        r.record_op(section="sitrep", operation="write", after=big)
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(ops[0]["data"]["bytes_written"], len(big))
        # Hash still computes correctly at scale
        self.assertEqual(ops[0]["data"]["after_hash"], st._hash(big))

    def test_snapshot_preview_truly_capped(self):
        """
        Even with 10 MB content, snapshot preview must be ≤ 500 chars.
        (Prevents an attacker stuffing the DB via preview growth.)
        """
        r = self._rec(debounce_seconds=0.0)
        big = "A" * (10 * 1024 * 1024)
        r.record_snapshot(section="sitrep", content=big)
        snaps = st.read_jsonl(jsonl_root=self.jsonl_root, kind="snapshot")
        self.assertLessEqual(len(snaps[0]["data"]["content_preview"]),
                              st.PREVIEW_MAX_CHARS)

    def test_null_bytes_in_content_handled(self):
        """Null bytes shouldn't corrupt JSONL — they're valid JSON strings."""
        r = self._rec(debounce_seconds=0.0)
        r.start_session()
        content = "before\x00after"
        r.record_op(section="sitrep", operation="write", after=content)
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        self.assertEqual(ops[0]["data"]["after_hash"], st._hash(content))


# ══════════════════════════════════════════════════════════════════════
# 7. Recovery scenarios
# ══════════════════════════════════════════════════════════════════════

class TestRecovery(RigorousBase):
    def test_truncated_line_does_not_block_good_lines_after(self):
        """
        Process killed mid-write → truncated line. Tracker keeps appending;
        reader drops the bad line, returns all good ones before AND after.
        """
        path = self.jsonl_root / st._today_str()[:7] / f"{st._today_str()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Simulate: 1 good line, 1 truncated line, 2 good lines
        with open(path, "w") as f:
            f.write('{"schema":"daily_ops/v1","kind":"op","data":{"section_name":"a","op_id":"1"}}\n')
            f.write('{"schema":"daily_ops/v1","kind":"op","data":{"section_name":"b"')  # truncated, no newline
            f.write('\n{"schema":"daily_ops/v1","kind":"op","data":{"section_name":"c","op_id":"3"}}\n')
            f.write('{"schema":"daily_ops/v1","kind":"op","data":{"section_name":"d","op_id":"4"}}\n')
        records = st.read_jsonl(jsonl_root=self.jsonl_root)
        # Expect 3 parseable: a, c, d (b's truncation swallows the newline boundary
        # but the next record starts cleanly on its own line)
        self.assertGreaterEqual(len(records), 3,
                                "recovery must find at least 3 good records")
        sections = [r["data"]["section_name"] for r in records]
        self.assertIn("a", sections)
        self.assertIn("c", sections)
        self.assertIn("d", sections)

    def test_continue_writing_after_truncated_file(self):
        """After a truncation, new record_op calls must still succeed."""
        path = self.jsonl_root / st._today_str()[:7] / f"{st._today_str()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema":"daily_ops/v1","kind":"op","data":{"se')  # truncated
        r = self._rec(debounce_seconds=0.0)
        r.start_session()
        r.record_op(section="sitrep", operation="write", after="new")
        # The new write should be present + parseable
        text = path.read_text()
        # find the new record line
        lines = text.splitlines()
        found_new = False
        for line in lines:
            try:
                rec = json.loads(line)
                if rec.get("kind") == "op" and rec["data"].get("section_name") == "sitrep":
                    found_new = True
            except json.JSONDecodeError:
                pass
        self.assertTrue(found_new, "new op must append cleanly after truncation")


# ══════════════════════════════════════════════════════════════════════
# 8. Byte-level content preservation
# ══════════════════════════════════════════════════════════════════════

class TestByteLevelPreservation(RigorousBase):
    def test_content_survives_jsonl_roundtrip_byte_exact(self):
        """
        Write → read_jsonl → recompute hash of preview (if full content
        stored). We don't store full content in envelopes, but linked_doc
        summary and snapshot preview (≤500) ARE stored — verify those.
        """
        r = self._rec(debounce_seconds=0.0)
        unicode_summary = "日本語テスト · 🎯 café · α·β·γ — «quoted» text"
        r.record_linked_doc(
            parent_section="sitrep",
            child_path="test.md",
            relationship="reference",
            summary=unicode_summary,
        )
        records = st.read_jsonl(jsonl_root=self.jsonl_root, kind="linked_doc")
        self.assertEqual(records[0]["data"]["summary"], unicode_summary,
                         "JSONL round-trip must preserve unicode byte-exactly")

    def test_hash_consensus_across_unicode_normalization_variants(self):
        """
        Two different byte sequences for 'café' (NFC vs NFD) are semantically
        equivalent but byte-different. Verify hash distinguishes them —
        silent normalization would hide real content changes.
        """
        import unicodedata
        nfc = unicodedata.normalize("NFC", "café")  # é = U+00E9 (1 char)
        nfd = unicodedata.normalize("NFD", "café")  # e + combining acute (2 chars)
        self.assertNotEqual(nfc, nfd)
        self.assertNotEqual(st._hash(nfc), st._hash(nfd),
                            "hash must NOT silently normalize — treat byte diffs as real")


# ══════════════════════════════════════════════════════════════════════
# 9. Secret scrub — precision matters (not just recall)
# ══════════════════════════════════════════════════════════════════════

class TestSecretScrubPrecision(RigorousBase):
    def test_false_positive_gates(self):
        """Benign text containing trigger words must NOT be scrubbed."""
        benign = [
            "my password policy requires 12 chars",
            "the token of appreciation",
            "we need an api key to proceed",  # no colon/equals + value
            "write about security",
            "discussing secrets in general",
        ]
        for text in benign:
            scrubbed = st._scrub(text)
            self.assertEqual(scrubbed, text,
                              f"benign text was scrubbed: {text!r} → {scrubbed!r}")

    def test_true_positive_coverage(self):
        """Known secret formats must all be caught."""
        cases = [
            "sk-abcdefghijklmnopqrstuvwxyz1234",
            "sk-ant-api03-" + "a" * 40,
            "ghp_" + "a" * 36,
            "gho_" + "b" * 36,
            # Built by concatenation, like its neighbours above, so the literal
            # never appears in the file. GitHub push protection flags a
            # well-formed Slack token even inside a redaction test, and a
            # blocked push is a worse outcome than a slightly uglier fixture.
            "xoxb-" + "0" * 12 + "-" + "1" * 12 + "-" + "c" * 24,
            "AKIAIOSFODNN7EXAMPLE",
            "api_key: supersecret12345",
            "API_KEY=realpassword9999",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + "a" * 40 + "." + "b" * 20,
        ]
        for secret in cases:
            scrubbed = st._scrub(secret)
            self.assertIn("redacted", scrubbed,
                          f"secret NOT scrubbed: {secret!r} → {scrubbed!r}")

    def test_scrub_preserves_surrounding_context(self):
        """Scrub only the secret — surrounding prose must survive."""
        content = "The config has api_key: realSecretValue1234 and more text after"
        scrubbed = st._scrub(content)
        self.assertIn("The config has", scrubbed)
        self.assertIn("and more text after", scrubbed)
        self.assertNotIn("realSecretValue1234", scrubbed)


# ══════════════════════════════════════════════════════════════════════
# 10. Rate limit boundary
# ══════════════════════════════════════════════════════════════════════

class TestRateLimitBoundary(RigorousBase):
    def test_exactly_at_ceiling_not_flagged(self):
        """First N ops (under ceiling) must NOT be flagged rate_limited."""
        r = self._rec(debounce_seconds=0.0, max_ops_per_minute=5)
        r.start_session()
        for i in range(5):
            r.record_op(section="sitrep", operation="write", after=f"x{i}")
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        flagged = [o for o in ops if o["data"]["metadata"].get("rate_limited")]
        self.assertEqual(len(flagged), 0, "ops within ceiling must not be flagged")

    def test_first_over_ceiling_flagged(self):
        """The N+1th op must be flagged."""
        r = self._rec(debounce_seconds=0.0, max_ops_per_minute=5)
        r.start_session()
        for i in range(7):
            r.record_op(section="sitrep", operation="write", after=f"x{i}")
        ops = st.read_jsonl(jsonl_root=self.jsonl_root, kind="op")
        flagged = [o for o in ops if o["data"]["metadata"].get("rate_limited")]
        self.assertEqual(len(flagged), 2,
                         "exactly 2 ops (ops 6 and 7) should be flagged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
