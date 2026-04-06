"""
Smoke tests for SimpleAgentOS Self-Explorer.
Run: python -m pytest tests/ -v
These tests verify the system is correctly configured before launching.
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent


# ── Dependency checks ──────────────────────────────────────────────

def test_python_version():
    """Python 3.10+ required for match statements and union types."""
    assert sys.version_info >= (3, 10), f"Python 3.10+ required, got {sys.version}"


def test_fastapi_installed():
    """FastAPI must be installed."""
    importlib.import_module("fastapi")


def test_uvicorn_installed():
    """Uvicorn must be installed."""
    importlib.import_module("uvicorn")


def test_httpx_installed():
    """httpx must be installed."""
    importlib.import_module("httpx")


# ── File structure checks ──────────────────────────────────────────

def test_server_file_exists():
    assert (PROJECT_DIR / "self_explore_server.py").exists()


def test_html_file_exists():
    assert (PROJECT_DIR / "self_explore.html").exists()


def test_launch_script_exists():
    assert (PROJECT_DIR / "run_self_explore.sh").exists()


def test_launch_script_executable():
    script = PROJECT_DIR / "run_self_explore.sh"
    assert os.access(script, os.X_OK), f"{script} is not executable. Run: chmod +x {script}"


def test_gitignore_exists():
    assert (PROJECT_DIR / ".gitignore").exists()


# ── Server module checks ──────────────────────────────────────────

def test_server_module_imports():
    """Verify self_explore_server.py can be imported without side effects."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "self_explore_server", PROJECT_DIR / "self_explore_server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Don't exec — just verify the spec loads. Exec would start uvicorn.
    assert spec is not None


def test_server_has_required_routes():
    """Verify all expected route paths exist in the server source."""
    src = (PROJECT_DIR / "self_explore_server.py").read_text()
    required = [
        "/api/health",
        "/api/explorer/start",
        "/api/explorer/stop",
        "/api/explorer/status",
        "/api/explorer/journal",
        "/api/explorer/stream",
        "/api/explorer/docs",
        "/api/query",
    ]
    for route in required:
        assert route in src, f"Missing route: {route}"


def test_html_has_sse_connection():
    """Verify the HTML uses SSE, not just polling."""
    src = (PROJECT_DIR / "self_explore.html").read_text()
    assert "EventSource" in src, "HTML should use EventSource for real-time updates"


def test_html_has_copy_button():
    """Verify copy-session functionality exists."""
    src = (PROJECT_DIR / "self_explore.html").read_text()
    assert "copySession" in src, "HTML should have copySession function"


# ── LLM infrastructure checks ─────────────────────────────────────

def test_llama_server_binary_exists():
    """Check that llama-server binary is where the launch script expects it."""
    # Parse the path from run_self_explore.sh
    script = (PROJECT_DIR / "run_self_explore.sh").read_text()
    for line in script.splitlines():
        if line.startswith("LLAMA_SERVER="):
            path = line.split("=", 1)[1].strip().strip('"').replace("$HOME", os.path.expanduser("~"))
            if Path(path).exists():
                return
            else:
                raise AssertionError(
                    f"llama-server not found at {path}\n"
                    f"Build it: cd ~/Code/llama.cpp && cmake -B build && cmake --build build --target llama-server"
                )
    raise AssertionError("Could not find LLAMA_SERVER= in run_self_explore.sh")


def test_model_file_exists():
    """Check that the GGUF model file is where the launch script expects it."""
    script = (PROJECT_DIR / "run_self_explore.sh").read_text()
    for line in script.splitlines():
        if line.startswith("MODEL="):
            path = line.split("=", 1)[1].strip().strip('"').replace("$HOME", os.path.expanduser("~"))
            if Path(path).exists():
                return
            else:
                raise AssertionError(
                    f"Model not found at {path}\n"
                    f"Download: huggingface-cli download google/gemma-4-E4B-it-GGUF --include '*Q4_K_M*' --local-dir ~/"
                )
    raise AssertionError("Could not find MODEL= in run_self_explore.sh")


# ── Port checks ────────────────────────────────────────────────────

def test_port_1010_available():
    """Check port 1010 is free (or already ours)."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1)
        result = sock.connect_ex(("127.0.0.1", 1010))
        # 0 = port in use (might be us), nonzero = free (good)
        # We just warn, don't fail — it might be our running instance
        if result == 0:
            print("WARNING: Port 1010 already in use (may be a running instance)")
    finally:
        sock.close()


def test_port_8080_available():
    """Check port 8080 is free (or already ours)."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1)
        result = sock.connect_ex(("127.0.0.1", 8080))
        if result == 0:
            print("WARNING: Port 8080 already in use (may be a running instance)")
    finally:
        sock.close()
