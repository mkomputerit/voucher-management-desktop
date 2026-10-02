"""Operator-driven alignment of controller/legacy vouchers.

Alignment fills only facts Voucher Management cannot infer safely from UniFi:
nominality and historical print state.  Controller-owned fields remain read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .database import (
    Database,
    PRINT_STATE_NOT_PRINTED,
    PRINT_STATE_PRINTED,
    PRINT_STATE_UNKNOWN,
    PRINT_STATES,
)


@dataclass(frozen=True)
class VoucherAlignmentCandidate:
    voucher_id: int
    controller_id: int
    unifi_id: str
    name: str
    created_at: str
    origin: str
    is_nominal: bool | None
    print_state: str
    last_printed_at: str


@dataclass(frozen=True)
class VoucherAlignmentResult:
    updated_ids: tuple[int, ...]
    unchanged_ids: tuple[int, ...]


def alignment_candidates(
    database: Database,
    *,
    controller_id: int,
    present_only: bool = True,
) -> tuple[VoucherAlignmentCandidate, ...]:
    """Return vouchers whose operator alignment is not yet complete."""

    present_clause = "AND v.present_on_controller=1" if present_only else ""
    rows = database.connection.execute(
        f"""SELECT
                v.id,
                v.controller_id,
                v.unifi_id,
                v.name,
                v.created_at,
                v.origin,
                v.is_nominal,
                v.print_state,
                COALESCE(MAX(vp.printed_at), '') AS last_printed_at
            FROM vouchers AS v
            LEFT JOIN voucher_prints AS vp ON vp.voucher_id=v.id
            WHERE v.controller_id=?
              AND v.archived_at IS NULL
              AND v.alignment_completed_at IS NULL
              {present_clause}
            GROUP BY v.id
            ORDER BY COALESCE(v.created_at, v.imported_at), v.id""",
        (int(controller_id),),
    ).fetchall()

    return tuple(
        VoucherAlignmentCandidate(
            voucher_id=int(row["id"]),
            controller_id=int(row["controller_id"]),
            unifi_id=str(row["unifi_id"]),
            name=str(row["name"] or "").strip(),
            created_at=str(row["created_at"] or ""),
            origin=str(row["origin"] or "UNKNOWN"),
            is_nominal=(
                None if row["is_nominal"] is None else bool(row["is_nominal"])
            ),
            print_state=str(row["print_state"] or PRINT_STATE_UNKNOWN),
            last_printed_at=str(row["last_printed_at"] or ""),
        )
        for row in rows
    )


def align_vouchers(
    database: Database,
    *,
    controller_id: int,
    voucher_ids: list[int] | tuple[int, ...],
    is_nominal: bool,
    print_state: str,
    aligned_at: str,
    windows_user: str,
) -> VoucherAlignmentResult:
    """Atomically complete operator alignment for one or more vouchers."""

    if type(is_nominal) is not bool:
        raise ValueError("La nominalità deve essere Nominale o Non nominale")

    normalized_print_state = str(print_state or "").strip().upper()
    if normalized_print_state not in PRINT_STATES:
        raise ValueError("Stato stampa non valido")

    ids = tuple(dict.fromkeys(int(value) for value in voucher_ids))
    if not ids:
        raise ValueError("Selezionare almeno un voucher")

    stamp = str(aligned_at or "").strip()
    operator = str(windows_user or "").strip()
    if not stamp:
        raise ValueError("aligned_at is required")
    if not operator:
        raise ValueError("windows_user is required")

    placeholders = ",".join("?" for _ in ids)
    with database.transaction() as db:
        rows = db.execute(
            f"""SELECT
                    v.id,
                    v.name,
                    v.is_nominal,
                    v.print_state,
                    v.origin,
                    v.alignment_completed_at,
                    EXISTS(
                        SELECT 1
                        FROM voucher_prints AS vp
                        WHERE vp.voucher_id=v.id
                    ) AS has_verified_print
                FROM vouchers AS v
                WHERE v.controller_id=?
                  AND v.id IN ({placeholders})
                  AND v.archived_at IS NULL
                ORDER BY v.id""",
            (int(controller_id), *ids),
        ).fetchall()
        if len(rows) != len(ids):
            raise RuntimeError(
                "Uno o più voucher selezionati non appartengono alla controller attiva"
            )

        if is_nominal and any(
            not str(row["name"] or "").strip()
            for row in rows
        ):
            raise ValueError(
                "Un voucher nominale deve avere un destinatario nella "
                "descrizione UniFi. La descrizione controller è sola lettura: "
                "classificarlo Non nominale oppure eliminarlo con motivazione."
            )

        if normalized_print_state != PRINT_STATE_PRINTED:
            verified_print_ids = [
                int(row["id"]) for row in rows if bool(row["has_verified_print"])
            ]
            if verified_print_ids:
                raise ValueError(
                    "Lo stato stampa richiesto contraddice una stampa verificata "
                    "già presente nello storico."
                )

        controller_origin_without_verified_print = [
            int(row["id"])
            for row in rows
            if str(row["origin"] or "").strip().upper() == "CONTROLLER"
            and not bool(row["has_verified_print"])
        ]
        if (
            controller_origin_without_verified_print
            and normalized_print_state != PRINT_STATE_UNKNOWN
        ):
            raise ValueError(
                "Per i voucher trovati direttamente sulla controller lo stato "
                "di stampa non può essere dedotto. Usare 'Non determinabile' "
                "finché non esiste una stampa verificata."
            )

        updated: list[int] = []
        unchanged: list[int] = []
        for row in rows:
            voucher_id = int(row["id"])
            current_nominal = (
                None if row["is_nominal"] is None else bool(row["is_nominal"])
            )
            current_print_state = str(
                row["print_state"] or PRINT_STATE_UNKNOWN
            ).upper()
            already_aligned = bool(
                str(row["alignment_completed_at"] or "").strip()
            )

            if (
                already_aligned
                and current_nominal is is_nominal
                and current_print_state == normalized_print_state
            ):
                unchanged.append(voucher_id)
                continue

            db.execute(
                """UPDATE vouchers
                   SET is_nominal=?,
                       print_state=?,
                       alignment_completed_at=?,
                       nominality_redacted=0
                   WHERE id=?""",
                (
                    int(is_nominal),
                    normalized_print_state,
                    stamp,
                    voucher_id,
                ),
            )
            db.execute(
                """INSERT INTO voucher_events(
                       event_uuid, voucher_id, event_type, occurred_at,
                       source, windows_user, details_json
                   ) VALUES (?, ?, 'VOUCHER_ALIGNED', ?, 'OPERATOR', ?, ?)""",
                (
                    str(uuid4()),
                    voucher_id,
                    stamp,
                    operator,
                    Database.encode_event_details(
                        {
                            "is_nominal": bool(is_nominal),
                            "print_state": normalized_print_state,
                        }
                    ),
                ),
            )
            updated.append(voucher_id)

    return VoucherAlignmentResult(
        updated_ids=tuple(updated),
        unchanged_ids=tuple(unchanged),
    )


def align_vouchers_to_path(
    database_path: Path,
    **kwargs,
) -> VoucherAlignmentResult:
    """Worker-owned SQLite wrapper for the Tk alignment UI."""

    database = Database(Path(database_path))
    try:
        database.initialize()
        return align_vouchers(database, **kwargs)
    finally:
        database.close()


__all__ = [
    "PRINT_STATE_UNKNOWN",
    "PRINT_STATE_NOT_PRINTED",
    "PRINT_STATE_PRINTED",
    "VoucherAlignmentCandidate",
    "VoucherAlignmentResult",
    "alignment_candidates",
    "align_vouchers",
    "align_vouchers_to_path",
]
