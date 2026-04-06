# SimpleAgentOS — Self-Explorer

A self-reflective AI system that reads its own source code and journals about what it finds.
Gemma4 (local LLM via llama.cpp) explores, analyzes, and reflects on its own architecture
in real-time, visible through a terminal-style browser dashboard.

**Version:** 0.2.0 | **License:** MIT | **Status:** Working prototype

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/ctavolazzi/SimpleAgentOS.git
cd SimpleAgentOS

# 2. Install Python deps
pip install -r requirements.txt

# 3. Edit paths (set your llama-server binary + model location)
nano run_self_explore.sh

# 4. Run pre-flight checks
python -m pytest tests/test_smoke.py -v

# 5. Launch
./run_self_explore.sh

# 6. Open browser
open http://localhost:1010
```

Click **START EXPLORATION** and watch Gemma4 read its own code.

---

## How It Works

```
Browser (:1010)
  |  SSE (real-time, 500ms push)
  v
self_explore_server.py (FastAPI on :1010)
  |--- GET  /                       --> serves dashboard HTML
  |--- GET  /api/health             --> status check
  |--- POST /api/explorer/start     --> launch OODA loop
  |--- POST /api/explorer/stop      --> cancel (kills in-flight request)
  |--- GET  /api/explorer/status    --> current state
  |--- GET  /api/explorer/journal   --> full journal (polling fallback)
  |--- GET  /api/explorer/stream    --> SSE real-time feed
  |--- GET  /api/explorer/docs      --> generated documentation
  |--- POST /api/query              --> relay any prompt to LLM
  v
llama-server (:8080)
  Gemma-4 E4B (Q4_K_M, 5GB, CPU mode)
  OpenAI-compatible /v1/chat/completions
```

### The OODA Loop

Each step, the SelfExplorer:

1. **Observe** — reads a source file from its own codebase (max 1500 chars)
2. **Orient** — sends the file to Gemma4: "What does this file do? What does it reveal about your architecture?"
3. **Decide** — picks the next file from the queue, or auto-discovers `.py` files
4. **Act** — records analysis + 1-sentence reflection to the journal

Every token streams to the browser via Server-Sent Events. You see Gemma4 think in real-time.

---

## Prerequisites

| Requirement | Why | How to get it |
|---|---|---|
| Python 3.10+ | Union type syntax, match statements | `brew install python@3.12` |
| llama.cpp (built) | Runs Gemma4 locally | `git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp && cmake -B build && cmake --build build --target llama-server` |
| Gemma-4 E4B GGUF | The brain | `huggingface-cli download google/gemma-4-E4B-it-GGUF --include '*Q4_K_M*' --local-dir ~/` |
| npx / Node.js | `concurrently` for launching both servers | `brew install node` |

---

## Configuration

Edit `run_self_explore.sh`:

```bash
LLAMA_SERVER="$HOME/Code/llama.cpp/build/bin/llama-server"  # your path
MODEL="$HOME/google_gemma-4-E4B-it-Q4_K_M.gguf"            # your model
```

### CPU-only mode (Intel Macs / no GPU)

The launch script already sets `GGML_METAL=0` and `-ngl 0`. If you have a GPU that works with Metal, you can remove these flags for faster inference.

### Tuning for your hardware

| Flag | Default | What it does |
|---|---|---|
| `-c 2048` | Context window size | Lower = less RAM, faster prefill |
| `-np 1` | Parallel sequences | Keep at 1 for CPU |
| `-b 256` | Batch size | Lower = less RAM per batch |
| `--no-warmup` | Skip warmup | Faster startup (app does its own warmup) |

---

## Testing

```bash
# Pre-flight: verify deps, files, binaries, ports
python -m pytest tests/test_smoke.py -v

# Unit tests: test SelfExplorer logic (no LLM needed)
python -m pytest tests/test_explorer.py -v

# All tests
python -m pytest tests/ -v
```

---

## Troubleshooting

### "Port 8080 already in use"

```bash
lsof -ti:8080 | xargs kill -9
```

### "Port 1010 already in use"

```bash
lsof -ti:1010 | xargs kill -9
```

### GPU Timeout / Metal errors on Intel Mac

The launch script already handles this with `GGML_METAL=0`. If you still see Metal errors:

```bash
GGML_METAL=0 llama-server -m model.gguf --port 8080 -ngl 0
```

### "Prefilling... Xs elapsed" takes forever

CPU inference on a 5GB model is slow. Expected times:
- **Warmup** (17 tokens): ~2s
- **File analysis** (~400 token prompt): ~20-40s prefill, then ~5 tok/s generation
- **Reflection** (~50 token prompt): ~5s prefill

If it's taking >2 minutes, check `htop` — all CPU cores should be busy.

### No tokens appearing in browser

1. Check the terminal — you should see colored tokens (yellow = thinking, green = content)
2. If terminal shows tokens but browser doesn't, hard-refresh the page (Cmd+Shift+R)
3. Check browser console for errors (F12)
4. Verify SSE: `curl -N http://localhost:1010/api/explorer/stream`

### "Connection refused" in browser console

llama-server hasn't started yet. Wait for `main: server is listening on http://127.0.0.1:8080` in the terminal.

### STOP button doesn't respond

If the explorer is mid-request, STOP cancels the in-flight HTTP call. It may take 1-2 seconds. Check the terminal for `[STOP] Task cancelled.`

---

## FAQ

**Q: Can I use a different model?**
A: Yes. Any GGUF model served by llama-server works. Edit `MODEL` in `run_self_explore.sh`. Smaller models (1-3B) will be faster on CPU.

**Q: Can I use Ollama instead of llama-server?**
A: Yes. Point `LLAMA_URL` in `self_explore_server.py` to `http://localhost:11434/v1/chat/completions` and run `ollama serve` + `ollama run gemma3`.

**Q: Why SSE instead of WebSocket?**
A: The journal is a one-way data stream (server → browser). SSE is simpler, auto-reconnects, and needs zero client libraries. WebSocket is overkill for read-only feeds.

**Q: Why does it truncate files at 1500 chars?**
A: CPU prefill time scales linearly with prompt length. 1500 chars (~400 tokens) keeps prefill under 30 seconds. Change `MAX_FILE_CHARS` in `self_explore_server.py` if you have faster hardware.

**Q: Can I add my own files to the exploration queue?**
A: Edit `_seed_queue()` in `self_explore_server.py`. The explorer also auto-discovers `.py` files when the queue empties.

**Q: Where are the generated docs?**
A: `.self_explorer/docs/` (gitignored). The explorer writes markdown files there as it discovers patterns.

**Q: How do I copy the session log?**
A: Click the **COPY** button in the top bar. It exports the full journal as plain text to your clipboard.

---

## Files

| File | Purpose |
|---|---|
| `self_explore_server.py` | FastAPI server + SelfExplorer agent (~350 lines) |
| `self_explore.html` | Browser dashboard with SSE streaming (~340 lines) |
| `run_self_explore.sh` | Launch script (llama-server + Python server via concurrently) |
| `requirements.txt` | Python dependencies (fastapi, uvicorn, httpx) |
| `tests/test_smoke.py` | Pre-flight checks: deps, files, binaries, ports |
| `tests/test_explorer.py` | Unit tests for SelfExplorer logic |
| `CHANGELOG.md` | Version history |
| `core_engine/` | Original SimpleAgentOS (React + PocketBase + llama.cpp) |
| `build_os.py` | Original system builder |
| `rebuild_AgentOS.py` | Original system rebuilder |
| `seed_engine.py` | Original data seeder |

---

## Version History

See [CHANGELOG.md](CHANGELOG.md) for full details.

- **v0.2.0** (2026-04-05) — Self-Explorer: OODA loop, SSE streaming, real-time dashboard
- **v0.1.0** (2026-04-04) — Original SimpleAgentOS: React HUD + PocketBase + llama.cpp
