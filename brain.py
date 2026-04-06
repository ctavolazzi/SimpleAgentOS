"""
brain.py — Model-agnostic LLM adapter with full provenance tracking.

Supports local (llama-server, Ollama) and cloud (OpenAI-compatible) backends.
Every call is traced: model, endpoint, tokens, latency, cost, content hash.

Usage:
    brain = Brain(conn)  # SQLite connection from state_db
    brain.register("local-gemma4", "http://127.0.0.1:8080/v1/chat/completions",
                   model_id="gemma-4-E4B", backend="llama-server")
    brain.register("cloud-claude", "https://api.anthropic.com/v1/messages",
                   model_id="claude-sonnet-4-20250514", backend="anthropic", api_key="sk-...")

    result = await brain.call("local-gemma4", messages, max_tokens=256, stream=True)
    # result.text, result.trace_id, result.tokens_in, result.tokens_out, result.latency_ms
"""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx


def _now():
    return datetime.now(timezone.utc).isoformat()


def _uuid():
    return str(uuid.uuid4())


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# ── Database schema for brain traces ──────────────────────────────

def init_brain_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS brain_backends (
            name          TEXT PRIMARY KEY,
            url           TEXT NOT NULL,
            model_id      TEXT NOT NULL,
            backend_type  TEXT NOT NULL DEFAULT 'openai-compat',
            api_key_env   TEXT,
            config_json   TEXT DEFAULT '{}',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS brain_traces (
            id            TEXT PRIMARY KEY,
            backend_name  TEXT NOT NULL,
            model_id      TEXT NOT NULL,
            session_id    TEXT,
            messages_hash TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens  INTEGER DEFAULT 0,
            latency_ms    INTEGER DEFAULT 0,
            content       TEXT,
            content_hash  TEXT,
            reasoning     TEXT,
            finish_reason TEXT,
            error         TEXT,
            cost_usd      REAL DEFAULT 0.0,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (backend_name) REFERENCES brain_backends(name)
        );

        CREATE INDEX IF NOT EXISTS idx_traces_backend ON brain_traces(backend_name);
        CREATE INDEX IF NOT EXISTS idx_traces_session ON brain_traces(session_id);
        CREATE INDEX IF NOT EXISTS idx_traces_model ON brain_traces(model_id);
    """)
    conn.commit()


# ── Cost estimates (per 1M tokens) ────────────────────────────────

COST_TABLE = {
    # Local models — free
    "llama-server": {"input": 0.0, "output": 0.0},
    "ollama": {"input": 0.0, "output": 0.0},
    # Cloud models (approximate, USD per 1M tokens)
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-haiku-3.5": {"input": 0.80, "output": 4.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
}


def estimate_cost(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = COST_TABLE.get(model_id, {"input": 0.0, "output": 0.0})
    return (prompt_tokens * rates["input"] + completion_tokens * rates["output"]) / 1_000_000


# ── Result dataclass ──────────────────────────────────────────────

@dataclass
class BrainResult:
    text: str
    reasoning: str = ""
    trace_id: str = ""
    backend_name: str = ""
    model_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    content_hash: str = ""
    finish_reason: str = ""
    error: str | None = None


# ── Brain class ───────────────────────────────────────────────────

class Brain:
    """Model-agnostic LLM adapter with provenance tracking."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        init_brain_tables(conn)
        self._backends: dict[str, dict] = {}
        self._load_backends()

    def _load_backends(self):
        rows = self.conn.execute("SELECT * FROM brain_backends").fetchall()
        for r in rows:
            self._backends[r["name"]] = dict(r)

    def register(self, name: str, url: str, model_id: str,
                 backend_type: str = "openai-compat", api_key_env: str | None = None,
                 config: dict | None = None):
        """Register an LLM backend."""
        now = _now()
        self.conn.execute(
            "INSERT OR REPLACE INTO brain_backends (name, url, model_id, backend_type, api_key_env, config_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, url, model_id, backend_type, api_key_env, json.dumps(config or {}), now, now)
        )
        self.conn.commit()
        self._load_backends()

    def list_backends(self) -> list[dict]:
        return list(self._backends.values())

    def get_backend(self, name: str) -> dict | None:
        return self._backends.get(name)

    async def call(self, backend_name: str, messages: list[dict],
                   max_tokens: int = 256, session_id: str | None = None,
                   stream: bool = True,
                   on_token=None) -> BrainResult:
        """
        Call an LLM backend with full tracing.
        on_token: optional async callback(token_str, is_reasoning) for streaming UI.
        """
        backend = self._backends.get(backend_name)
        if not backend:
            return BrainResult(text="", error=f"Unknown backend: {backend_name}")

        url = backend["url"]
        model_id = backend["model_id"]
        api_key = None
        if backend.get("api_key_env"):
            api_key = os.environ.get(backend["api_key_env"])

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        msg_hash = _hash(json.dumps(messages))
        t0 = time.monotonic()
        trace_id = _uuid()

        reasoning = ""
        content = ""
        tokens_in = 0
        tokens_out = 0
        finish_reason = ""
        error = None

        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                payload = {
                    "model": model_id,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "stream": stream,
                }

                if stream:
                    async with client.stream("POST", url, json=payload, headers=headers) as resp:
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: ") or line == "data: [DONE]":
                                if line == "data: [DONE]":
                                    break
                                continue
                            try:
                                chunk = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue

                            # Extract usage from final chunk if present
                            usage = chunk.get("usage", {})
                            if usage:
                                tokens_in = usage.get("prompt_tokens", tokens_in)
                                tokens_out = usage.get("completion_tokens", tokens_out)

                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            fr = chunk.get("choices", [{}])[0].get("finish_reason")
                            if fr:
                                finish_reason = fr

                            r = delta.get("reasoning_content", "")
                            c = delta.get("content", "")

                            if r:
                                reasoning += r
                                tokens_out += 1
                                if on_token:
                                    await on_token(r, True)
                            if c:
                                content += c
                                tokens_out += 1
                                if on_token:
                                    await on_token(c, False)
                else:
                    resp = await client.post(url, json=payload, headers=headers)
                    data = resp.json()
                    choice = data.get("choices", [{}])[0]
                    msg = choice.get("message", {})
                    content = msg.get("content", "")
                    reasoning = msg.get("reasoning_content", "")
                    finish_reason = choice.get("finish_reason", "")
                    usage = data.get("usage", {})
                    tokens_in = usage.get("prompt_tokens", 0)
                    tokens_out = usage.get("completion_tokens", 0)

        except Exception as e:
            error = str(e)

        latency_ms = int((time.monotonic() - t0) * 1000)
        content_h = _hash(content) if content else ""
        cost = estimate_cost(model_id, tokens_in, tokens_out)

        # Store trace
        self.conn.execute(
            "INSERT INTO brain_traces (id, backend_name, model_id, session_id, messages_hash, "
            "prompt_tokens, completion_tokens, total_tokens, latency_ms, content, content_hash, "
            "reasoning, finish_reason, error, cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trace_id, backend_name, model_id, session_id, msg_hash,
             tokens_in, tokens_out, tokens_in + tokens_out, latency_ms,
             content, content_h, reasoning[:2000] if reasoning else None,
             finish_reason, error, cost, _now())
        )
        self.conn.commit()

        return BrainResult(
            text=content or reasoning or "",
            reasoning=reasoning,
            trace_id=trace_id,
            backend_name=backend_name,
            model_id=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost_usd=cost,
            content_hash=content_h,
            finish_reason=finish_reason,
            error=error,
        )

    # ── Analytics ─────────────────────────────────────────────────

    def get_traces(self, limit=50, backend_name=None, session_id=None) -> list[dict]:
        query = "SELECT * FROM brain_traces"
        params = []
        conditions = []
        if backend_name:
            conditions.append("backend_name=?")
            params.append(backend_name)
        if session_id:
            conditions.append("session_id=?")
            params.append(session_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_usage_summary(self) -> dict:
        """Aggregate usage stats across all backends."""
        rows = self.conn.execute("""
            SELECT backend_name, model_id,
                   COUNT(*) as calls,
                   SUM(prompt_tokens) as total_prompt,
                   SUM(completion_tokens) as total_completion,
                   SUM(total_tokens) as total_tokens,
                   AVG(latency_ms) as avg_latency_ms,
                   SUM(cost_usd) as total_cost,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as errors
            FROM brain_traces
            GROUP BY backend_name, model_id
        """).fetchall()
        return {"backends": [dict(r) for r in rows]}

    def get_model_comparison(self) -> list[dict]:
        """Compare model performance across backends."""
        rows = self.conn.execute("""
            SELECT model_id,
                   COUNT(*) as calls,
                   AVG(latency_ms) as avg_latency,
                   AVG(completion_tokens) as avg_tokens_out,
                   SUM(cost_usd) as total_cost,
                   AVG(CAST(LENGTH(content) AS REAL) / NULLIF(completion_tokens, 0)) as avg_chars_per_token
            FROM brain_traces
            WHERE error IS NULL
            GROUP BY model_id
            ORDER BY calls DESC
        """).fetchall()
        return [dict(r) for r in rows]
