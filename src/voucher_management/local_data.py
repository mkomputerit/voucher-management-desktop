"""Operator-owned local metadata for controller vouchers.

This module is deliberately narrow: it can update only fields that belong to
Voucher Management.  Controller-origin facts such as code, UniFi name,
duration, expiry, counters and provenance are never accepted as inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .database import Database


@dataclass(frozen=True)
class LocalVoucherPatch:
    apply_notes: bool = False
    notes: str = ""
    apply_is_nominal: bool = False
    is_nominal: bool | None = None

    def validated(self) -> "LocalVoucherPatch":
        if not (
            self.apply_notes
            or self.apply_is_nominal
        ):
            raise ValueError("Selezionare almeno un dato locale da aggiornare")

        notes = str(self.notes or "").strip()
        if len(notes) > 1000:
            raise ValueError("Le note locali non possono superare 1000 caratteri")
        if self.apply_is_nominal and type(self.is_nominal) is not bool:
            raise ValueError("La nominalità deve essere Nominale o Non nominale")

        return LocalVoucherPatch(
            apply_notes=bool(self.apply_notes),
            notes=notes,
            apply_is_nominal=bool(self.apply_is_nominal),
            is_nominal=self.is_nominal,
        )


@dataclass(frozen=True)
class LocalDataUpdateResult:
    updated_ids: tuple[int, ...]
    unchanged_ids: tuple[int, ...]


def apply_local_voucher_patch(
    database: Database,
    *,
    controller_id: int,
    voucher_ids: list[int] | tuple[int, ...],
    patch: LocalVoucherPatch,
    updated_at: str,
    windows_user: str,
) -> LocalDataUpdateResult:
    """Atomically apply operator-owned metadata to all requested vouchers.

    Missing/mismatched rows abort the whole batch.  Audit events record only
    which local fields changed, never the previous/new personal text values.
    """

    clean = patch.validated()
    ids = tuple(dict.fromkeys(int(value) for value in voucher_ids))
    if not ids:
        raise ValueError("Selezionare almeno un voucher")
    stamp = str(updated_at or "").strip()
    operator = str(windows_user or "").strip()
    if not stamp:
        raise ValueError("updated_at is required")
    if not operator:
        raise ValueError("windows_user is required")

    placeholders = ",".join("?" for _ in ids)
    with database.transaction() as db:
        rows = db.execute(
            f"""SELECT id, notes, is_nominal, nominality_redacted
                FROM vouchers
                WHERE controller_id=?
                  AND id IN ({placeholders})
                  AND archived_at IS NULL
                ORDER BY id""",
            (int(controller_id), *ids),
        ).fetchall()
        if len(rows) != len(ids):
            raise RuntimeError(
                "Uno o più voucher selezionati non appartengono alla controller attiva"
            )

        updated: list[int] = []
        unchanged: list[int] = []
        for row in rows:
            voucher_id = int(row["id"])
            changes: list[str] = []
            assignments: list[str] = []
            params: list[object] = []

            if clean.apply_notes:
                current = str(row["notes"] or "")
                if current != clean.notes:
                    assignments.append("notes=?")
                    params.append(clean.notes)
                    changes.append("notes")

            if clean.apply_is_nominal:
                current_raw = row["is_nominal"]
                current = None if current_raw is None else bool(current_raw)
                redacted = bool(row["nominality_redacted"])
                if current != clean.is_nominal or redacted:
                    assignments.append("is_nominal=?")
                    params.append(int(clean.is_nominal))
                    assignments.append("nominality_redacted=0")
                    changes.append("is_nominal")

            if not changes:
                unchanged.append(voucher_id)
                continue

            params.append(voucher_id)
            db.execute(
                f"""UPDATE vouchers
                    SET {", ".join(assignments)}
                    WHERE id=?""",
                tuple(params),
            )
            db.execute(
                """INSERT INTO voucher_events(
                       event_uuid, voucher_id, event_type, occurred_at,
                       source, windows_user, details_json
                   ) VALUES (?, ?, 'LOCAL_METADATA_UPDATED', ?, 'OPERATOR', ?, ?)""",
                (
                    str(uuid4()),
                    voucher_id,
                    stamp,
                    operator,
                    Database.encode_event_details(
                        {"fields": sorted(set(changes))}
                    ),
                ),
            )
            updated.append(voucher_id)

    return LocalDataUpdateResult(
        updated_ids=tuple(updated),
        unchanged_ids=tuple(unchanged),
    )


def apply_local_voucher_patch_to_path(
    database_path: Path,
    *,
    controller_id: int,
    voucher_ids: list[int] | tuple[int, ...],
    patch: LocalVoucherPatch,
    updated_at: str,
    windows_user: str,
) -> LocalDataUpdateResult:
    """Worker-owned SQLite wrapper for the Tk UI."""

    database = Database(Path(database_path))
    try:
        database.initialize()
        return apply_local_voucher_patch(
            database,
            controller_id=controller_id,
            voucher_ids=voucher_ids,
            patch=patch,
            updated_at=updated_at,
            windows_user=windows_user,
        )
    finally:
        database.close()
