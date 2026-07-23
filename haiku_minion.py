"""
haiku_minion.py — Tiny harness for delegating one-shot tasks to Claude Haiku.

Uses the local `claude` CLI in print mode (OAuth via Claude Code keychain, no
ANTHROPIC_API_KEY needed). If ANTHROPIC_API_KEY is set the anthropic SDK is
used instead — same surface, fewer moving parts.

Python API:

    from haiku_minion import ask, ask_json, summarize, pick, quip

    reply  = ask("Describe a tired server in 8 words.")
    data   = ask_json("List 3 cocktails as JSON array of {name, spirit}.",
                      schema={"type": "array"})
    tldr   = summarize(long_text, max_words=25)
    winner = pick("which is warmer", ["tundra", "sauna", "fridge"])
    line   = quip("Rapanui Rock at sunset", "harness commit day")

Shell:

    python3 haiku_minion.py "one-line cheeky caption"
    python3 haiku_minion.py --json "list 3 python idioms"
    python3 haiku_minion.py --system "you are terse" "explain pooling"
    python3 haiku_minion.py --cache daily-quip "..."

Design notes:
  - Each call is independent. No session state.
  - Errors are logged to stderr and a `haiku_minion.failed` flag is set on the
    returned empty result via module-level `last_error`. Callers who want to
    fail hard can check `last_error` after the call.
  - Cache writes are atomic (tempfile + os.replace).
  - `_clean()` is OFF by default for `ask()` so legitimate quoted output
    survives. Convenience helpers (quip, summarize, pick) opt in.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ── Config ──────────────────────────────────────────────────────────────────

# Newest Haiku alias. Floating — picks up minor revisions within the line.
# Bump this string when Anthropic ships a newer Haiku generation (5.x, 6.x, …).
# Runtime override: `HAIKU_MINION_MODEL=<id>` env var.
_NEWEST_HAIKU = "claude-haiku-4-5"
DEFAULT_MODEL = os.environ.get("HAIKU_MINION_MODEL", _NEWEST_HAIKU)

DEFAULT_TIMEOUT = 30
CACHE_DIR = Path.home() / ".cache" / "haiku-minion"

log = logging.getLogger("haiku_minion")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[haiku_minion:%(levelname)s] %(message)s"))
    log.addHandler(_h)
log.setLevel(os.environ.get("HAIKU_MINION_LOG", "WARNING"))

# Exposed so callers can inspect why an empty result came back.
last_error: Optional[str] = None


# ── Cache ───────────────────────────────────────────────────────────────────


def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:120]
    return CACHE_DIR / f"{safe}.json"


def _read_cache(key: str) -> Optional[str]:
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("text")
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cache read failed for %s: %s", key, e)
        return None


def _write_cache(key: str, text: str, model: str) -> None:
    """Atomic write: tempfile in same dir, fsync, os.replace."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "text": text,
            "model": model,
            "cached_at": datetime.now().isoformat(),
        }
        dest = _cache_path(key)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{dest.name}.", suffix=".tmp", dir=CACHE_DIR
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except OSError as e:
        log.warning("cache write failed for %s: %s", key, e)


# ── Text cleanup (opt-in) ───────────────────────────────────────────────────


def _strip_wrappers(text: str) -> str:
    """Strip fence + matched outer quotes that models sometimes add.

    Only touches wrappers that enclose the WHOLE output. Mid-string quotes
    are untouched.
    """
    t = (text or "").strip()
    # Strip fenced block only if the whole output is one fence
    if t.startswith("```") and t.endswith("```"):
        lines = t.splitlines()
        if len(lines) >= 2:
            t = "\n".join(lines[1:-1]).strip()
    # Strip matched outer quotes ONLY if the interior has no matching closer
    # (so 'he said "hi" today' isn't mangled — but '"hi"' becomes 'hi')
    for q in ('"', "'", "`"):
        if len(t) >= 2 and t[0] == q and t[-1] == q and t.count(q) == 2:
            t = t[1:-1].strip()
            break
    return t


# ── Transport ───────────────────────────────────────────────────────────────


def _use_sdk() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _call_sdk(prompt: str, system: Optional[str], model: str,
              timeout: int) -> str:
    """Primary path when ANTHROPIC_API_KEY is set. Raises on error."""
    import anthropic  # lazy import — stdlib-only path still works
    client = anthropic.Anthropic(timeout=timeout)
    kwargs = {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    return "".join(parts).strip()


def _call_cli(prompt: str, system: Optional[str], model: str,
              timeout: int) -> str:
    """Fallback path via `claude -p`. Raises on error."""
    cmd = ["claude", "-p", "--model", model]
    if system:
        cmd.extend(["--append-system-prompt", system])
    cmd.append(prompt)
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude exited {result.returncode}: "
            f"{(result.stderr or '').strip()[:400]}"
        )
    return (result.stdout or "").strip()


# ── Core ────────────────────────────────────────────────────────────────────


def ask(
    prompt: str,
    system: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    cache_key: Optional[str] = None,
    force: bool = False,
    strip_wrappers: bool = False,
) -> str:
    """Send prompt to haiku, return text. Empty string on failure.

    Failures set module-level `last_error` and log to stderr. Callers who
    want to fail hard can check `last_error` after the call.
    """
    global last_error
    last_error = None

    if not prompt or not prompt.strip():
        last_error = "empty prompt"
        return ""

    if cache_key and not force:
        hit = _read_cache(cache_key)
        if hit is not None:
            return _strip_wrappers(hit) if strip_wrappers else hit

    try:
        if _use_sdk():
            text = _call_sdk(prompt, system, model, timeout)
        else:
            text = _call_cli(prompt, system, model, timeout)
    except subprocess.TimeoutExpired as e:
        last_error = f"timeout after {e.timeout}s"
        log.warning(last_error)
        return ""
    except FileNotFoundError:
        last_error = "claude CLI not on PATH and no ANTHROPIC_API_KEY set"
        log.error(last_error)
        return ""
    except ImportError as e:
        last_error = f"anthropic SDK import failed: {e}"
        log.error(last_error)
        return ""
    except Exception as e:  # transport/network/SDK — bucketed but logged
        last_error = f"{type(e).__name__}: {e}"
        log.warning(last_error)
        return ""

    if not text:
        last_error = "empty response"
        return ""

    if cache_key:
        _write_cache(cache_key, text, model)

    return _strip_wrappers(text) if strip_wrappers else text


def ask_json(
    prompt: str,
    schema: Optional[dict] = None,
    system: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    cache_key: Optional[str] = None,
    force: bool = False,
) -> Any:
    """Ask haiku for JSON. Returns parsed object, or {} / [] on failure.

    JSON output is always wrapper-stripped before parsing (models like to
    fence JSON in ```json blocks).
    """
    global last_error
    last_error = None

    if cache_key and not force:
        hit = _read_cache(cache_key)
        if hit is not None:
            try:
                return json.loads(hit)
            except json.JSONDecodeError as e:
                log.warning("cached json invalid, re-fetching: %s", e)

    if schema is not None and _use_sdk():
        # SDK has no `--json-schema` flag equivalent without tool use; append hint.
        full_prompt = (
            f"{prompt}\n\nRespond with JSON matching this schema. No prose, "
            f"no code fences.\n\nSchema:\n{json.dumps(schema)}"
        )
        raw = ask(full_prompt, system=system, model=model, timeout=timeout,
                  strip_wrappers=True)
    elif schema is not None:
        try:
            cmd = ["claude", "-p", "--model", model,
                   "--json-schema", json.dumps(schema)]
            if system:
                cmd.extend(["--append-system-prompt", system])
            cmd.append(prompt)
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
            if result.returncode != 0:
                last_error = (
                    f"claude (schema mode) exited {result.returncode}: "
                    f"{(result.stderr or '').strip()[:400]}"
                )
                log.warning(last_error)
                return {} if _expects_object(schema) else []
            raw = _strip_wrappers(result.stdout)
        except subprocess.TimeoutExpired as e:
            last_error = f"timeout after {e.timeout}s (json schema mode)"
            log.warning(last_error)
            return {} if _expects_object(schema) else []
        except FileNotFoundError:
            last_error = "claude CLI not on PATH"
            log.error(last_error)
            return {} if _expects_object(schema) else []
    else:
        full_prompt = (
            prompt
            + "\n\nRespond with JSON only. No prose, no code fences, no preamble."
        )
        raw = ask(full_prompt, system=system, model=model, timeout=timeout,
                  strip_wrappers=True)

    if not raw:
        return {} if _expects_object(schema) else []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        last_error = f"invalid json: {e}"
        log.warning(last_error)
        return {} if _expects_object(schema) else []

    if cache_key:
        _write_cache(cache_key, raw, model)
    return data


def _expects_object(schema: Optional[dict]) -> bool:
    return bool(schema and schema.get("type") == "object")


# ── Convenience helpers ────────────────────────────────────────────────────


def summarize(text: str, max_words: int = 30, **kwargs) -> str:
    if not text.strip():
        return ""
    prompt = (
        f"Summarize in at most {max_words} words. Plain sentence. No list, "
        f"no preamble:\n\n{text}"
    )
    return ask(prompt, strip_wrappers=True, **kwargs)


def pick(question: str, choices: list[str], **kwargs) -> str:
    if not choices:
        return ""
    enum = "\n".join(f"- {c}" for c in choices)
    prompt = (
        f"{question}\n\nChoose exactly ONE of these, copy it verbatim, "
        f"no other text:\n{enum}"
    )
    out = ask(prompt, strip_wrappers=True, **kwargs).strip()
    for c in choices:
        if c.lower() == out.lower():
            return c
    for c in choices:
        if c.lower() in out.lower():
            return c
    return ""


def quip(subject: str, context: str, style: str = "dry wit, playful", **kwargs) -> str:
    prompt = (
        f"Subject: {subject}\nContext: {context}\n\n"
        f"Write ONE caption (max 25 words) tying subject to context. "
        f"Style: {style}. No emoji, no surrounding quotes, no preamble. "
        f"Output only the single line."
    )
    return ask(prompt, strip_wrappers=True, **kwargs)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="haiku_minion",
        description="Delegate one-shot tasks to Claude Haiku.",
    )
    p.add_argument("prompt", nargs="+", help="The prompt text.")
    p.add_argument("--json", action="store_true",
                   help="Parse response as JSON and pretty-print.")
    p.add_argument("--system", default=None,
                   help="Optional system prompt appended to defaults.")
    p.add_argument("--cache", dest="cache_key", default=None,
                   help="Cache key for this call (persists under ~/.cache/haiku-minion).")
    p.add_argument("--force", action="store_true",
                   help="Bypass cache even if --cache is set.")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Model id (default: {DEFAULT_MODEL}).")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help=f"Timeout seconds (default: {DEFAULT_TIMEOUT}).")
    p.add_argument("--strip", action="store_true",
                   help="Strip wrapping quotes/fences from text output.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable info-level logs on stderr.")
    return p


def _cli(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv[1:])
    if args.verbose:
        log.setLevel(logging.INFO)

    prompt = " ".join(args.prompt)
    common = dict(system=args.system, model=args.model,
                  timeout=args.timeout, cache_key=args.cache_key,
                  force=args.force)

    if args.json:
        data = ask_json(prompt, **common)
        print(json.dumps(data, indent=2))
    else:
        print(ask(prompt, strip_wrappers=args.strip, **common))

    return 0 if not last_error else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
