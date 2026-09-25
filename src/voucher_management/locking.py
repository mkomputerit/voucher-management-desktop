"""Small cross-process filesystem lock used for append-only audit writes.

The lock deliberately avoids deleting a path merely because its timestamp looks
old. That former stale-file recovery had a TOCTOU window: another process could
replace the file between stat() and unlink(). Version 5.0 therefore uses an OS
native advisory lock. Windows uses a one-byte range; POSIX uses a whole-file
flock. The operating system releases that lock when a process exits, so crash recovery does not require stale-file deletion.
"""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    """Raised when a filesystem lock cannot be acquired within the deadline."""


def _try_lock(fd: int) -> bool:
    """Try to acquire one byte without blocking on Windows or POSIX."""

    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            # Retry only genuine contention; surface unrelated I/O failures.
            if exc.errno in {errno.EACCES, errno.EDEADLK} or getattr(exc, 'winerror', None) == 33:
                return False
            raise

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(fd: int) -> None:
    """Release the native advisory lock held by fd."""

    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(
    path: Path,
    timeout: float = 8.0,
    stale_after: float = 120.0,
):
    """Acquire a crash-safe cross-process advisory lock.

    stale_after remains in the public signature for source compatibility with
    4.x callers, but is intentionally ignored. Crash recovery is owned by the
    operating system rather than inferred from a mutable file timestamp.

    The lock file itself is persistent and contains no sensitive application
    data. Keeping it in place is intentional: unlinking an advisory-lock file
    can create two independently lockable inodes and reintroduce the race this
    implementation is designed to remove.
    """

    del stale_after
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout

    # Ensure at least one byte exists because Windows byte-range locking cannot
    # lock beyond an empty file. O_APPEND avoids truncating a file another
    # process may already have open.
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"1")
            try:
                os.fsync(fd)
            except OSError:
                # Correctness does not depend on durable sentinel persistence.
                pass

        while not locked:
            locked = _try_lock(fd)
            if locked:
                break
            if time.monotonic() >= deadline:
                raise LockTimeout(f"Timeout acquisizione lock: {path}")
            time.sleep(0.12)

        yield
    finally:
        try:
            if locked:
                _unlock(fd)
        finally:
            os.close(fd)
