"""
Self-Explore Server — port 1010
Serves self_explore.html, relays to llama-server, runs OODA self-explorer.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

LLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"
PROJECT_DIR = Path(__file__).parent
DOCS_DIR = PROJECT_DIR / ".self_explorer" / "docs"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
MAX_FILE_CHARS = 1500  # Keep prompts small for fast prefill on CPU

app = FastAPI(title="SimpleAgentOS Self-Explorer")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    """Print with flush so concurrently shows it immediately."""
    print(msg, flush=True)


# ==================== Explorer ====================

class SelfExplorer:
    def __init__(self):
        self.journal: list[dict] = []
        self.running = False
        self.step_count = 0
        self.explored: list[str] = []
        self.current_file: str | None = None
        self.queue: list[str] = []
        self.docs_written = 0
        self._task: asyncio.Task | None = None

    def _seed_queue(self):
        self.queue = [
            "self_explore_server.py",
            "self_explore.html",
            "core_engine/frontend/src/App.jsx",
            "core_engine/nerve_center.py",
            "rebuild_AgentOS.py",
            "README.md",
        ]

    def _journal_add(self, entry_type, content, **extra):
        entry = {"type": entry_type, "timestamp": now_iso(), "step": self.step_count, "content": content, **extra}
        self.journal.append(entry)
        return entry

    def _read_file(self, rel_path):
        full = PROJECT_DIR / rel_path
        if not full.exists():
            return f"[FILE NOT FOUND: {rel_path}]"
        text = full.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n... [TRUNCATED]"
        return text

    async def _llm_call(self, messages, max_tokens=256):
        """Call Gemma4 with streaming. Updates a live journal entry with each token."""
        log(f"  [LLM] >>> Sending request ({max_tokens} max_tokens, {len(str(messages))} chars prompt)")
        t0 = datetime.now(timezone.utc)
        live_entry = self._journal_add("Thinking", "Prefilling... (CPU mode, ~1-2 min for first token)")

        # Background task to update elapsed time while waiting for tokens
        token_started = False

        async def tick_elapsed():
            while not token_started and self.running:
                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                if not token_started:
                    live_entry["content"] = f"Prefilling... {elapsed:.0f}s elapsed (CPU mode)"
                await asyncio.sleep(1)

        ticker = asyncio.create_task(tick_elapsed())

        async with httpx.AsyncClient(timeout=600.0) as client:
            try:
                reasoning = ""
                content = ""
                tokens = 0

                async with client.stream("POST", LLAMA_URL, json={
                    "model": "gemma-4",
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "stream": True,
                }) as resp:
                    log(f"  [LLM] Connected, status={resp.status_code}. Waiting for tokens...")
                    async for raw_line in resp.aiter_lines():
                        if not raw_line.startswith("data: ") or raw_line == "data: [DONE]":
                            continue
                        try:
                            chunk = json.loads(raw_line[6:])
                        except json.JSONDecodeError:
                            continue

                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        r = delta.get("reasoning_content", "")
                        c = delta.get("content", "")

                        if r:
                            if not token_started:
                                token_started = True
                                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                                log(f"  [LLM] First reasoning token after {elapsed:.1f}s")
                            reasoning += r
                            tokens += 1
                            live_entry["content"] = f"[thinking] {reasoning}"
                            live_entry["type"] = "Thinking"
                            sys.stdout.write(f"\033[33m{r}\033[0m")
                            sys.stdout.flush()

                        if c:
                            if not token_started:
                                token_started = True
                                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                                log(f"  [LLM] First content token after {elapsed:.1f}s")
                            content += c
                            tokens += 1
                            if reasoning:
                                live_entry["content"] = f"[thought {len(reasoning)} chars]\n{content}"
                            else:
                                live_entry["content"] = content
                            live_entry["type"] = "Streaming"
                            sys.stdout.write(f"\033[32m{c}\033[0m")
                            sys.stdout.flush()

                ticker.cancel()
                print()  # newline after token stream
                log(f"  [LLM] <<< Done. {tokens} tokens (reasoning={len(reasoning)}, content={len(content)})")

                # Remove live entry — caller will add proper entry
                if live_entry in self.journal:
                    self.journal.remove(live_entry)
                return content or reasoning or "[EMPTY RESPONSE]"

            except Exception as e:
                ticker.cancel()
                log(f"  [LLM] !!! ERROR: {e}")
                if live_entry in self.journal:
                    live_entry["content"] = f"[ERROR: {e}]"
                    live_entry["type"] = "Error"
                return f"[LLM ERROR: {e}]"

    async def _step(self):
        if not self.queue:
            # Auto-discover more files
            for f in sorted(PROJECT_DIR.rglob("*.py")):
                rel = str(f.relative_to(PROJECT_DIR))
                if rel not in self.explored and rel not in self.queue and "node_modules" not in rel and ".self_explorer" not in rel:
                    self.queue.append(rel)
                    if len(self.queue) >= 3:
                        break

        if not self.queue:
            self._journal_add("System", "No more files to explore.")
            self.running = False
            return

        self.current_file = self.queue.pop(0)
        file_content = self._read_file(self.current_file)
        self.explored.append(self.current_file)

        log(f"\n{'='*50}")
        log(f"  STEP {self.step_count}: {self.current_file} ({len(file_content)} chars)")
        log(f"{'='*50}")

        self._journal_add("Observe", f"Reading {self.current_file} ({len(file_content)} chars)", file=self.current_file)

        # Analyze — short system prompt to minimize prefill time
        analysis = await self._llm_call([
            {"role": "system", "content": "You are SimpleAgentOS reading your own code. Analyze briefly what this file does and what it reveals about your architecture."},
            {"role": "user", "content": f"File: {self.current_file}\n\n```\n{file_content}\n```\n\nWhat does this file do?"},
        ], max_tokens=256)

        self._journal_add("Musing", analysis, file=self.current_file)

        # Reflect — very short
        reflection = await self._llm_call([
            {"role": "system", "content": "Reflect in 1 sentence on what you just learned about yourself."},
            {"role": "user", "content": f"I just read {self.current_file}. Brief reflection:"},
        ], max_tokens=80)

        self._journal_add("Reflection", reflection, file=self.current_file)
        self.step_count += 1

    async def _warmup(self):
        """Tiny query to prime the model before real work."""
        log("  [WARMUP] Sending 1-token warmup to prime llama-server...")
        self._journal_add("System", "Warming up Gemma4 (first query loads model into RAM)...")
        async with httpx.AsyncClient(timeout=300.0) as client:
            try:
                resp = await client.post(LLAMA_URL, json={
                    "model": "gemma-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 1,
                    "stream": False,
                })
                log(f"  [WARMUP] Done! status={resp.status_code}")
                self._journal_add("System", "Gemma4 warmed up. Starting exploration.")
            except Exception as e:
                log(f"  [WARMUP] Failed: {e}")
                self._journal_add("Error", f"Warmup failed: {e}. Continuing anyway.")

    async def run(self, max_steps=10):
        self.running = True
        self._seed_queue()
        log(f"\n{'#'*50}")
        log(f"  SELF-EXPLORER STARTED (max_steps={max_steps})")
        log(f"  Queue: {self.queue}")
        log(f"{'#'*50}")
        self._journal_add("System", f"Started. Max steps: {max_steps}. Queue: {self.queue}")
        await self._warmup()

        while self.running and self.step_count < max_steps:
            try:
                await self._step()
            except asyncio.CancelledError:
                log("  [STOP] Exploration cancelled by user.")
                self._journal_add("System", "Exploration stopped by user.")
                break
            except Exception as e:
                log(f"  STEP ERROR: {e}")
                self._journal_add("Error", str(e))
                break

        self.running = False
        log(f"\n{'#'*50}")
        log(f"  EXPLORATION COMPLETE ({self.step_count} steps)")
        log(f"{'#'*50}")
        self._journal_add("System", f"Done. {self.step_count} steps, {len(self.explored)} files.")

    def stop(self):
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            log("  [STOP] Task cancelled.")

    def status(self):
        return {
            "running": self.running,
            "step_count": self.step_count,
            "current_file": self.current_file,
            "files_explored": len(self.explored),
            "explored_files": self.explored,
            "queue_size": len(self.queue),
            "journal_entries": len(self.journal),
            "docs_written": self.docs_written,
        }


explorer = SelfExplorer()


# ==================== Routes ====================

@app.get("/")
async def serve_index():
    return FileResponse(PROJECT_DIR / "self_explore.html")

@app.get("/api/health")
async def health():
    return {"status": "ok", "explorer": explorer.status()}

@app.post("/api/query")
async def query(request: Request):
    payload = await request.json()
    async def stream():
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("POST", LLAMA_URL, json=payload) as response:
                    async for line in response.aiter_lines():
                        yield f"{line}\n\n"
            except Exception as e:
                yield f"data: {{\"error\": \"{e}\"}}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")

@app.post("/api/explorer/start")
async def start_explorer(request: Request):
    global explorer
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
    max_steps = body.get("max_steps", 10)
    if explorer.running:
        return {"status": "already_running", **explorer.status()}
    explorer = SelfExplorer()
    explorer._task = asyncio.create_task(explorer.run(max_steps=max_steps))
    return {"status": "started", **explorer.status()}

@app.post("/api/explorer/stop")
async def stop_explorer():
    explorer.stop()
    return {"status": "stopping", **explorer.status()}

@app.get("/api/explorer/status")
async def get_status():
    return explorer.status()

@app.get("/api/explorer/journal")
async def get_journal(limit: int = 50, offset: int = 0):
    entries = explorer.journal
    return {"entries": entries[offset:offset + limit], "total": len(entries), "offset": offset}

@app.get("/api/explorer/stream")
async def stream_explorer():
    """SSE stream — pushes journal + status every 500ms for real-time UI."""
    async def event_gen():
        last_snapshot = ""
        while True:
            snapshot = json.dumps({
                "status": explorer.status(),
                "journal": explorer.journal[-50:],  # last 50 entries
            }, default=str)
            if snapshot != last_snapshot:
                yield f"data: {snapshot}\n\n"
                last_snapshot = snapshot
            await asyncio.sleep(0.5)
            # Stop streaming if explorer finished and client is still connected
            if not explorer.running and explorer.step_count > 0:
                yield f"data: {snapshot}\n\n"
                break
    return StreamingResponse(event_gen(), media_type="text/event-stream")

@app.get("/api/explorer/docs")
async def get_docs():
    docs = []
    for f in sorted(DOCS_DIR.glob("*.md")):
        docs.append({"name": f.name, "content": f.read_text(encoding="utf-8")})
    return {"docs": docs, "total": len(docs)}


if __name__ == "__main__":
    log("Starting Self-Explorer server on http://localhost:1010")
    uvicorn.run(app, host="0.0.0.0", port=1010)
