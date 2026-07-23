"""
test_git_audit.py — Tests for git_audit.py.

Requires git binary. Tests use real git init in tmp directories.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_git_audit.py -v
"""

import shutil
import subprocess
import sys
import time
import os
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import git_audit
import harness_lib

GIT_AVAILABLE = shutil.which("git") is not None


@unittest.skipUnless(GIT_AVAILABLE, "git binary not available")
class TestClassifyDirtyPath(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                       cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=self.tmp, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_untracked_is_wip(self):
        f = self.tmp / "new_file.txt"
        f.write_text("hello")
        result = git_audit.classify_dirty_path(self.tmp, "?? new_file.txt", 24)
        self.assertEqual(result["class"], "wip")
        self.assertEqual(result["path"], "new_file.txt")

    def test_old_untracked_is_stale(self):
        f = self.tmp / "old_file.txt"
        f.write_text("hello")
        old_time = time.time() - (48 * 3600)
        os.utime(f, (old_time, old_time))
        result = git_audit.classify_dirty_path(self.tmp, "?? old_file.txt", 24)
        self.assertEqual(result["class"], "stale")

    def test_result_has_required_keys(self):
        f = self.tmp / "test.txt"
        f.write_text("hi")
        result = git_audit.classify_dirty_path(self.tmp, "?? test.txt", 24)
        for key in ("path", "xy", "age_hours", "class", "suggested_action"):
            self.assertIn(key, result)


@unittest.skipUnless(GIT_AVAILABLE, "git binary not available")
class TestAuditWorkspace(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        # Create a single dirty repo
        self.repo = self.tmp / "my_repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"],
                       cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.repo, check=True)
        (self.repo / "dirty.txt").write_text("untracked")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detects_dirty_repo(self):
        result = git_audit.audit_workspace(workspace=self.tmp)
        self.assertGreaterEqual(result["totals"]["repos_dirty"], 1)

    def test_result_structure(self):
        result = git_audit.audit_workspace(workspace=self.tmp)
        self.assertIn("repos", result)
        self.assertIn("totals", result)
        self.assertIn("generated_at", result)
        self.assertIn("wip", result["totals"])
        self.assertIn("stale", result["totals"])


class TestFormatAuditCompact(unittest.TestCase):

    def test_output_is_string(self):
        audit = {
            "repos": [],
            "totals": {"wip": 0, "stale": 0, "repos_dirty": 0},
            "generated_at": "2026-04-27T12:00:00",
        }
        result = git_audit.format_audit_compact(audit)
        self.assertIsInstance(result, str)

    def test_contains_totals(self):
        audit = {
            "repos": [],
            "totals": {"wip": 5, "stale": 3, "repos_dirty": 2},
            "generated_at": "2026-04-27T12:00:00",
        }
        result = git_audit.format_audit_compact(audit)
        self.assertIn("5", result)
        self.assertIn("3", result)


if __name__ == "__main__":
    unittest.main()
