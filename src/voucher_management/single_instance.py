"""OS-backed single-instance ownership tied to the active application data root.

Portable mode keeps the lock under LocalAppData and is therefore per-user.
Installed shared mode puts the same lock under ProgramData, making ownership
common to every Windows session that is authorized to access the shared tree.
The filesystem lock is released by the operating system on process exit.
"""

from __future__ import annotations

from pathlib import Path

from .locking import LockTimeout, exclusive_file_lock


class InstanceAlreadyRunning(RuntimeError):
    """Raised when another process already owns this user's application guard."""


class SingleInstanceGuard:
    """Hold one OS-backed lock for the complete application lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._context = None

    def acquire(self) -> None:
        """Acquire immediately; never make a second launch wait several seconds."""

        if self._context is not None:
            return
        context = exclusive_file_lock(self.path, timeout=0.0)
        try:
            context.__enter__()
        except LockTimeout as exc:
            raise InstanceAlreadyRunning(
                "Voucher Management è già in esecuzione in un'altra sessione Windows."
            ) from exc
        self._context = context

    def release(self) -> None:
        """Release ownership; safe to call repeatedly during shutdown cleanup."""

        context, self._context = self._context, None
        if context is not None:
            context.__exit__(None, None, None)

    def __enter__(self) -> "SingleInstanceGuard":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
