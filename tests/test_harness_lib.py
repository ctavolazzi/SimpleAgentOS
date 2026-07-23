"""
test_harness_lib.py — Tests for harness_lib.py shared helpers.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_harness_lib.py -v
"""

import os
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import harness_lib


class TestDiscoverRepos(unittest.TestCase):

    def test_returns_list(self):
        result = harness_lib.discover_repos()
        self.assertIsInstance(result, list)

    def test_all_results_are_paths(self):
        result = harness_lib.discover_repos()
        for r in result:
            self.assertIsInstance(r, Path)

    def test_all_results_have_git_dir(self):
        result = harness_lib.discover_repos()
        for r in result:
            self.assertTrue((r / ".git").exists(), f"{r} has no .git dir")

    def test_fake_workspace(self):
        """Only dirs with .git are returned; non-repo dirs ignored."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            # Two repos
            for name in ("alpha", "beta"):
                (ws / name / ".git").mkdir(parents=True)
            # One non-repo
            (ws / "not_a_repo").mkdir()

            result = harness_lib.discover_repos(ws)
            names = {r.name for r in result}
            self.assertIn("alpha", names)
            self.assertIn("beta", names)
            self.assertNotIn("not_a_repo", names)

    def test_active_subdir_scanned(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "active" / "sub_repo" / ".git").mkdir(parents=True)
            result = harness_lib.discover_repos(ws)
            names = {r.name for r in result}
            self.assertIn("sub_repo", names)

    def test_active_itself_not_included(self):
        """The active/ container dir is skipped, only its children included."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "active" / ".git").mkdir(parents=True)
            result = harness_lib.discover_repos(ws)
            names = {r.name for r in result}
            # active/ itself should NOT appear — it's the scan container
            self.assertNotIn("active", names)


class TestClassifyMtime(unittest.TestCase):

    def test_fresh_file_is_wip(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            result = harness_lib.classify_mtime(Path(f.name), threshold_hours=24)
            self.assertEqual(result, "wip")

    def test_old_file_is_stale(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            # Backdate mtime by 48 hours
            old_time = time.time() - (48 * 3600)
            os.utime(f.name, (old_time, old_time))
            result = harness_lib.classify_mtime(Path(f.name), threshold_hours=24)
            self.assertEqual(result, "stale")

    def test_missing_file_is_stale(self):
        result = harness_lib.classify_mtime(Path("/nonexistent/file.txt"))
        self.assertEqual(result, "stale")

    def test_custom_threshold(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            # Backdate by 2 hours
            old_time = time.time() - (2 * 3600)
            os.utime(f.name, (old_time, old_time))
            # Threshold 1h → stale
            self.assertEqual(harness_lib.classify_mtime(Path(f.name), 1), "stale")
            # Threshold 3h → wip
            self.assertEqual(harness_lib.classify_mtime(Path(f.name), 3), "wip")


class TestIsoNow(unittest.TestCase):

    def test_returns_string(self):
        result = harness_lib.iso_now()
        self.assertIsInstance(result, str)

    def test_format(self):
        from datetime import datetime
        result = harness_lib.iso_now()
        # Must be parseable as ISO datetime
        datetime.fromisoformat(result)

    def test_second_precision(self):
        result = harness_lib.iso_now()
        # Should not have fractional seconds
        self.assertNotIn(".", result)


if __name__ == "__main__":
    unittest.main()
