"""Durable guard for uncertain controller-side voucher creation.

The marker intentionally contains no voucher data, API credentials, controller
addresses, or operator-entered fields. Its existence alone means that a create
request may have crossed the network boundary without a definitive response,
so another create is blocked until the operator performs a successful refresh.
"""

from __future__ import annotations

import os
from pathlib import Path


class CreateMutationGuardError(RuntimeError):
    """Raised when the durable create guard cannot be changed safely."""


class CreateMutationGuard:
    """Atomic existence marker preventing accidental duplicate creates."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def pending(self) -> bool:
        """Return whether a create attempt still requires reconciliation."""

        return self.path.is_file()

    def begin(self) -> None:
        """Create the marker atomically before entering the network mutation."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise CreateMutationGuardError(
                "Una precedente creazione richiede ancora una sincronizzazione"
            ) from exc
        except OSError as exc:
            raise CreateMutationGuardError(
                "Impossibile proteggere la creazione da una ripetizione"
            ) from exc

        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write("pending\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # Keep any marker that was created: a false-positive reconciliation
            # requirement is safer than allowing a duplicate controller POST.
            raise CreateMutationGuardError(
                "Impossibile confermare il blocco anti-ripetizione"
            ) from exc

    def clear(self) -> bool:
        """Clear the marker after a definitive result or successful refresh."""

        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CreateMutationGuardError(
                "Impossibile sbloccare la creazione dopo la sincronizzazione"
            ) from exc
