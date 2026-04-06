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

import hashlib
import re
import shutil

import state_db as db
from brain import Brain
from ranch import Ranch

LLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"
PROJECT_DIR = Path(__file__).parent
DOCS_DIR = PROJECT_DIR / ".self_explorer" / "docs"
BACKUPS_DIR = PROJECT_DIR / ".self_explorer" / "backups"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
HTML_FILE = PROJECT_DIR / "self_explore.html"
CAPS_FILE = PROJECT_DIR / "capabilities.json"
MAX_FILE_CHARS = 1500  # Keep prompts small for fast prefill on CPU


def load_capabilities():
    if CAPS_FILE.exists():
        return json.loads(CAPS_FILE.read_text(encoding="utf-8"))
    return {"capabilities": []}


def save_capabilities(data):
    CAPS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

ZONE_START = "<!-- AGENT-ZONE-START -->"
ZONE_END = "<!-- AGENT-ZONE-END -->"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    """Print with flush so concurrently shows it immediately."""
    print(msg, flush=True)


app = FastAPI(title="SimpleAgentOS Self-Explorer")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Database init ──
_conn = db.get_conn()
db.init_db(_conn)
# Seed capabilities from JSON if DB is empty
if not db.get_capabilities(_conn):
    caps_data = load_capabilities()
    db.seed_capabilities(_conn, caps_data.get("capabilities", []))
    log(f"  [DB] Seeded {len(caps_data.get('capabilities', []))} capabilities from capabilities.json")

# ── Brain init ──
_brain = Brain(_conn)
# Auto-register the default local backend
if not _brain.get_backend("local-gemma4"):
    _brain.register("local-gemma4", LLAMA_URL, model_id="gemma-4-E4B",
                    backend_type="llama-server")
    log("  [BRAIN] Registered local-gemma4 backend")

# ── Ranch init ──
_ranch = Ranch(_conn)
log("  [RANCH] Initialized (stable, corral, trail, foreman)")


# ==================== Explorer ====================

class SelfExplorer:
    def __init__(self):
        self.journal: list[dict] = []  # In-memory for SSE streaming speed
        self.running = False
        self.step_count = 0
        self.explored: list[str] = []
        self.current_file: str | None = None
        self.queue: list[str] = []
        self.docs_written = 0
        self.ui_version = 0
        self.ui_hash = self._get_ui_hash()
        self._task: asyncio.Task | None = None
        self.session_id: str | None = None

    @staticmethod
    def _get_ui_hash():
        if HTML_FILE.exists():
            return hashlib.md5(HTML_FILE.read_bytes()).hexdigest()[:8]
        return "none"

    @staticmethod
    def _read_agent_zone():
        """Read the current content of the agent zone."""
        html = HTML_FILE.read_text(encoding="utf-8")
        start = html.find(ZONE_START)
        end = html.find(ZONE_END)
        if start == -1 or end == -1:
            return None
        return html[start + len(ZONE_START):end].strip()

    def _write_agent_zone(self, new_content: str):
        """Replace the agent zone content, with backup."""
        html = HTML_FILE.read_text(encoding="utf-8")
        start = html.find(ZONE_START)
        end = html.find(ZONE_END)
        if start == -1 or end == -1:
            log("  [UI] ERROR: Agent zone markers not found in HTML!")
            return False

        # Backup before modifying
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUPS_DIR / f"self_explore_{ts}_v{self.ui_version}.html"
        shutil.copy2(HTML_FILE, backup_path)
        log(f"  [UI] Backup saved: {backup_path.name}")

        # Inject new content
        new_html = (
            html[:start + len(ZONE_START)]
            + "\n"
            + new_content
            + "\n"
            + html[end:]
        )
        HTML_FILE.write_text(new_html, encoding="utf-8")
        self.ui_version += 1
        self.ui_hash = self._get_ui_hash()
        # Persist UI version to DB
        db.save_ui_version(_conn, self.ui_version, new_content, self.session_id)
        log(f"  [UI] Agent zone updated (v{self.ui_version}, hash={self.ui_hash})")
        return True

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
        # Persist to DB (skip live "Thinking" entries that update in-place)
        if self.session_id and entry_type not in ("Thinking",):
            db.add_journal_entry(_conn, self.session_id, self.step_count, entry_type, content, extra.get("file"))
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

        # ── Dream recall: have we thought about this file before? ──
        prior_context = ""
        priors = db.recall_prior_thoughts(_conn, file=self.current_file, limit=2)
        if priors:
            novel = False
            compressed_bits = []
            for p in priors:
                summary = p.get("summary") or p.get("compressed") or p.get("thought", "")[:150]
                compressed_bits.append(f"- {summary}")
            prior_context = "\n\nPrior thoughts on this file:\n" + "\n".join(compressed_bits)
            self._journal_add("DreamRecall", f"Found {len(priors)} prior thought(s) about {self.current_file}", file=self.current_file)
        else:
            novel = True

        # Analyze — short system prompt to minimize prefill time
        trigger = f"File: {self.current_file}\n\n```\n{file_content}\n```\n\nWhat does this file do?"
        analysis = await self._llm_call([
            {"role": "system", "content": "You are SimpleAgentOS reading your own code. Analyze briefly what this file does and what it reveals about your architecture."},
            {"role": "user", "content": trigger + prior_context},
        ], max_tokens=256)

        self._journal_add("Musing", analysis, file=self.current_file)

        # Store thought chain
        thought_entry = db.store_thought(_conn, self.session_id, trigger, analysis, file=self.current_file)
        if not thought_entry["is_novel"]:
            self._journal_add("System", f"Deja vu: similar thought existed (chain {thought_entry['prior_id'][:8]})", file=self.current_file)

        # Reflect — very short
        reflection = await self._llm_call([
            {"role": "system", "content": "Reflect in 1 sentence on what you just learned about yourself."},
            {"role": "user", "content": f"I just read {self.current_file}. Brief reflection:"},
        ], max_tokens=80)

        self._journal_add("Reflection", reflection, file=self.current_file)

        # Store reflection as compressed dream context for future recall
        db.store_dream(_conn, thought_entry["id"], reflection,
                       relevance_keys=thought_entry.get("keywords", ""))

        self.step_count += 1

        # Modify the UI after exploring self_explore.html, or every 3 steps
        if self.current_file == "self_explore.html" or self.step_count % 3 == 0:
            await self._modify_ui()

    async def _modify_ui(self):
        """Ask the LLM to generate HTML for the agent zone based on what it's learned."""
        explored_summary = ", ".join(self.explored) if self.explored else "nothing yet"
        current_zone = self._read_agent_zone() or "(empty)"

        # Build a context of what the agent has learned so far
        insights = []
        for entry in self.journal:
            if entry["type"] in ("Musing", "Reflection"):
                insights.append(f"[{entry['type']}] {entry['content'][:200]}")
        insights_text = "\n".join(insights[-6:])  # last 6 insights to keep prompt small

        self._journal_add("UIModify", f"Generating UI modification (v{self.ui_version + 1})...")

        new_html = await self._llm_call([
            {"role": "system", "content": (
                "You are an AI agent that can modify its own dashboard UI. "
                "Generate ONLY raw HTML (with inline CSS and JS if needed) for your agent zone. "
                "No markdown, no code fences, no explanation — just the HTML. "
                "The zone sits below the main dashboard in a dark terminal-style page (background: #020202, text: #00ff41). "
                "Use inline styles. You can create visualizations, status displays, or anything that helps you understand yourself."
            )},
            {"role": "user", "content": (
                f"Files explored so far: {explored_summary}\n\n"
                f"Your recent insights:\n{insights_text}\n\n"
                f"Current agent zone content:\n{current_zone}\n\n"
                f"Generate new HTML for your agent zone. Build something that visualizes what you've learned about your own architecture. "
                f"Include your name, what you've discovered, and a visual representation of your self-understanding."
            )},
        ], max_tokens=512)

        # Clean up: strip code fences if the model wraps them anyway
        cleaned = new_html.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:html)?\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)

        if len(cleaned) < 10 or "ERROR" in cleaned:
            self._journal_add("Error", f"UI modification failed: response too short or error")
            return

        success = self._write_agent_zone(cleaned)
        if success:
            self._journal_add("UIModify", f"Dashboard updated to v{self.ui_version}. The browser will reload.")
        else:
            self._journal_add("Error", "Failed to write agent zone — markers missing?")

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
        self.session_id = db.create_session(_conn, {"max_steps": max_steps})
        log(f"\n{'#'*50}")
        log(f"  SELF-EXPLORER STARTED (session={self.session_id[:8]}, max_steps={max_steps})")
        log(f"  Queue: {self.queue}")
        log(f"{'#'*50}")
        self._journal_add("System", f"Started. Session: {self.session_id[:8]}. Max steps: {max_steps}.")
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
        if self.session_id:
            db.end_session(_conn, self.session_id, self.step_count, self.explored)
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
            "ui_version": self.ui_version,
            "ui_hash": self.ui_hash,
        }


explorer = SelfExplorer()


# ==================== Routes ====================

@app.get("/")
async def serve_index():
    return FileResponse(PROJECT_DIR / "self_explore.html")

@app.get("/wild_west")
async def serve_wild_west():
    return FileResponse(PROJECT_DIR / "wild_west.html")

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

@app.get("/api/explorer/ui-hash")
async def get_ui_hash():
    """Returns current UI hash — browser polls this to detect modifications."""
    return {"hash": explorer.ui_hash, "version": explorer.ui_version}

@app.post("/api/explorer/revert")
async def revert_ui():
    """Revert to the most recent backup."""
    backups = sorted(BACKUPS_DIR.glob("self_explore_*.html"))
    if not backups:
        return {"status": "error", "message": "No backups available"}
    latest = backups[-1]
    shutil.copy2(latest, HTML_FILE)
    explorer.ui_hash = explorer._get_ui_hash()
    explorer.ui_version = max(0, explorer.ui_version - 1)
    log(f"  [REVERT] Restored from {latest.name}")
    return {"status": "reverted", "from": latest.name, "ui_hash": explorer.ui_hash}

@app.get("/api/explorer/backups")
async def list_backups():
    backups = sorted(BACKUPS_DIR.glob("self_explore_*.html"))
    return {"backups": [b.name for b in backups], "total": len(backups)}

@app.get("/api/explorer/docs")
async def get_docs():
    docs = []
    for f in sorted(DOCS_DIR.glob("*.md")):
        docs.append({"name": f.name, "content": f.read_text(encoding="utf-8")})
    return {"docs": docs, "total": len(docs)}


@app.get("/api/capabilities")
async def get_capabilities_endpoint():
    return {"capabilities": db.get_capabilities(_conn)}

@app.post("/api/capabilities/{cap_id}/toggle")
async def toggle_capability_endpoint(cap_id: str):
    result = db.toggle_capability(_conn, cap_id)
    if not result:
        return {"status": "error", "message": f"Capability '{cap_id}' not found"}
    if "error" in result:
        return {"status": "error", "message": result["error"]}
    log(f"  [CAPS] Toggled '{cap_id}' -> {'enabled' if result['enabled'] else 'disabled'}")
    return {"status": "ok", **result}

@app.post("/api/capabilities/add")
async def add_capability_endpoint(request: Request):
    body = await request.json()
    result = db.add_capability(_conn, body)
    log(f"  [CAPS] Added: {result['id']}")
    return {"status": "added", **result}

@app.get("/api/sessions")
async def list_sessions():
    return {"sessions": db.list_sessions(_conn)}

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    s = db.get_session(_conn, session_id)
    if not s:
        return {"status": "error", "message": "Session not found"}
    return s

@app.get("/api/sessions/{session_id}/journal")
async def get_session_journal(session_id: str, limit: int = 100, offset: int = 0):
    entries = db.get_journal(_conn, session_id, limit, offset)
    return {"entries": entries, "total": db.journal_count(_conn, session_id)}

@app.get("/api/stats")
async def get_stats():
    return db.get_stats(_conn)

@app.get("/api/brain/backends")
async def list_brain_backends():
    return {"backends": _brain.list_backends()}

@app.post("/api/brain/register")
async def register_brain_backend(request: Request):
    body = await request.json()
    _brain.register(
        name=body["name"], url=body["url"], model_id=body["model_id"],
        backend_type=body.get("backend_type", "openai-compat"),
        api_key_env=body.get("api_key_env"),
        config=body.get("config"),
    )
    log(f"  [BRAIN] Registered backend: {body['name']} ({body['model_id']})")
    return {"status": "registered", "name": body["name"]}

@app.get("/api/brain/traces")
async def get_brain_traces(limit: int = 50, backend: str | None = None, session: str | None = None):
    return {"traces": _brain.get_traces(limit, backend, session)}

@app.get("/api/brain/usage")
async def get_brain_usage():
    return _brain.get_usage_summary()

@app.get("/api/brain/compare")
async def compare_brain_models():
    return {"models": _brain.get_model_comparison()}

@app.get("/api/ui-versions")
async def list_ui_versions():
    return {"versions": db.get_ui_versions(_conn)}


# ── Ranch API ──

@app.get("/api/ranch/status")
async def ranch_status():
    return _ranch.full_status()

@app.get("/api/ranch/stable")
async def ranch_stable():
    return {"horses": _ranch.stable.roster()}

@app.post("/api/ranch/stable/check/{name}")
async def check_horse(name: str):
    entry = await _ranch.stable.check_horse(name)
    return {"name": name, "status": entry.status.value, "latency": entry.avg_latency_ms}

@app.post("/api/ranch/stable/check")
async def check_all_horses():
    await _ranch.stable.check_all()
    return {"horses": _ranch.stable.roster()}

@app.get("/api/ranch/trail")
async def ranch_trail(limit: int = 50):
    return {"trail": _ranch.trail.get_trail(limit=limit)}


if __name__ == "__main__":
    log("Starting Self-Explorer server on http://localhost:1010")
    uvicorn.run(app, host="0.0.0.0", port=1010)
