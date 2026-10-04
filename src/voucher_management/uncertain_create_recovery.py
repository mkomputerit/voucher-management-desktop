"""Durable recovery for a voucher create whose POST outcome is uncertain.

The intent marker is written before the non-idempotent POST and deliberately
contains no voucher codes, recipient text, API credentials or controller
address. Recipient matching uses an HMAC supplied by HistoryService. Recovery
never associates candidates automatically: an exact candidate set is only
presented to the operator for explicit confirmation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4

from .database import Database
from .unifi_api import ApiVoucher


FORMAT_VERSION = 1


class UncertainCreateRecoveryError(RuntimeError):
    """Raised when an uncertain-create marker cannot be trusted."""


@dataclass(frozen=True)
class PendingCreateIntent:
    controller_id: int
    site_id: str
    requested_at: str
    baseline_ids: tuple[str, ...]
    quantity: int
    recipient_digest: str
    duration_minutes: int
    quota: int
    data_mb: int | None
    down_kbps: int | None
    up_kbps: int | None
    is_nominal: bool


@dataclass(frozen=True)
class CreateRecoveryMatch:
    """Candidate set discovered after a later authoritative synchronization."""

    status: str
    compatible_ids: tuple[str, ...]
    new_ids: tuple[str, ...]

    @property
    def exact(self) -> bool:
        return self.status == "exact"


def _optional_nonnegative_int(value, *, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise UncertainCreateRecoveryError(
            f"Marker creazione incerta: {field} non valido"
        )
    return value


def load_pending_create_intent(path: Path) -> PendingCreateIntent | None:
    marker = Path(path)
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UncertainCreateRecoveryError(
            "Marker della creazione incerta non leggibile"
        ) from exc
    if not isinstance(payload, dict) or payload.get("format") != FORMAT_VERSION:
        raise UncertainCreateRecoveryError(
            "Marker della creazione incerta non supportato"
        )

    controller_id = payload.get("controller_id")
    site_id = str(payload.get("site_id") or "").strip()
    requested_at = str(payload.get("requested_at") or "").strip()
    baseline = payload.get("baseline_ids")
    quantity = payload.get("quantity")
    recipient_digest = str(payload.get("recipient_digest") or "").strip().lower()
    duration_minutes = payload.get("duration_minutes")
    quota = payload.get("quota")
    is_nominal = payload.get("is_nominal")

    if (
        type(controller_id) is not int
        or controller_id < 1
        or not site_id
        or not requested_at
        or not isinstance(baseline, list)
        or any(not isinstance(value, str) or not value.strip() for value in baseline)
        or type(quantity) is not int
        or quantity < 1
        or not recipient_digest
        or len(recipient_digest) != 64
        or any(ch not in "0123456789abcdef" for ch in recipient_digest)
        or type(duration_minutes) is not int
        or duration_minutes < 1
        or type(quota) is not int
        or quota < 0
        or type(is_nominal) is not bool
    ):
        raise UncertainCreateRecoveryError(
            "Marker della creazione incerta incompleto"
        )

    baseline_ids = tuple(dict.fromkeys(value.strip() for value in baseline))
    if len(baseline_ids) != len(baseline):
        raise UncertainCreateRecoveryError(
            "Marker della creazione incerta con UUID duplicati"
        )

    return PendingCreateIntent(
        controller_id=controller_id,
        site_id=site_id,
        requested_at=requested_at,
        baseline_ids=baseline_ids,
        quantity=quantity,
        recipient_digest=recipient_digest,
        duration_minutes=duration_minutes,
        quota=quota,
        data_mb=_optional_nonnegative_int(payload.get("data_mb"), field="data_mb"),
        down_kbps=_optional_nonnegative_int(
            payload.get("down_kbps"),
            field="down_kbps",
        ),
        up_kbps=_optional_nonnegative_int(
            payload.get("up_kbps"),
            field="up_kbps",
        ),
        is_nominal=is_nominal,
    )


def write_pending_create_intent(
    path: Path,
    *,
    controller_id: int,
    site_id: str,
    requested_at: str,
    baseline_ids: Sequence[str],
    quantity: int,
    recipient_digest: str,
    duration_minutes: int,
    quota: int,
    data_mb: int | None,
    down_kbps: int | None,
    up_kbps: int | None,
    is_nominal: bool,
) -> None:
    """Write the privacy-safe create intent before entering the POST."""

    marker = Path(path)
    ids = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in baseline_ids
            if str(value).strip()
        )
    )
    digest = str(recipient_digest or "").strip().lower()
    site = str(site_id or "").strip()
    stamp = str(requested_at or "").strip()
    if (
        int(controller_id) < 1
        or not site
        or not stamp
        or int(quantity) < 1
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
        or int(duration_minutes) < 1
        or int(quota) < 0
    ):
        raise ValueError("invalid uncertain create recovery intent")

    payload = {
        "format": FORMAT_VERSION,
        "controller_id": int(controller_id),
        "site_id": site,
        "requested_at": stamp,
        "baseline_ids": list(ids),
        "quantity": int(quantity),
        "recipient_digest": digest,
        "duration_minutes": int(duration_minutes),
        "quota": int(quota),
        "data_mb": None if data_mb is None else int(data_mb),
        "down_kbps": None if down_kbps is None else int(down_kbps),
        "up_kbps": None if up_kbps is None else int(up_kbps),
        "is_nominal": bool(is_nominal),
    }

    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        raise UncertainCreateRecoveryError(
            "Esiste già una creazione incerta da riconciliare"
        )
    temp = marker.with_name(marker.name + ".tmp")
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


def clear_pending_create_intent(path: Path) -> None:
    Path(path).unlink(missing_ok=True)


def match_pending_create_intent(
    pending: PendingCreateIntent,
    vouchers: Sequence[ApiVoucher],
    *,
    recipient_digest: Callable[[str], str],
) -> CreateRecoveryMatch:
    """Find an exact compatible set without ever claiming ownership of it."""

    baseline = set(pending.baseline_ids)
    new_vouchers = [
        voucher
        for voucher in vouchers
        if str(voucher.id).strip()
        and str(voucher.id).strip() not in baseline
    ]

    compatible: list[str] = []
    for voucher in new_vouchers:
        if int(voucher.duration_minutes) != pending.duration_minutes:
            continue
        if int(voucher.quota) != pending.quota:
            continue
        if voucher.data_mb != pending.data_mb:
            continue
        if voucher.down_kbps != pending.down_kbps:
            continue
        if voucher.up_kbps != pending.up_kbps:
            continue
        if recipient_digest(str(voucher.recipient or "")) != pending.recipient_digest:
            continue
        compatible.append(str(voucher.id).strip())

    compatible_ids = tuple(dict.fromkeys(compatible))
    new_ids = tuple(
        dict.fromkeys(str(voucher.id).strip() for voucher in new_vouchers)
    )
    if len(compatible_ids) == pending.quantity:
        status = "exact"
    elif len(compatible_ids) > pending.quantity:
        status = "ambiguous"
    else:
        status = "incomplete"
    return CreateRecoveryMatch(
        status=status,
        compatible_ids=compatible_ids,
        new_ids=new_ids,
    )


def reject_pending_create_intent_to_path(
    database_path: Path,
    marker_path: Path,
    *,
    controller_id: int,
    candidate_ids: Sequence[str],
    rejected_at: str,
    windows_user: str,
) -> tuple[int, ...]:
    """Record an operator decision not to associate candidates, then unblock."""

    pending = load_pending_create_intent(marker_path)
    if pending is None:
        raise UncertainCreateRecoveryError(
            "Nessuna creazione incerta da chiudere"
        )
    if pending.controller_id != int(controller_id):
        raise UncertainCreateRecoveryError(
            "La creazione incerta appartiene a un'altra controller"
        )

    ids = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in candidate_ids
            if str(value).strip()
        )
    )
    stamp = str(rejected_at or "").strip()
    operator = str(windows_user or "").strip()
    if not stamp or not operator:
        raise ValueError("rejected_at and windows_user are required")

    database = Database(Path(database_path))
    try:
        database.initialize()
        rows = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = database.connection.execute(
                f"""SELECT id, unifi_id
                    FROM vouchers
                    WHERE controller_id=?
                      AND unifi_id IN ({placeholders})
                      AND archived_at IS NULL
                    ORDER BY id""",
                (int(controller_id), *ids),
            ).fetchall()
            if len(rows) != len(ids):
                raise UncertainCreateRecoveryError(
                    "Uno o più voucher candidati non sono nello snapshot locale"
                )

        with database.transaction() as tx:
            for row in rows:
                tx.execute(
                    """INSERT INTO voucher_events(
                           event_uuid, voucher_id, event_type, occurred_at,
                           source, windows_user, details_json
                       ) VALUES (?, ?, 'UNCERTAIN_CREATE_ASSOCIATION_REJECTED', ?, 'OPERATOR', ?, ?)""",
                    (
                        str(uuid4()),
                        int(row["id"]),
                        stamp,
                        operator,
                        Database.encode_event_details(
                            {
                                "requested_at": pending.requested_at,
                                "workflow": "operator_rejected_uncertain_create",
                            }
                        ),
                    ),
                )
        clear_pending_create_intent(marker_path)
        return tuple(int(row["id"]) for row in rows)
    finally:
        database.close()


def confirm_pending_create_intent_to_path(
    database_path: Path,
    marker_path: Path,
    *,
    controller_id: int,
    candidate_ids: Sequence[str],
    confirmed_at: str,
    windows_user: str,
) -> tuple[int, ...]:
    """Apply an operator-confirmed association and clear its intent marker."""

    pending = load_pending_create_intent(marker_path)
    if pending is None:
        raise UncertainCreateRecoveryError(
            "Nessuna creazione incerta da confermare"
        )
    if pending.controller_id != int(controller_id):
        raise UncertainCreateRecoveryError(
            "La creazione incerta appartiene a un'altra controller"
        )

    ids = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in candidate_ids
            if str(value).strip()
        )
    )
    if len(ids) != pending.quantity:
        raise UncertainCreateRecoveryError(
            "Il numero di voucher da associare non corrisponde alla richiesta"
        )
    stamp = str(confirmed_at or "").strip()
    operator = str(windows_user or "").strip()
    if not stamp or not operator:
        raise ValueError("confirmed_at and windows_user are required")

    database = Database(Path(database_path))
    try:
        database.initialize()
        placeholders = ",".join("?" for _ in ids)
        with database.transaction() as tx:
            rows = tx.execute(
                f"""SELECT id, unifi_id
                    FROM vouchers
                    WHERE controller_id=?
                      AND unifi_id IN ({placeholders})
                      AND archived_at IS NULL
                    ORDER BY id""",
                (int(controller_id), *ids),
            ).fetchall()
            if len(rows) != len(ids):
                raise UncertainCreateRecoveryError(
                    "Uno o più voucher candidati non sono nello snapshot locale"
                )

            database.mark_application_created_vouchers(
                controller_id=int(controller_id),
                unifi_ids=ids,
                is_nominal=pending.is_nominal,
                aligned_at=stamp,
                connection=tx,
            )
            for row in rows:
                tx.execute(
                    """INSERT INTO voucher_events(
                           event_uuid, voucher_id, event_type, occurred_at,
                           source, windows_user, details_json
                       ) VALUES (?, ?, 'UNCERTAIN_CREATE_ASSOCIATED', ?, 'OPERATOR', ?, ?)""",
                    (
                        str(uuid4()),
                        int(row["id"]),
                        stamp,
                        operator,
                        Database.encode_event_details(
                            {
                                "requested_at": pending.requested_at,
                                "workflow": "operator_confirmed_uncertain_create",
                            }
                        ),
                    ),
                )
        clear_pending_create_intent(marker_path)
        return tuple(int(row["id"]) for row in rows)
    finally:
        database.close()


__all__ = [
    "CreateRecoveryMatch",
    "PendingCreateIntent",
    "UncertainCreateRecoveryError",
    "clear_pending_create_intent",
    "confirm_pending_create_intent_to_path",
    "load_pending_create_intent",
    "match_pending_create_intent",
    "reject_pending_create_intent_to_path",
    "write_pending_create_intent",
]
