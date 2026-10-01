"""Durable reconciliation for confirmed voucher creation reporting facts.

The controller can confirm creation before SQLite classification is durable.
This module keeps only privacy-safe reconciliation data: local controller id,
confirmed UniFi voucher UUIDs, nominality and the confirmation timestamp.
Voucher codes, recipients and credentials are deliberately excluded.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .database import Database


FORMAT_VERSION = 1


class CreateReportingRecoveryError(RuntimeError):
    """Raised when the durable reporting-reconciliation marker is invalid."""


@dataclass(frozen=True)
class PendingCreateReporting:
    controller_id: int
    voucher_ids: tuple[str, ...]
    is_nominal: bool
    confirmed_at: str


def marker_path_for_database(database_path: Path) -> Path:
    return Path(database_path).with_name("pending_create_reporting.json")


def load_pending_create_reporting(path: Path) -> PendingCreateReporting | None:
    marker = Path(path)
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CreateReportingRecoveryError(
            "Marker di riconciliazione creazione non leggibile"
        ) from exc
    if not isinstance(payload, dict) or payload.get("format") != FORMAT_VERSION:
        raise CreateReportingRecoveryError(
            "Marker di riconciliazione creazione non supportato"
        )
    controller_id = payload.get("controller_id")
    voucher_ids = payload.get("voucher_ids")
    is_nominal = payload.get("is_nominal")
    confirmed_at = str(payload.get("confirmed_at") or "").strip()
    if (
        type(controller_id) is not int
        or controller_id < 1
        or not isinstance(voucher_ids, list)
        or not voucher_ids
        or any(not isinstance(value, str) or not value.strip() for value in voucher_ids)
        or type(is_nominal) is not bool
        or not confirmed_at
    ):
        raise CreateReportingRecoveryError(
            "Marker di riconciliazione creazione incompleto"
        )
    normalized = tuple(dict.fromkeys(value.strip() for value in voucher_ids))
    if len(normalized) != len(voucher_ids):
        raise CreateReportingRecoveryError(
            "Marker di riconciliazione creazione duplicato"
        )
    return PendingCreateReporting(
        controller_id=controller_id,
        voucher_ids=normalized,
        is_nominal=is_nominal,
        confirmed_at=confirmed_at,
    )


def write_pending_create_reporting(
    path: Path,
    *,
    controller_id: int,
    voucher_ids: list[str] | tuple[str, ...],
    is_nominal: bool,
    confirmed_at: str,
) -> None:
    """Atomically persist confirmed UUID/classification before SQLite writes."""

    marker = Path(path)
    ids = tuple(dict.fromkeys(str(value).strip() for value in voucher_ids if str(value).strip()))
    if int(controller_id) < 1 or not ids or not str(confirmed_at).strip():
        raise ValueError("invalid create reporting reconciliation data")
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        raise CreateReportingRecoveryError(
            "Esiste già una creazione da riconciliare"
        )
    temp = marker.with_name(marker.name + ".tmp")
    payload = {
        "format": FORMAT_VERSION,
        "controller_id": int(controller_id),
        "voucher_ids": list(ids),
        "is_nominal": bool(is_nominal),
        "confirmed_at": str(confirmed_at).strip(),
    }
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, marker)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def clear_pending_create_reporting(path: Path) -> None:
    Path(path).unlink(missing_ok=True)


def reconcile_pending_create_reporting(
    database: Database,
    path: Path,
    *,
    controller_id: int | None = None,
) -> bool:
    """Apply one durable classification marker idempotently when rows exist."""

    pending = load_pending_create_reporting(path)
    if pending is None:
        return False
    if controller_id is not None and pending.controller_id != int(controller_id):
        return False

    placeholders = ",".join("?" for _ in pending.voucher_ids)
    count = database.connection.execute(
        f"""SELECT COUNT(*)
            FROM vouchers
            WHERE controller_id=?
              AND unifi_id IN ({placeholders})""",
        (pending.controller_id, *pending.voucher_ids),
    ).fetchone()[0]
    if int(count) != len(pending.voucher_ids):
        return False

    database.mark_application_created_vouchers(
        controller_id=pending.controller_id,
        unifi_ids=pending.voucher_ids,
        is_nominal=pending.is_nominal,
        aligned_at=pending.confirmed_at,
    )
    clear_pending_create_reporting(path)
    return True


def reconcile_pending_create_reporting_to_path(
    database_path: Path,
    marker_path: Path,
    *,
    controller_id: int | None = None,
) -> bool:
    """Reconcile using a short worker-owned SQLite connection."""

    database = Database(Path(database_path))
    try:
        database.initialize()
        return reconcile_pending_create_reporting(
            database,
            marker_path,
            controller_id=controller_id,
        )
    finally:
        database.close()
