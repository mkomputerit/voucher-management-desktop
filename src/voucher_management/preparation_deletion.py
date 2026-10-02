"""Preparation-error deletion audit and reconciliation.

Ordinary deletion is intentionally distinct from security revocation.  It is
allowed only for vouchers positively known to be aligned and not printed.  A
mandatory operator reason is persisted before the remote mutation so an
uncertain network outcome never loses the business reason.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from uuid import uuid4

from .database import Database, PRINT_STATE_NOT_PRINTED


MAX_PREPARATION_DELETE_REASON = 1000


@dataclass(frozen=True)
class PreparationDeleteFact:
    voucher_id: int
    unifi_id: str
    print_state: str
    alignment_completed: bool
    ever_used: bool


def preparation_delete_facts(
    database: Database,
    *,
    controller_id: int,
    unifi_ids: list[str] | tuple[str, ...],
) -> dict[str, PreparationDeleteFact]:
    ids = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in unifi_ids
            if str(value).strip()
        )
    )
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = database.connection.execute(
        f"""SELECT id, unifi_id, print_state, alignment_completed_at, ever_used
            FROM vouchers
            WHERE controller_id=?
              AND unifi_id IN ({placeholders})
              AND archived_at IS NULL""",
        (int(controller_id), *ids),
    ).fetchall()
    return {
        str(row["unifi_id"]): PreparationDeleteFact(
            voucher_id=int(row["id"]),
            unifi_id=str(row["unifi_id"]),
            print_state=str(row["print_state"] or "UNKNOWN"),
            alignment_completed=bool(
                str(row["alignment_completed_at"] or "").strip()
            ),
            ever_used=bool(row["ever_used"]),
        )
        for row in rows
    }


def _normalized_reason(reason: str) -> str:
    value = str(reason or "").strip()
    if not value:
        raise ValueError("La motivazione della cancellazione è obbligatoria.")
    if len(value) > MAX_PREPARATION_DELETE_REASON:
        raise ValueError(
            "La motivazione della cancellazione non può superare "
            f"{MAX_PREPARATION_DELETE_REASON} caratteri."
        )
    return value


def _merge_details(raw: str | None, **updates) -> str:
    try:
        current = json.loads(raw) if raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.update(updates)
    return Database.encode_event_details(current) or "{}"


def record_preparation_delete_requests(
    database: Database,
    *,
    controller_id: int,
    unifi_ids: list[str] | tuple[str, ...],
    reason: str,
    requested_at: str,
    windows_user: str,
) -> tuple[int, ...]:
    """Persist one durable delete intent per voucher before remote DELETE."""

    ids = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in unifi_ids
            if str(value).strip()
        )
    )
    if not ids:
        raise ValueError("Selezionare almeno un voucher.")

    normalized_reason = _normalized_reason(reason)
    stamp = str(requested_at or "").strip()
    operator = str(windows_user or "").strip()
    if not stamp:
        raise ValueError("requested_at is required")
    if not operator:
        raise ValueError("windows_user is required")

    placeholders = ",".join("?" for _ in ids)
    with database.transaction() as db:
        rows = db.execute(
            f"""SELECT
                    v.id,
                    v.unifi_id,
                    v.print_state,
                    v.alignment_completed_at,
                    v.ever_used,
                    EXISTS(
                        SELECT 1
                        FROM voucher_prints AS vp
                        WHERE vp.voucher_id=v.id
                    ) AS has_verified_print
                FROM vouchers AS v
                WHERE v.controller_id=?
                  AND v.unifi_id IN ({placeholders})
                  AND v.archived_at IS NULL
                ORDER BY v.id""",
            (int(controller_id), *ids),
        ).fetchall()
        if len(rows) != len(ids):
            raise RuntimeError(
                "Uno o più voucher selezionati non sono presenti nello storico locale."
            )

        voucher_ids: list[int] = []
        for row in rows:
            voucher_id = int(row["id"])
            voucher_ids.append(voucher_id)
            if bool(row["ever_used"]):
                raise RuntimeError(
                    "Un voucher già utilizzato non può essere cancellato come errore di preparazione."
                )
            if not str(row["alignment_completed_at"] or "").strip():
                raise RuntimeError(
                    "Un voucher non ancora allineato non può essere cancellato ordinariamente."
                )
            if bool(row["has_verified_print"]):
                raise RuntimeError(
                    "Una stampa verificata nello storico blocca la cancellazione ordinaria."
                )
            if str(row["print_state"] or "UNKNOWN") != PRINT_STATE_NOT_PRINTED:
                raise RuntimeError(
                    "La cancellazione ordinaria richiede stato stampa Non stampato."
                )
            pending = db.execute(
                """SELECT 1
                   FROM voucher_events
                   WHERE voucher_id=?
                     AND event_type='PREPARATION_DELETE_REQUESTED'
                   LIMIT 1""",
                (voucher_id,),
            ).fetchone()
            if pending is not None:
                raise RuntimeError(
                    "Esiste già una cancellazione da riconciliare. Eseguire Sincronizza."
                )

        for voucher_id in voucher_ids:
            db.execute(
                """INSERT INTO voucher_events(
                       event_uuid, voucher_id, event_type, occurred_at,
                       source, windows_user, details_json
                   ) VALUES (?, ?, 'PREPARATION_DELETE_REQUESTED', ?, 'OPERATOR', ?, ?)""",
                (
                    str(uuid4()),
                    voucher_id,
                    stamp,
                    operator,
                    Database.encode_event_details(
                        {
                            "reason": normalized_reason,
                            "requested_at": stamp,
                            "workflow": "preparation_error",
                        }
                    ),
                ),
            )

    return tuple(voucher_ids)


def record_preparation_delete_requests_to_path(
    database_path: Path,
    **kwargs,
) -> tuple[int, ...]:
    database = Database(Path(database_path))
    try:
        database.initialize()
        return record_preparation_delete_requests(database, **kwargs)
    finally:
        database.close()


def reconcile_preparation_delete_requests(
    database: Database,
    *,
    controller_id: int,
    present_unifi_ids: set[str] | frozenset[str],
    confirmed_absent_ids: set[str] | frozenset[str] = frozenset(),
    observed_at: str,
    connection=None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Resolve pending requests only from positive presence or confirmed absence.

    A list omission alone is not enough to prove deletion. Presence proves the
    requested ordinary delete did not apply; absence is accepted only after the
    synchronization layer has confirmed the voucher UUID is not found directly.
    """

    present = {str(value).strip() for value in present_unifi_ids if str(value).strip()}
    confirmed_absent = {
        str(value).strip()
        for value in confirmed_absent_ids
        if str(value).strip()
    }
    stamp = str(observed_at or "").strip()
    if not stamp:
        raise ValueError("observed_at is required")

    def write(db):
        rows = db.execute(
            """SELECT ve.id AS event_id, ve.voucher_id, ve.details_json, v.unifi_id
               FROM voucher_events AS ve
               JOIN vouchers AS v ON v.id=ve.voucher_id
               WHERE v.controller_id=?
                 AND ve.event_type='PREPARATION_DELETE_REQUESTED'
               ORDER BY ve.id""",
            (int(controller_id),),
        ).fetchall()
        deleted: list[int] = []
        not_applied: list[int] = []
        for row in rows:
            voucher_id = int(row["voucher_id"])
            remote_id = str(row["unifi_id"])
            if remote_id in present:
                event_type = "PREPARATION_DELETE_NOT_APPLIED"
                source = "fresh_snapshot_present"
                not_applied.append(voucher_id)
            elif remote_id in confirmed_absent:
                event_type = "PREPARATION_DELETED"
                source = "direct_uuid_not_found"
                deleted.append(voucher_id)
            else:
                # A transient list omission keeps the durable request pending.
                continue
            db.execute(
                """UPDATE voucher_events
                   SET event_type=?,
                       occurred_at=?,
                       details_json=?
                   WHERE id=?""",
                (
                    event_type,
                    stamp,
                    _merge_details(
                        row["details_json"],
                        confirmed_at=stamp,
                        confirmation_source=source,
                    ),
                    int(row["event_id"]),
                ),
            )
        return tuple(deleted), tuple(not_applied)

    if connection is not None:
        return write(connection)
    with database.transaction() as db:
        return write(db)


__all__ = [
    "MAX_PREPARATION_DELETE_REASON",
    "PreparationDeleteFact",
    "preparation_delete_facts",
    "record_preparation_delete_requests",
    "record_preparation_delete_requests_to_path",
    "reconcile_preparation_delete_requests",
]
