"""Controller-to-SQLite synchronization for Voucher Management 5.0.

This module deliberately contains no Tk code. It converts a successful UniFi
snapshot into durable current state and change observations. Missing vouchers
are marked absent only after the caller has obtained a complete successful
snapshot; network failures must never call this function with partial data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from uuid import uuid4

from .database import Database
from .identity import LEGACY_BACKUP_API_ROOT_PREFIX
from .unifi_api import ApiVoucher


@dataclass(frozen=True)
class PersistedControllerSnapshot:
    """Controller identity plus one fully committed voucher snapshot."""

    controller_id: int
    controller_name: str
    observed_at: str


OBSERVED_FIELDS = (
    "authorized_guest_count",
    "activated_at",
    "expires_at",
    "expired",
    "present_on_controller",
)


def _iso_from_epoch(value: int) -> str | None:
    """Convert the adapter's epoch seconds to canonical UTC text."""

    if not value:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _canonical_code(value: object) -> str:
    return str(value or "").strip().replace("-", "")


def _identity_event_uuid(kind: str, target_id: int, source_ids: list[int]) -> str:
    payload = ",".join(str(value) for value in sorted(source_ids))
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()[:20]
    return f"legacy-identity-{kind}-{int(target_id)}-{digest}"


def _record_identity_review_required(
    database: Database,
    tx,
    *,
    target_id: int,
    source_ids: list[int],
    observed_at: str,
    reason: str,
) -> None:
    """Persist a non-PII marker when legacy/live identity cannot be proven."""

    tx.execute(
        """INSERT OR IGNORE INTO voucher_events
           (event_uuid, voucher_id, event_type, occurred_at, source,
            windows_user, details_json)
           VALUES (?, ?, 'LEGACY_IDENTITY_REVIEW_REQUIRED', ?, 'SYSTEM',
                   NULL, ?)""",
        (
            _identity_event_uuid("review", target_id, source_ids),
            int(target_id),
            str(observed_at),
            database.encode_event_details(
                {
                    "reason": str(reason),
                    "candidate_count": len(source_ids),
                }
            ),
        ),
    )


def _renumber_prints_chronologically(tx, voucher_id: int) -> None:
    rows = tx.execute(
        """SELECT vp.id, vp.print_sequence, vp.printed_at, pj.print_job_uuid
           FROM voucher_prints AS vp
           JOIN print_jobs AS pj ON pj.id=vp.print_job_id
           WHERE vp.voucher_id=?
           ORDER BY
               (julianday(vp.printed_at) IS NULL),
               julianday(vp.printed_at) ASC,
               pj.print_job_uuid,
               vp.id""",
        (int(voucher_id),),
    ).fetchall()
    if not rows:
        return
    maximum = max(int(row["print_sequence"]) for row in rows)
    offset = maximum + len(rows) + 1
    tx.execute(
        """UPDATE voucher_prints
           SET print_sequence=print_sequence+?
           WHERE voucher_id=?""",
        (offset, int(voucher_id)),
    )
    for sequence, row in enumerate(rows, start=1):
        tx.execute(
            """UPDATE voucher_prints
               SET print_sequence=?, is_reprint=?
               WHERE id=?""",
            (sequence, int(sequence > 1), int(row["id"])),
        )


def _merge_legacy_archive_voucher(
    database: Database,
    tx,
    *,
    target_id: int,
    source_id: int,
    observed_at: str,
) -> None:
    """Move one verified synthetic legacy identity onto its live voucher."""

    source = tx.execute(
        """SELECT id, name, assigned_to
           FROM vouchers WHERE id=?""",
        (int(source_id),),
    ).fetchone()
    if source is None:
        return

    source_prints = tx.execute(
        """SELECT id, print_job_id, printed_at, physical_copies
           FROM voucher_prints
           WHERE voucher_id=?
           ORDER BY id""",
        (int(source_id),),
    ).fetchall()
    for row in source_prints:
        duplicate = tx.execute(
            """SELECT id, printed_at, physical_copies
               FROM voucher_prints
               WHERE voucher_id=? AND print_job_id=?""",
            (int(target_id), int(row["print_job_id"])),
        ).fetchone()
        if duplicate is None:
            continue
        if (
            str(duplicate["printed_at"]) != str(row["printed_at"])
            or int(duplicate["physical_copies"]) != int(row["physical_copies"])
        ):
            raise RuntimeError(
                "Conflicting legacy/live print evidence for the same print job"
            )
        tx.execute(
            "DELETE FROM voucher_prints WHERE id=?",
            (int(row["id"]),),
        )

    remaining = tx.execute(
        """SELECT COALESCE(MAX(print_sequence), 0)
           FROM voucher_prints WHERE voucher_id=?""",
        (int(target_id),),
    ).fetchone()[0]
    if int(remaining) < 0:
        raise RuntimeError("Invalid target print sequence")
    tx.execute(
        """UPDATE voucher_prints
           SET print_sequence=print_sequence+?
           WHERE voucher_id=?""",
        (int(remaining), int(source_id)),
    )
    tx.execute(
        "UPDATE voucher_prints SET voucher_id=? WHERE voucher_id=?",
        (int(target_id), int(source_id)),
    )
    _renumber_prints_chronologically(tx, int(target_id))

    tx.execute(
        "UPDATE voucher_events SET voucher_id=? WHERE voucher_id=?",
        (int(target_id), int(source_id)),
    )
    tx.execute(
        "UPDATE legacy_audit_events SET voucher_id=? WHERE voucher_id=?",
        (int(target_id), int(source_id)),
    )
    tx.execute(
        "UPDATE voucher_sync_observations SET voucher_id=? WHERE voucher_id=?",
        (int(target_id), int(source_id)),
    )

    legacy_recipient = (
        str(source["assigned_to"] or "").strip()
        or str(source["name"] or "").strip()
    )
    if legacy_recipient:
        tx.execute(
            """UPDATE vouchers
               SET assigned_to=CASE
                   WHEN TRIM(assigned_to)='' THEN ?
                   ELSE assigned_to
               END
               WHERE id=?""",
            (legacy_recipient, int(target_id)),
        )

    tx.execute(
        """INSERT OR IGNORE INTO voucher_events
           (event_uuid, voucher_id, event_type, occurred_at, source,
            windows_user, details_json)
           VALUES (?, ?, 'LEGACY_IDENTITY_CONSOLIDATED', ?, 'MIGRATION',
                   NULL, ?)""",
        (
            _identity_event_uuid("merge", target_id, [source_id]),
            int(target_id),
            str(observed_at),
            database.encode_event_details(
                {
                    "source_voucher_id": int(source_id),
                    "method": "unique_code_same_installation",
                }
            ),
        ),
    )
    tx.execute("DELETE FROM vouchers WHERE id=?", (int(source_id),))


def _consolidate_legacy_identity_for_live_voucher(
    database: Database,
    tx,
    *,
    controller_id: int,
    target_id: int,
    code: str,
    observed_at: str,
) -> None:
    """Consolidate one uniquely matched pre-SQLite placeholder into live state."""

    canonical = _canonical_code(code)
    if not canonical:
        return

    live_matches = tx.execute(
        f"""SELECT v.id
            FROM vouchers AS v
            JOIN controllers AS c ON c.id=v.controller_id
            WHERE REPLACE(v.code, '-', '')=?
              AND c.api_root NOT LIKE ?
            ORDER BY v.id""",
        (canonical, f"{LEGACY_BACKUP_API_ROOT_PREFIX}%"),
    ).fetchall()
    live_ids = [int(row["id"]) for row in live_matches]
    if live_ids != [int(target_id)]:
        sources = tx.execute(
            f"""SELECT v.id
                FROM vouchers AS v
                JOIN controllers AS c ON c.id=v.controller_id
                WHERE REPLACE(v.code, '-', '')=?
                  AND c.api_root LIKE ?
                  AND v.archived_at IS NULL
                ORDER BY v.id""",
            (canonical, f"{LEGACY_BACKUP_API_ROOT_PREFIX}%"),
        ).fetchall()
        if sources:
            _record_identity_review_required(
                database,
                tx,
                target_id=int(target_id),
                source_ids=[int(row["id"]) for row in sources],
                observed_at=observed_at,
                reason="multiple_live_identities",
            )
        return

    sources = tx.execute(
        f"""SELECT v.id
            FROM vouchers AS v
            JOIN controllers AS c ON c.id=v.controller_id
            WHERE REPLACE(v.code, '-', '')=?
              AND c.api_root LIKE ?
              AND v.archived_at IS NULL
            ORDER BY v.id""",
        (canonical, f"{LEGACY_BACKUP_API_ROOT_PREFIX}%"),
    ).fetchall()
    source_ids = [int(row["id"]) for row in sources]
    if not source_ids:
        return
    if len(source_ids) != 1:
        _record_identity_review_required(
            database,
            tx,
            target_id=int(target_id),
            source_ids=source_ids,
            observed_at=observed_at,
            reason="multiple_legacy_identities",
        )
        return

    _merge_legacy_archive_voucher(
        database,
        tx,
        target_id=int(target_id),
        source_id=source_ids[0],
        observed_at=observed_at,
    )


def persist_successful_snapshot(
    database: Database,
    *,
    controller_id: int,
    vouchers: list[ApiVoucher],
    observed_at: str,
    sync_uuid: str | None = None,
    application_created_ids: list[str] | tuple[str, ...] = (),
    application_created_is_nominal: bool | None = None,
) -> str:
    """Persist one complete successful UniFi voucher-list snapshot.

    The function records only meaningful state changes. It does not infer an
    exact use time from an incrementing authorizedGuestCount; observed_at means
    only that Voucher Management noticed the new counter at that synchronization.
    """

    run_uuid = sync_uuid or str(uuid4())
    db = database.connection

    # Read the previous state before writes so observations describe actual
    # transitions rather than the just-updated row.
    previous = {
        str(row["unifi_id"]): dict(row)
        for row in db.execute(
            "SELECT * FROM vouchers WHERE controller_id=?",
            (controller_id,),
        )
    }

    changes: list[tuple[int, str, object, object]] = []
    seen_remote_ids: set[str] = set()
    live_voucher_ids: dict[str, int] = {}

    with database.transaction() as tx:
        tx.execute(
            """INSERT INTO sync_runs
               (sync_uuid, controller_id, started_at, completed_at, status,
                vouchers_received, changes_detected)
               VALUES (?, ?, ?, ?, 'SUCCESS', ?, 0)""",
            (run_uuid, controller_id, observed_at, observed_at, len(vouchers)),
        )

        for voucher in vouchers:
            seen_remote_ids.add(voucher.id)
            old = previous.get(voucher.id)
            voucher_id = database.upsert_voucher(
                controller_id=controller_id,
                unifi_id=voucher.id,
                code=voucher.code,
                name=voucher.recipient,
                created_at=_iso_from_epoch(voucher.create_time),
                imported_at=observed_at,
                duration_minutes=voucher.duration_minutes,
                authorized_guest_limit=voucher.quota or None,
                authorized_guest_count=voucher.used,
                activated_at=_iso_from_epoch(voucher.start_time),
                expires_at=_iso_from_epoch(voucher.end_time),
                expired=voucher.status == "EXPIRED",
                data_limit_mb=voucher.data_mb,
                download_limit_kbps=voucher.down_kbps,
                upload_limit_kbps=voucher.up_kbps,
                last_synced_at=observed_at,
                connection=tx,
            )
            live_voucher_ids[str(voucher.id)] = int(voucher_id)
            if old is None:
                continue

            current = {
                "authorized_guest_count": voucher.used,
                "activated_at": _iso_from_epoch(voucher.start_time),
                "expires_at": _iso_from_epoch(voucher.end_time),
                "expired": int(voucher.status == "EXPIRED"),
                "present_on_controller": 1,
            }
            for field in OBSERVED_FIELDS:
                old_value = old[field]
                new_value = current[field]
                if old_value != new_value:
                    changes.append((voucher_id, field, old_value, new_value))

        for voucher in vouchers:
            target_id = live_voucher_ids.get(str(voucher.id))
            if target_id is None:
                raise RuntimeError("Live voucher identity missing after upsert")
            _consolidate_legacy_identity_for_live_voucher(
                database,
                tx,
                controller_id=int(controller_id),
                target_id=target_id,
                code=voucher.code,
                observed_at=observed_at,
            )

        if application_created_ids:
            if application_created_is_nominal is None:
                raise ValueError(
                    "application-created classification requires nominality"
                )
            database.mark_application_created_vouchers(
                controller_id=controller_id,
                unifi_ids=application_created_ids,
                is_nominal=application_created_is_nominal,
                connection=tx,
            )

        # Absence is meaningful only because this function represents a
        # complete successful list operation.
        for remote_id, old in previous.items():
            if remote_id in seen_remote_ids or not old["present_on_controller"]:
                continue
            tx.execute(
                """UPDATE vouchers
                   SET present_on_controller=0, last_synced_at=?
                   WHERE id=?""",
                (observed_at, old["id"]),
            )
            changes.append((old["id"], "present_on_controller", 1, 0))

        for voucher_id, field, old_value, new_value in changes:
            tx.execute(
                """INSERT INTO voucher_sync_observations
                   (voucher_id, observed_at, field_name, previous_value,
                    new_value, sync_uuid)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    voucher_id,
                    observed_at,
                    field,
                    None if old_value is None else str(old_value),
                    None if new_value is None else str(new_value),
                    run_uuid,
                ),
            )

        tx.execute(
            """UPDATE sync_runs SET changes_detected=?
               WHERE sync_uuid=?""",
            (len(changes), run_uuid),
        )
        tx.execute(
            """UPDATE controllers
               SET last_successful_sync_at=? WHERE id=?""",
            (observed_at, controller_id),
        )

    return run_uuid


def persist_connection_snapshot_to_path(
    database_path: Path,
    *,
    api_root: str,
    cert_sha256: str,
    requested_name: str,
    site_name: str,
    vouchers: list[ApiVoucher],
    observed_at: str,
) -> PersistedControllerSnapshot:
    """Persist connection identity/snapshot on a worker-owned SQLite handle."""

    database = Database(Path(database_path))
    try:
        database.initialize()
        existing_id = database.find_controller_by_api_root(api_root)
        persisted_name = str(requested_name or "").strip()
        if not persisted_name and existing_id is not None:
            persisted_name = database.controller_name(existing_id) or ""
        if not persisted_name:
            persisted_name = str(site_name or "").strip() or "Controller UniFi"

        controller_id = database.get_or_create_controller(
            name=persisted_name,
            api_root=api_root,
            observed_at=observed_at,
            cert_sha256=cert_sha256,
        )
        persist_successful_snapshot(
            database,
            controller_id=controller_id,
            vouchers=list(vouchers),
            observed_at=observed_at,
        )
        return PersistedControllerSnapshot(
            controller_id=controller_id,
            controller_name=persisted_name,
            observed_at=observed_at,
        )
    finally:
        database.close()


def persist_refresh_snapshot_to_path(
    database_path: Path,
    *,
    controller_id: int,
    vouchers: list[ApiVoucher],
    observed_at: str,
) -> str:
    """Persist one refresh using a worker-owned SQLite connection."""

    database = Database(Path(database_path))
    try:
        database.initialize()
        return persist_successful_snapshot(
            database,
            controller_id=int(controller_id),
            vouchers=list(vouchers),
            observed_at=observed_at,
        )
    finally:
        database.close()


def persist_create_result_to_path(
    database_path: Path,
    *,
    controller_id: int,
    snapshot: list[ApiVoucher],
    created: list[ApiVoucher],
    snapshot_complete: bool,
    snapshot_observed: bool,
    is_nominal: bool,
    observed_at: str,
) -> None:
    """Persist a create result without inventing controller facts.

    A successful follow-up GET is a complete controller snapshot and can use the
    normal synchronization path. If creation succeeded but that GET failed,
    only the vouchers returned by the successful POST are upserted; absence of
    any other voucher is deliberately not inferred.
    """

    database = Database(Path(database_path))
    try:
        database.initialize()
        created_ids = [voucher.id for voucher in created]
        if snapshot_complete:
            persist_successful_snapshot(
                database,
                controller_id=int(controller_id),
                vouchers=list(snapshot),
                observed_at=observed_at,
                application_created_ids=created_ids,
                application_created_is_nominal=bool(is_nominal),
            )
        elif created:
            # A stale-but-successful GET is useful positive evidence for rows it
            # returned, but must not authoritatively mark omitted rows absent.
            # A failed GET contributes no fresh rows beyond the confirmed POST.
            partial_rows = list(snapshot) if snapshot_observed else list(created)
            by_id = {voucher.id: voucher for voucher in partial_rows}
            for voucher in created:
                by_id[voucher.id] = voucher
            with database.transaction() as tx:
                for voucher in by_id.values():
                    database.upsert_voucher(
                        controller_id=int(controller_id),
                        unifi_id=voucher.id,
                        code=voucher.code,
                        name=voucher.recipient,
                        created_at=_iso_from_epoch(voucher.create_time),
                        imported_at=observed_at,
                        duration_minutes=voucher.duration_minutes,
                        authorized_guest_limit=voucher.quota or None,
                        authorized_guest_count=voucher.used,
                        activated_at=_iso_from_epoch(voucher.start_time),
                        expires_at=_iso_from_epoch(voucher.end_time),
                        expired=voucher.status == "EXPIRED",
                        data_limit_mb=voucher.data_mb,
                        download_limit_kbps=voucher.down_kbps,
                        upload_limit_kbps=voucher.up_kbps,
                        last_synced_at=observed_at,
                        connection=tx,
                    )
                database.mark_application_created_vouchers(
                    controller_id=int(controller_id),
                    unifi_ids=created_ids,
                    is_nominal=bool(is_nominal),
                    connection=tx,
                )
    finally:
        database.close()


def _epoch_from_iso(value: str | None) -> int:
    """Convert persisted UTC text back to the ApiVoucher compatibility shape."""

    if not value:
        return 0
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def load_local_vouchers(database: Database, *, controller_id: int) -> list[ApiVoucher]:
    """Load the last durable voucher state without contacting UniFi.

    This is a cache/history view, not a fresh controller assertion. Rows that
    disappeared from UniFi remain available because local history is permanent.
    """

    rows = database.connection.execute(
        """SELECT * FROM vouchers
           WHERE controller_id=? AND archived_at IS NULL
           ORDER BY COALESCE(created_at, imported_at) DESC, id DESC""",
        (controller_id,),
    ).fetchall()
    vouchers: list[ApiVoucher] = []
    for row in rows:
        if row["expired"]:
            status = "EXPIRED"
        elif row["authorized_guest_count"] > 0:
            status = "USED_MULTIPLE"
        else:
            status = "VALID_MULTI"
        vouchers.append(
            ApiVoucher(
                id=str(row["unifi_id"]),
                code=str(row["code"]),
                recipient=str(row["name"]),
                duration_minutes=int(row["duration_minutes"] or 0),
                create_time=_epoch_from_iso(row["created_at"]),
                quota=int(row["authorized_guest_limit"] or 0),
                used=int(row["authorized_guest_count"]),
                status=status,
                start_time=_epoch_from_iso(row["activated_at"]),
                end_time=_epoch_from_iso(row["expires_at"]),
                data_mb=row["data_limit_mb"],
                down_kbps=row["download_limit_kbps"],
                up_kbps=row["upload_limit_kbps"],
            )
        )
    return vouchers
