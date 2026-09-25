"""Per-user single-instance ownership for the 5.0 transition.

Milestone A deliberately keeps application data under LocalAppData.  The guard
therefore scopes ownership to that per-user data root; Milestone C will replace
this with a machine-wide Windows-session guard when the database itself moves to
ProgramData.
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
                "Voucher Management è già in esecuzione per questo utente Windows."
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
