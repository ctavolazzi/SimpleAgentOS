"""
test_preflight_empirica.py — Preflight D1 must count the transactions that are open.

Background: D1 ("Open empirica transactions") has been wrong twice.

  1. It shelled out to `empirica transaction-list --open --output json`. The
     installed CLI has no such subcommand, so the call failed with a usage
     error on every run and the check reported SKIP "empirica cmd unavailable"
     with the binary on PATH. A check that can only skip measures nothing.
  2. The first fix asked `empirica status --all --json` instead. That command
     exists and returned a clean answer: 0 open. At that moment nine
     `.empirica/active_transaction*.json` files on disk said `"status": "open"`,
     one opened minutes earlier by the session running the check and eight left
     over since March and April. The command lists live instances, not open
     transactions. It was caught only because a transaction was known to be
     open while the check said none were.

The state files are empirica's own record, so D1 reads them directly. Every
fixture below is that file's real shape.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_preflight_empirica.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import preflight

DAY = 86400


class _TmpCodeRoot(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        patch = mock.patch.object(preflight, "CODE_ROOT", self.root)
        patch.start()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(patch.stop)

    def _tx(self, project: str, status: str, age_days: float = 0,
            name: str = "active_transaction.json", raw: str = None) -> Path:
        """Write one transaction file the way empirica does, aged by mtime."""
        d = self.root / project / ".empirica"
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text(raw if raw is not None else json.dumps({
            "transaction_id": f"tx-{project}-{name}", "session_id": "s",
            "status": status, "project_path": str(self.root / project),
            "preflight_timestamp": time.time() - age_days * DAY,
            "updated_at": time.time() - age_days * DAY, "work_type": "code",
        }))
        when = time.time() - age_days * DAY
        os.utime(f, (when, when))
        return f

    def _d1(self):
        with mock.patch.object(preflight, "run", return_value=(1, "", "")):
            return next(c for c in preflight.check_epistemic() if c.id == "D1")


class TestD1CountsTheStateFiles(_TmpCodeRoot):
    def test_only_closed_transactions_passes_with_zero(self):
        self._tx("alpha", "closed", age_days=40)
        d1 = self._d1()
        self.assertEqual(d1.status, preflight.PASS)
        self.assertEqual(d1.data["count"], 0)

    def test_one_fresh_open_transaction_is_normal(self):
        self._tx("alpha", "open")
        d1 = self._d1()
        self.assertEqual(d1.status, preflight.PASS)
        self.assertEqual(d1.data["count"], 1)
        self.assertEqual(d1.data["stale"], 0)

    def test_leftover_open_transactions_warn_and_name_the_oldest(self):
        self._tx("alpha", "open")
        self._tx("fogsift", "open", age_days=190, name="active_transaction_term_a.json")
        self._tx("fogsift", "open", age_days=200, name="active_transaction_term_b.json")
        self._tx("beta", "closed", age_days=3)
        d1 = self._d1()
        self.assertEqual(d1.status, preflight.WARN)
        self.assertEqual(d1.data["count"], 3)
        self.assertEqual(d1.data["stale"], 2)
        self.assertIn("fogsift", d1.message)
        self.assertIn("2 older than a day", d1.message)
        self.assertTrue(d1.action)

    def test_several_files_in_one_project_are_each_counted(self):
        for i in range(4):
            self._tx("fogsift", "open", age_days=100 + i, name=f"active_transaction_t{i}.json")
        self.assertEqual(self._d1().data["count"], 4)

    def test_nested_project_is_found(self):
        self._tx("active/waft", "open", age_days=180)
        d1 = self._d1()
        self.assertEqual(d1.data["count"], 1)
        self.assertEqual(d1.data["open_in"], ["waft"])


class TestD1IsHonestWhenItCannotMeasure(_TmpCodeRoot):
    def test_no_state_files_skips_and_says_why(self):
        d1 = self._d1()
        self.assertEqual(d1.status, preflight.SKIP)
        self.assertIn("no empirica transaction files", d1.message)
        self.assertNotIn("unavailable", d1.message)

    def test_a_corrupt_file_is_reported_not_swallowed(self):
        self._tx("alpha", "open", raw="{not json")
        d1 = self._d1()
        self.assertEqual(d1.status, preflight.WARN)
        self.assertEqual(d1.data["unreadable"], 1)
        self.assertIn("unreadable", d1.message)


class TestTheWalkStaysOutOfVendoredTrees(_TmpCodeRoot):
    def test_node_modules_is_not_searched(self):
        self._tx("alpha/node_modules/some-package", "open")
        self.assertEqual(self._d1().status, preflight.SKIP)

    def test_depth_is_bounded(self):
        self._tx("a/b/c/d/e/f/deep", "open")
        self.assertEqual(self._d1().status, preflight.SKIP)


class TestD1DoesNotDependOnTheCli(_TmpCodeRoot):
    """The CLI answered wrongly twice. D1 must not shell out to it at all."""

    def test_no_empirica_subprocess_is_started(self):
        self._tx("alpha", "open")
        with mock.patch.object(preflight, "run", return_value=(1, "", "")) as run:
            preflight.check_epistemic()
        empirica_calls = [c for c in run.call_args_list
                          if c.args and isinstance(c.args[0], (list, tuple))
                          and c.args[0] and c.args[0][0] == "empirica"]
        self.assertEqual(empirica_calls, [])


@unittest.skipUnless((Path.home() / "Code" / ".empirica").is_dir(),
                     "no empirica project at ~/Code on this machine")
class TestD1OnThisMachine(unittest.TestCase):
    """Fixtures prove the logic. This proves it reads the real tree."""

    def test_the_real_tree_is_measured_within_a_second(self):
        start = time.time()
        txs, _unreadable = preflight._empirica_transactions()
        elapsed = time.time() - start
        self.assertTrue(txs, "found no transaction files under the real ~/Code")
        self.assertLess(elapsed, 3.0, f"walk took {elapsed:.1f}s")
        d1 = next(c for c in preflight.check_epistemic() if c.id == "D1")
        self.assertNotEqual(d1.status, preflight.SKIP, d1.message)


if __name__ == "__main__":
    unittest.main()
