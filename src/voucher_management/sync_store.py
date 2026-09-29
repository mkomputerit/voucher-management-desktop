"""Controller-to-SQLite synchronization for Voucher Management 5.0.

This module deliberately contains no Tk code. It converts a successful UniFi
snapshot into durable current state and change observations. Missing vouchers
are marked absent only after the caller has obtained a complete successful
snapshot; network failures must never call this function with partial data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .database import Database
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


def _upsert_api_voucher(
    database: Database,
    *,
    controller_id: int,
    voucher: ApiVoucher,
    observed_at: str,
    connection,
) -> int:
    """Persist one API voucher without changing application-owned metadata."""

    return database.upsert_voucher(
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
        connection=connection,
    )


def persist_successful_snapshot(
    database: Database,
    *,
    controller_id: int,
    vouchers: list[ApiVoucher],
    observed_at: str,
    sync_uuid: str | None = None,
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
            voucher_id = _upsert_api_voucher(
                database,
                controller_id=controller_id,
                voucher=voucher,
                observed_at=observed_at,
                connection=tx,
            )
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


def persist_creation_result_to_path(
    database_path: Path,
    *,
    controller_id: int,
    vouchers: list[ApiVoucher],
    created: list[ApiVoucher],
    observed_at: str,
    is_nominal: bool,
    snapshot_complete: bool,
) -> None:
    """Persist a create result without inventing controller or reporting facts.

    A successful follow-up list is a complete controller snapshot and is stored
    through the normal synchronization path. If that GET fails after a
    definitive POST response, only the returned created vouchers are stored;
    absence of other vouchers is never inferred from a partial view.

    Nominal classification and "created by this application" are attached only
    to vouchers returned by a definitive create response. An uncertain POST
    therefore never classifies controller rows by guesswork.
    """

    database = Database(Path(database_path))
    try:
        database.initialize()
        if snapshot_complete:
            persist_successful_snapshot(
                database,
                controller_id=int(controller_id),
                vouchers=list(vouchers),
                observed_at=observed_at,
            )
        elif created:
            with database.transaction() as tx:
                for voucher in created:
                    _upsert_api_voucher(
                        database,
                        controller_id=int(controller_id),
                        voucher=voucher,
                        observed_at=observed_at,
                        connection=tx,
                    )

        if created:
            database.mark_vouchers_created_by_app(
                controller_id=int(controller_id),
                unifi_ids=[voucher.id for voucher in created],
                is_nominal=bool(is_nominal),
                classified_at=observed_at,
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
