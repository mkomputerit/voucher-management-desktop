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
    """Atomic marker preventing unsafe follow-up creates."""

    PENDING_STATE = "pending"
    MANUAL_RECOVERY_STATE = "confirmed-unreconciled"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._manual_recovery_in_memory = False

    @property
    def pending(self) -> bool:
        """Return whether a create attempt still requires reconciliation."""

        return self.path.is_file()

    @property
    def state(self) -> str:
        """Return the durable guard state, failing closed on unreadable content."""

        if not self.path.is_file():
            return ""
        try:
            value = self.path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return "unknown"
        return value if value in {
            self.PENDING_STATE,
            self.MANUAL_RECOVERY_STATE,
        } else "unknown"

    @property
    def requires_manual_recovery(self) -> bool:
        """Return whether automatic refresh is insufficient to unlock create."""

        return self._manual_recovery_in_memory or self.state in {
            self.MANUAL_RECOVERY_STATE,
            "unknown",
        }

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
                handle.write(self.PENDING_STATE + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # Keep any marker that was created: a false-positive reconciliation
            # requirement is safer than allowing a duplicate controller POST.
            raise CreateMutationGuardError(
                "Impossibile confermare il blocco anti-ripetizione"
            ) from exc

    def mark_confirmed_unreconciled(self) -> None:
        """Escalate a confirmed create to manual recovery without losing guard.

        This state contains no voucher codes, recipient text, API credentials
        or controller address. It records only that automatic attribution for a
        controller-confirmed create is no longer recoverable and therefore a
        later refresh must not silently unlock further creates.
        """

        self._manual_recovery_in_memory = True
        if not self.path.is_file():
            raise CreateMutationGuardError(
                "Blocco creazione mancante durante la riconciliazione"
            )
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temp.open("w", encoding="ascii", newline="\n") as handle:
                handle.write(self.MANUAL_RECOVERY_STATE + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise CreateMutationGuardError(
                "Impossibile rendere permanente il blocco di riconciliazione"
            ) from exc

    def clear(self) -> bool:
        """Clear the marker after a definitive result or successful refresh."""

        try:
            self.path.unlink()
            self._manual_recovery_in_memory = False
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CreateMutationGuardError(
                "Impossibile sbloccare la creazione dopo la sincronizzazione"
            ) from exc
