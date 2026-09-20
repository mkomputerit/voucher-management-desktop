"""Small filesystem lock used to serialize append-only audit writes."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    """Raised when a filesystem lock cannot be acquired within the deadline."""


@contextmanager
def exclusive_file_lock(
    path: Path,
    timeout: float = 8.0,
    stale_after: float = 120.0,
):
    """Acquire a small cross-process lock using exclusive file creation.

    History writes are intentionally short. A lock older than stale_after is
    therefore treated as debris from a crashed process. The descriptor is
    closed on every error path so a failed lock-file write cannot leak a Windows
    handle and keep the lock path artificially busy.
    """
    deadline = time.monotonic() + timeout
    fd: int | None = None

    while fd is None:
        candidate_fd: int | None = None
        try:
            candidate_fd = os.open(
                str(path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(
                candidate_fd,
                (
                    f"pid={os.getpid()} time={time.time()}"
                ).encode("ascii", errors="ignore"),
            )
            fd = candidate_fd
            candidate_fd = None
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
                if age > stale_after:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue

            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"Timeout acquisizione lock: {path}"
                )
            time.sleep(0.12)
        finally:
            if candidate_fd is not None:
                # The exclusive create already materialised the lock path.
                # If metadata writing failed before ownership was accepted,
                # remove that partial lock immediately rather than waiting for
                # stale-lock recovery.
                try:
                    os.close(candidate_fd)
                finally:
                    path.unlink(missing_ok=True)

    try:
        yield
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            path.unlink(missing_ok=True)
