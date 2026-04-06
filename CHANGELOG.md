# Changelog

## [0.2.0] — 2026-04-05

### Added
- **Self-Explorer**: OODA-loop agent that reads its own source code and journals about its architecture
- **Real-time dashboard** (`self_explore.html`): 3-column terminal UI with SSE streaming
- **SSE endpoint** (`/api/explorer/stream`): Server-pushed journal updates every 500ms
- **Warmup query**: Primes Gemma4 on startup so first real tokens arrive faster
- **Prefill timer**: Shows elapsed seconds while waiting for CPU inference
- **Copy session button**: One-click clipboard export of full journal
- **Connection indicator**: Green/red dot shows server connectivity
- **Stop actually works**: Cancels in-flight HTTP requests via `task.cancel()`
- **Direct query panel**: Ask Gemma4 anything, streaming response
- `requirements.txt` for Python dependencies
- `tests/test_smoke.py` — pre-flight checks (deps, files, ports, binaries)
- `tests/test_explorer.py` — unit tests for SelfExplorer (no LLM needed)
- `.gitignore` for `__pycache__`, `node_modules`, `pocketbase`, `.self_explorer/`

### Architecture
```
Browser (:1010) --SSE--> self_explore_server.py --HTTP--> llama-server (:8080)
```
- FastAPI serves HTML + API on port 1010
- llama-server runs Gemma-4 E4B (Q4_K_M, 5GB) on CPU
- `GGML_METAL=0` disables Metal GPU (required for Intel Iris Pro)
- OODA loop: Observe (read file) → Orient (analyze via LLM) → Decide (next file) → Act (journal)

## [0.1.0] — 2026-04-04

### Added
- Original SimpleAgentOS: React HUD + PocketBase + llama.cpp
- `rebuild_AgentOS.py`: System architect/builder
- `build_os.py`: Alternative builder
- `seed_engine.py`: Test data injection
- `core_engine/`: React frontend + PocketBase backend + Makefile
- `core_engine/nerve_center.py`: FastAPI relay to llama-server
