"""Controller-to-SQLite synchronization for Voucher Management 5.0.

This module deliberately contains no Tk code. It converts a successful UniFi
snapshot into durable current state and change observations. Missing vouchers
are marked absent only after the caller has obtained a complete successful
snapshot; network failures must never call this function with partial data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from .database import Database
from .unifi_api import ApiVoucher


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
