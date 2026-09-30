"""Durable guard for uncertain controller-side voucher creation.

The marker intentionally contains no voucher data, API credentials, controller
addresses, or operator-entered fields. Its existence alone means that a create
request may have crossed the network boundary without a definitive response,
so another create is blocked until the operator performs a successful refresh.
"""

from __future__ import annotations

import json
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

    def store_reporting_recovery(
        self,
        *,
        controller_id: int,
        voucher_ids: list[str] | tuple[str, ...],
        is_nominal: bool,
        confirmed_at: str,
    ) -> None:
        """Upgrade the guard with privacy-safe confirmed-create recovery data.

        The guard already exists before the network mutation. Reusing that
        durable file gives confirmed creations a second recovery channel when
        the dedicated reporting marker cannot be created. No voucher code,
        recipient, API credential or controller address is stored.
        """

        ids = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in voucher_ids
                if str(value).strip()
            )
        )
        stamp = str(confirmed_at or "").strip()
        if int(controller_id) < 1 or not ids or not stamp:
            raise ValueError("invalid confirmed-create recovery data")
        if not self.path.is_file():
            raise CreateMutationGuardError(
                "Il blocco anti-ripetizione non è più disponibile"
            )

        payload = {
            "format": 1,
            "controller_id": int(controller_id),
            "voucher_ids": list(ids),
            "is_nominal": bool(is_nominal),
            "confirmed_at": stamp,
        }
        temp = self.path.with_name(self.path.name + ".reporting.tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise CreateMutationGuardError(
                "Impossibile salvare il recovery della creazione confermata"
            ) from exc

    @property
    def has_reporting_recovery(self) -> bool:
        """Return whether this guard contains confirmed reporting metadata."""

        if not self.path.is_file():
            return False
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CreateMutationGuardError(
                "Impossibile leggere il blocco anti-ripetizione"
            ) from exc
        if raw.strip() == "pending":
            return False
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CreateMutationGuardError(
                "Recovery della creazione confermata danneggiato"
            ) from exc
        return (
            isinstance(payload, dict)
            and payload.get("format") == 1
            and type(payload.get("controller_id")) is int
            and isinstance(payload.get("voucher_ids"), list)
            and bool(payload.get("voucher_ids"))
            and type(payload.get("is_nominal")) is bool
            and bool(str(payload.get("confirmed_at") or "").strip())
        )

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
