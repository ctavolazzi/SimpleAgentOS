"""
Unit tests for SelfExplorer class.
Run: python -m pytest tests/test_explorer.py -v
Tests the explorer logic WITHOUT needing llama-server running.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Add parent to path so we can import the server module
sys.path.insert(0, str(Path(__file__).parent.parent))


def _load_explorer():
    """Import SelfExplorer without starting uvicorn."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "self_explore_server",
        Path(__file__).parent.parent / "self_explore_server.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Patch uvicorn.run so importing doesn't start a server
    with patch("uvicorn.run"):
        spec.loader.exec_module(mod)
    return mod.SelfExplorer


SelfExplorer = _load_explorer()


def test_explorer_init():
    e = SelfExplorer()
    assert e.running is False
    assert e.step_count == 0
    assert e.journal == []
    assert e.explored == []


def test_seed_queue():
    e = SelfExplorer()
    e._seed_queue()
    assert len(e.queue) > 0
    assert "self_explore_server.py" in e.queue


def test_journal_add():
    e = SelfExplorer()
    entry = e._journal_add("System", "test message", extra_key="val")
    assert entry["type"] == "System"
    assert entry["content"] == "test message"
    assert entry["extra_key"] == "val"
    assert "timestamp" in entry
    assert len(e.journal) == 1


def test_read_file_exists():
    e = SelfExplorer()
    content = e._read_file("self_explore_server.py")
    assert "FastAPI" in content
    assert len(content) > 0


def test_read_file_not_found():
    e = SelfExplorer()
    content = e._read_file("nonexistent_file_xyz.py")
    assert "FILE NOT FOUND" in content


def test_read_file_truncation():
    e = SelfExplorer()
    content = e._read_file("self_explore_server.py")
    # File is larger than MAX_FILE_CHARS (1500), should be truncated
    assert len(content) <= 1600  # 1500 + truncation message


def test_status():
    e = SelfExplorer()
    s = e.status()
    assert "running" in s
    assert "step_count" in s
    assert "files_explored" in s
    assert "queue_size" in s
    assert "journal_entries" in s


def test_stop():
    e = SelfExplorer()
    e.running = True
    e.stop()
    assert e.running is False


def test_stop_cancels_task():
    from unittest.mock import MagicMock
    e = SelfExplorer()
    e.running = True
    mock_task = MagicMock()
    mock_task.done.return_value = False
    e._task = mock_task
    e.stop()
    assert e.running is False
    mock_task.cancel.assert_called_once()
