"""
atomic_io.py — Global concurrency manager for the Daily Note Harness.

Mitigates the Lost Update Anomaly across:
  - Local Python scripts (we_factory, daily_note, spin_up, wrap_up)
  - Autonomous MCP tool processes (docs-maintainer)

Provides three primitives:
  1. vault_lock(...)    — global flock-style lock at the vault root
  2. atomic_write(...)  — buffered temp-file write + os.rename swap
  3. DelayedKeyboardInterrupt — defers SIGINT until critical section ends

Lock file lives at <vault_root>/.vault_daily_note.lock. All processes that
mutate vault content MUST acquire the lock first via this module.
"""

from __future__ import annotations

import logging
import os
import signal
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

import portalocker

LOG = logging.getLogger(__name__)

VAULT_ROOT = Path.home() / "Documents" / "Personal-Remote-Vault"
LOCK_FILENAME = ".vault_daily_note.lock"

DEFAULT_TIMEOUT_S = 8.0
DEFAULT_CHECK_INTERVAL_S = 0.25


def _lock_path(vault_root: Optional[Path] = None) -> Path:
    return (vault_root or VAULT_ROOT) / LOCK_FILENAME


class DelayedKeyboardInterrupt:
    """Defer SIGINT until the critical section completes.

    Wrap lock acquisition + atomic write + lock release. Even repeated
    Ctrl+C spam is captured and replayed only on __exit__.
    """

    def __enter__(self):
        self.signal_received: Union[bool, tuple] = False
        try:
            self.old_handler = signal.signal(signal.SIGINT, self._handler)
            self._installed = True
        except ValueError:
            self.old_handler = None
            self._installed = False
        return self

    def _handler(self, sig, frame):
        self.signal_received = (sig, frame)
        LOG.debug("SIGINT trapped; deferring KeyboardInterrupt until critical section ends")

    def __exit__(self, exc_type, exc, tb):
        if self._installed:
            signal.signal(signal.SIGINT, self.old_handler)
        if self.signal_received and self.old_handler not in (None, signal.SIG_IGN, signal.SIG_DFL):
            self.old_handler(*self.signal_received)
        elif self.signal_received and self.old_handler is signal.SIG_DFL:
            raise KeyboardInterrupt
        return False


@contextmanager
def vault_lock(
    vault_root: Optional[Path] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    check_interval: float = DEFAULT_CHECK_INTERVAL_S,
) -> Iterator[None]:
    """Acquire global flock on the vault root. Block up to `timeout` seconds."""
    root = vault_root or VAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path(root)

    # Note: explicit `flags=LOCK_EX` triggers a "timeout has no effect in blocking mode"
    # warning. Letting portalocker default (LOCK_EX | LOCK_NB internally + retry loop)
    # is what actually honors the timeout parameter.
    lock = portalocker.Lock(
        str(lock_file),
        mode="a",
        timeout=timeout,
        check_interval=check_interval,
        fail_when_locked=False,
    )
    with DelayedKeyboardInterrupt():
        with lock:
            yield


def vault_write(path: Union[Path, str], content: str, encoding: str = "utf-8") -> None:
    """Lock + atomic replace. The one canonical way to write a vault file.

    Prefer this over the separate `vault_lock()` / `atomic_write()` calls
    for single-shot writes — it's the same guarantee with less ceremony.
    """
    with vault_lock():
        atomic_write(path, content, encoding)


def atomic_write(path: Union[Path, str], content: str, encoding: str = "utf-8") -> None:
    """Atomically write `content` to `path`.

    Streams to a sibling tempfile, fsyncs, then os.replace swaps it over the
    target. On POSIX, os.replace is atomic — readers see either old or new,
    never partial.

    Caller MUST hold vault_lock when mutating shared vault files
    (or just use vault_write() instead).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding=encoding) as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextmanager
def locked_atomic_write(
    path: Union[Path, str],
    vault_root: Optional[Path] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Iterator[Path]:
    """Combined lock + atomic write context.

    Usage:
        with locked_atomic_write(path) as target:
            new_content = transform(target.read_text())
        # `new_content` written atomically below — but we don't have it here.

    Prefer `with vault_lock(): atomic_write(path, content)` for clarity.
    """
    target = Path(path)
    with vault_lock(vault_root=vault_root, timeout=timeout):
        yield target
