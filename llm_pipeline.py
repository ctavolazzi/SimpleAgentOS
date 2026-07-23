#!/usr/bin/env python3
"""
llm_pipeline.py — Multi-provider free-tier LLM pipeline for WE System v2.

Stages:
  1. Draft (multi-provider cascade) — fast free-tier models in priority order
     - Groq (Llama 3.1 8B, ~100ms TTFT, 14.4K RPD)
     - Together AI (Llama 3.1 8B Turbo, ZDR enabled)
     - DeepSeek (V4 Flash, concurrency-based)
     - OpenRouter (Gemma 2 9B, fallback)
  2. Sanitize (minimal) — shape validation, truncation, strip control chars
  3. Judge & Patch (Haiku) — significance gate + content verification + patching
  4. Fallback chain — Haiku-only → deterministic rule-based → error

Supports full simulation mode for dry runs (no network, canned responses).

Environment variables:
  GROQ_API_KEY           — enable Groq provider (primary)
  GROQ_MODEL             — override model (default: llama-3.1-8b-instant)
  WE_FACTORY_NO_GROQ=1   — disable Groq

  TOGETHER_API_KEY       — enable Together AI (secondary, ZDR)
  TOGETHER_MODEL         — override model
  WE_FACTORY_NO_TOGETHER=1

  DEEPSEEK_API_KEY       — enable DeepSeek (tertiary)
  DEEPSEEK_MODEL         — override model
  WE_FACTORY_NO_DEEPSEEK=1

  OPENROUTER_API_KEY     — enable OpenRouter (quaternary)
  OPENROUTER_MODEL       — override model
  WE_FACTORY_NO_OPENROUTER=1

Usage:
    from llm_pipeline import gate_and_draft, WE_FIELD_SPEC
    result = gate_and_draft("task text", field_spec=WE_FIELD_SPEC)
    print(result['source'], result['worthy'], result['content'])
"""

import os
import json
import re
import hashlib
import subprocess
from pathlib import Path
from typing import Optional, TypedDict
from datetime import datetime

try:
    from rich.console import Console
    _CONSOLE = Console(stderr=True)
except ImportError:
    _CONSOLE = None


# Telemetry log for fail-closed parse failures. Forensic trail for diagnosing
# which providers/models hallucinate which kinds of malformed payloads.
_FAILED_PARSE_LOG = Path.home() / ".spin_up" / "runs" / "cognitive" / "failed_parse.log"


# Regex pipeline for extracting JSON from non-deterministic LLM output.
# 1. Markdown code fences with optional language tag (```json … ``` or ``` … ```)
# 2. Bracket fallback — outermost {...} block when no fence is present
_FENCE_RE = re.compile(
    r"^```(?:json|markdown)?\s*\n?(.*?)\n?```",
    re.MULTILINE | re.DOTALL,
)
_BRACKET_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_payload(raw: str) -> str:
    """Strip preamble/postscript and markdown fences from an LLM payload.

    Multi-pass:
      1. If wrapped in markdown fences, take the inside.
      2. Otherwise, take the outermost curly-brace block.
      3. Otherwise, return stripped raw text and let json.loads decide.
    """
    text = raw.strip()
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        return fence_match.group(1).strip()
    bracket_match = _BRACKET_RE.search(text)
    if bracket_match:
        return bracket_match.group(0).strip()
    return text


def _log_parse_failure(provider: str, model: str, raw: str, exc: Exception) -> None:
    """Append raw hallucinated payload + error to the failed_parse log.

    Best-effort: never raises. If the telemetry directory cannot be created
    or written, we silently drop the record rather than crashing the pipeline.
    """
    try:
        _FAILED_PARSE_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().isoformat() + "Z"
        with _FAILED_PARSE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- {ts} | provider={provider} | model={model} ---\n")
            fh.write(f"error: {type(exc).__name__}: {exc}\n")
            fh.write("payload:\n")
            fh.write(raw if isinstance(raw, str) else repr(raw))
            fh.write("\n")
    except OSError:
        pass


class CognitiveGateError(Exception):
    """Raised when an LLM response cannot be parsed even after extraction."""


# ── Configuration ──────────────────────────────────────────────────────────

# Provider endpoints and defaults
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

TOGETHER_URL = "https://api.together.xyz/v1/chat/completions"
TOGETHER_MODEL = os.environ.get("TOGETHER_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemma-2-9b-it:free")

WE_FIELD_SPEC = {
    "slug": {
        "kind": "str",
        "max_len": 60,
        "regex": r"^[a-z][a-z0-9_]*$",
        "word_count": (3, 6),
        "required": True,
    },
    "title": {"kind": "str", "max_len": 80, "required": True},
    "tags": {
        "kind": "list",
        "max_len": 6,
        "item_regex": r"^[a-z][a-z0-9-]*$",
        "required": False,
        "default": ["general"],
    },
    "plan_body": {"kind": "str", "max_len": 600, "required": False, "default": ""},
}


class GateDraftResult(TypedDict):
    worthy: bool
    reason: str
    suggested_parent: Optional[str]
    content: dict
    source: str
    sanitized_issues: list
    verify_patches: dict
    reason_detail: Optional[str]


# ── Helpers ────────────────────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
    if text.endswith("```"):
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _rule_based_slug(task: str) -> str:
    """Deterministic slug: lowercase, spaces→underscore, strip non-alnum."""
    s = re.sub(r"[^\w\s-]", "", task.lower())
    s = re.sub(r"[-\s]+", "_", s).strip("_")
    return s[:40].rstrip("_") or "untitled"


def _json_schema(field_spec: dict) -> dict:
    """Derive JSON Schema dict from internal field_spec."""
    props, req = {}, []
    for k, spec in field_spec.items():
        if spec["kind"] == "list":
            props[k] = {"type": "array", "items": {"type": "string"}}
        else:
            props[k] = {"type": "string"}
        if spec.get("required"):
            req.append(k)
    return {
        "type": "object",
        "properties": props,
        "required": req,
    }


def _field_spec_summary(spec: dict) -> str:
    """Human-readable field spec description."""
    lines = []
    for k, s in spec.items():
        req = "required" if s.get("required") else "optional"
        kind = s["kind"]
        max_len = s.get("max_len", "∞")
        lines.append(f"  {k}: {kind} ({req}, max {max_len})")
    return "\n".join(lines)


# ── Stage 1: Draft via OpenRouter ──────────────────────────────────────────


def _draft_provider(
    url: str, model: str, api_key: str, task: str, *, field_spec: dict, timeout: float = 8.0, extra_headers: dict = None
) -> Optional[dict]:
    """Generic provider draft call. Returns JSON dict or None on failure.

    Wraps blocking network I/O in a rich.status spinner so users don't see
    a silent terminal hang during the 5-30s inference window. On parse
    failure, raw payload is appended to ~/.spin_up/runs/cognitive/failed_parse.log
    for forensic analysis, then None is returned so the cascade can fall
    through to the next provider.
    """
    if not api_key:
        return None

    prompt = (
        f"Task: {task}\n\n"
        f"Generate JSON matching this structure:\n{_field_spec_summary(field_spec)}\n\n"
        "Output ONLY valid JSON. No prose, no markdown fences."
    )

    raw = None
    try:
        import urllib.request

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(
            url,
            headers=headers,
            data=json.dumps(
                {
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Output ONLY JSON. No prose, no code fences.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 400,
                    "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                }
            ).encode(),
        )

        # Spinner suppressed automatically when stderr is not a TTY (e.g. MCP server).
        if _CONSOLE is not None:
            with _CONSOLE.status(f"[cyan]Cognitive gate · querying {model}…", spinner="dots"):
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = json.loads(resp.read())
        else:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())

        raw = body["choices"][0]["message"]["content"]
        payload = _extract_json_payload(raw)
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        _log_parse_failure(provider=url, model=model, raw=raw or "", exc=exc)
        return None
    except Exception as exc:
        # Network/transport errors logged at lower priority — these are
        # expected during free-tier rate-limit cascades.
        if raw is not None:
            _log_parse_failure(provider=url, model=model, raw=raw, exc=exc)
        return None


def _draft_groq(task: str, *, field_spec: dict, timeout: float = 8.0) -> Optional[dict]:
    """Draft via Groq (primary: fast LPU-based inference)."""
    if os.environ.get("WE_FACTORY_NO_GROQ") == "1":
        return None
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    return _draft_provider(GROQ_URL, GROQ_MODEL, key, task, field_spec=field_spec, timeout=timeout)


def _draft_together(task: str, *, field_spec: dict, timeout: float = 8.0) -> Optional[dict]:
    """Draft via Together AI (secondary: ZDR privacy, high availability)."""
    if os.environ.get("WE_FACTORY_NO_TOGETHER") == "1":
        return None
    key = os.environ.get("TOGETHER_API_KEY")
    if not key:
        return None
    extra_headers = {"X-Together-No-Store": "true"}
    return _draft_provider(TOGETHER_URL, TOGETHER_MODEL, key, task, field_spec=field_spec, timeout=timeout, extra_headers=extra_headers)


def _draft_deepseek(task: str, *, field_spec: dict, timeout: float = 8.0) -> Optional[dict]:
    """Draft via DeepSeek (tertiary: concurrency-based rate limiting)."""
    if os.environ.get("WE_FACTORY_NO_DEEPSEEK") == "1":
        return None
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    return _draft_provider(DEEPSEEK_URL, DEEPSEEK_MODEL, key, task, field_spec=field_spec, timeout=timeout)


def _draft_openrouter(task: str, *, field_spec: dict, timeout: float = 8.0) -> Optional[dict]:
    """Draft via OpenRouter (quaternary: aggregator fallback)."""
    if os.environ.get("WE_FACTORY_NO_OPENROUTER") == "1":
        return None
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    extra_headers = {
        "HTTP-Referer": "https://github.com/ctavolazzi/SimpleAgentOS",
        "X-Title": "SimpleAgentOS we_factory",
    }
    return _draft_provider(OPENROUTER_URL, OPENROUTER_MODEL, key, task, field_spec=field_spec, timeout=timeout, extra_headers=extra_headers)


# Backward compatibility alias
def draft(task: str, *, field_spec: dict, timeout: float = 8.0) -> Optional[dict]:
    """Deprecated: use gate_and_draft() instead. Falls back to OpenRouter only."""
    return _draft_openrouter(task, field_spec=field_spec, timeout=timeout)


# ── Stage 2: Sanitize (minimal) ────────────────────────────────────────────


def sanitize(raw: dict, *, field_spec: dict) -> tuple[dict, list[str]]:
    """Validate shape, truncate, strip control chars. Return (cleaned, issues)."""
    cleaned = {}
    issues = []

    for k, spec in field_spec.items():
        if k not in raw:
            if spec.get("required"):
                issues.append(f"missing:{k}")
            else:
                cleaned[k] = spec.get("default", "")
            continue

        val = raw[k]
        if spec["kind"] == "list":
            if not isinstance(val, list):
                issues.append(f"{k}:wrong_type")
                continue
            cleaned[k] = []
            for item in val:
                s = str(item).strip()
                s = "".join(c for c in s if c.isprintable() or c == "\n")
                s = s.strip("`").strip()
                s = _strip_fences(s)
                if re.match(spec.get("item_regex", r".*"), s):
                    cleaned[k].append(s)
                else:
                    issues.append(f"{k}:item_regex_fail")
        else:
            s = str(val).strip()
            s = "".join(c for c in s if c.isprintable() or c == "\n")
            s = s.strip("`").strip()
            s = _strip_fences(s)
            max_len = spec.get("max_len", float("inf"))
            if len(s) > max_len:
                s = " ".join(s[: int(max_len)].split()[:-1])
                issues.append(f"{k}:truncated")
            if k == "slug":
                if not re.match(spec.get("regex", r".*"), s):
                    s = _rule_based_slug(s)
                    if not re.match(spec.get("regex", r".*"), s):
                        issues.append(f"{k}:regex_fail")
                        continue
                words = len(s.split("_"))
                if not (spec["word_count"][0] <= words <= spec["word_count"][1]):
                    issues.append(f"{k}:word_count_fail")
                    continue
            cleaned[k] = s

    return cleaned, issues


# ── Stage 3: Judge & Patch via Haiku ───────────────────────────────────────


def judge_and_patch(
    task: str, draft_dict: dict, *, context: Optional[dict] = None, field_spec: dict = None
) -> dict:
    """Haiku evaluates: worthy? If yes, return patches. If no, return usable=false."""
    if field_spec is None:
        field_spec = WE_FIELD_SPEC
    import haiku_minion

    recent_titles = (context or {}).get("recent_we_titles", [])[:5]
    recent_activity = (context or {}).get("recent_activity", "")

    prompt = (
        f"TASK: {task}\n\n"
        f"DRAFT CONTENT: {json.dumps(draft_dict)}\n\n"
        f"RECENT WORK EFFORTS (for dedup / parent inference):\n"
        + "\n".join(f"- {t}" for t in recent_titles)
        + "\n\n"
        f"RECENT ACTIVITY:\n{recent_activity}\n\n"
        "DECIDE:\n"
        "1) Is this task WE-worthy? WEs = multi-hour, multi-step, distinct deliverables "
        "with planning/notes/subtasks. NOT for single-command chores.\n"
        "2) If worthy, patch any fields that are shape-invalid or missing.\n"
        "3) If the task is a sub-part of one of the recent WEs, suggest that wikilink "
        "as suggested_parent (format: \"[[10.NN_YYYYMMDD_slug]]\"). Else null.\n\n"
        "Return JSON: {worthy: bool, reason: string, suggested_parent: string | null, "
        "patches: object}"
    )

    schema = {
        "type": "object",
        "properties": {
            "worthy": {"type": "boolean"},
            "reason": {"type": "string"},
            "suggested_parent": {"type": ["string", "null"]},
            "patches": {"type": "object"},
        },
        "required": ["worthy", "reason"],
    }

    result = haiku_minion.ask_json(prompt, schema=schema)
    if not result or haiku_minion.last_error:
        return {
            "worthy": True,
            "reason": "haiku_verify_failed_fallback",
            "suggested_parent": None,
            "patches": {},
        }
    return result


# ── Stage 4: Fallback chain ────────────────────────────────────────────────


def _haiku_only_generate(task: str, *, field_spec: dict = None, context: Optional[dict] = None) -> tuple[dict, dict]:
    """Haiku generates draft + judgment in one call."""
    if field_spec is None:
        field_spec = WE_FIELD_SPEC
    import haiku_minion

    prompt = (
        f"Task: {task}\n\n"
        f"Generate JSON matching:\n{_field_spec_summary(field_spec)}\n"
        "Return JSON only. No prose."
    )
    schema = _json_schema(field_spec)
    draft_result = haiku_minion.ask_json(prompt, schema=schema)
    if not draft_result or haiku_minion.last_error:
        raise Exception("haiku_only_generate failed")

    cleaned, _ = sanitize(draft_result, field_spec=field_spec)
    judgment = judge_and_patch(task, cleaned, context=context, field_spec=field_spec)
    return cleaned, judgment


def deterministic_fallback(task: str, *, field_spec: dict = None) -> dict:
    """Rule-based fallback: no LLM calls."""
    if field_spec is None:
        field_spec = WE_FIELD_SPEC
    return {
        "slug": _rule_based_slug(task) or "untitled_work_effort",
        "title": task.strip()[:80] or "Untitled",
        "tags": ["general", "auto-created"],
        "plan_body": "",
    }


# ── Orchestrator ───────────────────────────────────────────────────────────


def gate_and_draft(
    task: str,
    *,
    field_spec: dict = None,
    context: Optional[dict] = None,
    simulate: bool = False,
    timeout: float = 8.0,
) -> GateDraftResult:
    """Main orchestrator: multi-provider draft cascade → sanitize → judge & patch."""
    if field_spec is None:
        field_spec = WE_FIELD_SPEC

    # Simulation mode
    if simulate or os.environ.get("WE_FACTORY_SIMULATE") == "1":
        content = deterministic_fallback(task, field_spec=field_spec)
        return GateDraftResult(
            worthy=True,
            reason="[simulated]",
            suggested_parent=None,
            content=content,
            source="simulated",
            sanitized_issues=[],
            verify_patches={},
            reason_detail=None,
        )

    # Multi-provider cascade: Groq → Together → DeepSeek → OpenRouter
    draft_providers = [
        ("groq+haiku", _draft_groq),
        ("together+haiku", _draft_together),
        ("deepseek+haiku", _draft_deepseek),
        ("openrouter+haiku", _draft_openrouter),
    ]

    drafted = None
    draft_source = None
    for source_label, provider_fn in draft_providers:
        drafted = provider_fn(task, field_spec=field_spec, timeout=timeout)
        if drafted is not None:
            draft_source = source_label
            break

    # Fallback chain: if all providers fail
    if drafted is None:
        try:
            cleaned, judgment = _haiku_only_generate(task, field_spec=field_spec, context=context)
            return GateDraftResult(
                worthy=judgment.get("worthy", True),
                reason=judgment.get("reason", "haiku_only"),
                suggested_parent=judgment.get("suggested_parent"),
                content=cleaned,
                source="haiku_only",
                sanitized_issues=[],
                verify_patches={},
                reason_detail="all_free_providers_unavailable",
            )
        except Exception as e:
            content = deterministic_fallback(task, field_spec=field_spec)
            return GateDraftResult(
                worthy=True,
                reason="deterministic_fallback",
                suggested_parent=None,
                content=content,
                source="deterministic",
                sanitized_issues=[],
                verify_patches={},
                reason_detail=f"haiku_also_failed: {e}",
            )

    # Sanitize draft
    cleaned, issues = sanitize(drafted, field_spec=field_spec)

    # Judge & patch
    judgment = judge_and_patch(task, cleaned, context=context, field_spec=field_spec)
    patched = {**cleaned, **(judgment.get("patches") or {})}

    return GateDraftResult(
        worthy=judgment.get("worthy", True),
        reason=judgment.get("reason", "unknown"),
        suggested_parent=judgment.get("suggested_parent"),
        content=patched,
        source=draft_source,
        sanitized_issues=issues,
        verify_patches=judgment.get("patches") or {},
        reason_detail=None,
    )


if __name__ == "__main__":
    # Quick test
    result = gate_and_draft("Test WE for verification", simulate=True)
    print(json.dumps(result, default=str, indent=2))
