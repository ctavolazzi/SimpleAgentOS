"""
test_vault_commit.py — Tests for vault_commit.py.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_vault_commit.py -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import vault_commit


class TestBuildSmartMessage(unittest.TestCase):

    def test_daily_notes_only(self):
        msg = vault_commit.build_smart_message(["Daily Notes/2026-04-27.md"])
        self.assertIn("daily note", msg.lower())

    def test_captured_only(self):
        msg = vault_commit.build_smart_message(["Captured/foo.md", "Captured/bar.md"])
        self.assertIn("capture", msg.lower())

    def test_audits_only(self):
        msg = vault_commit.build_smart_message(["Audits/2026-04-27-git-audit.md"])
        self.assertIn("audit", msg.lower())

    def test_plans_only(self):
        msg = vault_commit.build_smart_message(["Plans/2026-04-27_daily_plan.md"])
        self.assertIn("plan", msg.lower())

    def test_hubs_only(self):
        msg = vault_commit.build_smart_message(["Hubs/2026-04-27_hub.md"])
        self.assertIn("hub", msg.lower())

    def test_mixed_paths_gives_mid_day(self):
        msg = vault_commit.build_smart_message([
            "Daily Notes/2026-04-27.md",
            "Plans/plan.md",
            "Captured/foo.md",
        ])
        self.assertIn("mid-day", msg.lower())

    def test_message_is_string(self):
        msg = vault_commit.build_smart_message(["anything.md"])
        self.assertIsInstance(msg, str)

    def test_includes_file_count(self):
        paths = ["a.md", "b.md", "c.md"]
        msg = vault_commit.build_smart_message(paths)
        self.assertIn("3", msg)


class TestCommitVaultDryRun(unittest.TestCase):

    def test_dry_run_never_calls_git(self):
        with patch("subprocess.check_output") as mock_out, \
             patch("subprocess.check_call") as mock_call:
            # Simulate dirty vault
            mock_out.return_value = "M Daily Notes/2026-04-27.md\n"
            result = vault_commit.commit_vault(dry_run=True)
            mock_call.assert_not_called()

    def test_dry_run_status(self):
        with patch("subprocess.check_output") as mock_out:
            mock_out.return_value = "M Daily Notes/2026-04-27.md\n"
            result = vault_commit.commit_vault(dry_run=True)
            self.assertEqual(result["status"], "dry_run")

    def test_clean_vault_returns_clean(self):
        with patch("subprocess.check_output") as mock_out:
            mock_out.return_value = ""
            result = vault_commit.commit_vault(dry_run=True)
            self.assertEqual(result["status"], "clean")

    def test_no_git_repo_returns_failed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # Temporarily override VAULT_DIR
            original = vault_commit.VAULT_DIR
            vault_commit.VAULT_DIR = Path(tmp)
            try:
                result = vault_commit.commit_vault()
                self.assertEqual(result["status"], "failed")
            finally:
                vault_commit.VAULT_DIR = original


class TestFormatResultMd(unittest.TestCase):

    def test_clean(self):
        result = {"status": "clean"}
        md = vault_commit.format_result_md(result)
        self.assertIn("clean", md.lower())

    def test_dry_run(self):
        result = {"status": "dry_run", "files_changed": 5, "message": "chore: test"}
        md = vault_commit.format_result_md(result)
        self.assertIn("dry-run", md.lower())
        self.assertIn("5", md)

    def test_committed(self):
        result = {
            "status": "committed", "sha": "abc1234",
            "files_changed": 3, "pushed": True, "message": "chore: test"
        }
        md = vault_commit.format_result_md(result)
        self.assertIn("abc1234", md)
        self.assertIn("pushed", md.lower())

    def test_failed(self):
        result = {"status": "failed", "stderr": "no remote configured"}
        md = vault_commit.format_result_md(result)
        self.assertIn("FAILED", md)


if __name__ == "__main__":
    unittest.main()
