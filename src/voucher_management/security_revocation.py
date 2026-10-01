"""Security revocation workflow for printed vouchers left unused.

Revocation is deliberately separate from local privacy minimization.  A voucher
may be removed from UniFi for security while its complete local audit record,
including the voucher code, remains available for historical reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .database import Database

SECURITY_REVOKE_DAYS_KEY = "security_revoke_printed_unused_days"


@dataclass(frozen=True)
class SecurityRevocationCandidate:
    voucher_id: int
    controller_id: int
    controller_name: str
    unifi_id: str
    code: str
    recipient: str
    last_printed_at: str
    last_seen_at: str
    last_synced_at: str


@dataclass(frozen=True)
class SecurityRevocationBatchResult:
    """Outcome of a fresh-read security revocation batch."""

    revoked_ids: tuple[int, ...]
    skipped_ids: tuple[int, ...]
    failed_ids: tuple[int, ...]
    local_persistence_failed_ids: tuple[int, ...]


def _normalize_now(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def security_revoke_days(database: Database) -> int | None:
    row = database.connection.execute(
        "SELECT value FROM settings WHERE key=?",
        (SECURITY_REVOKE_DAYS_KEY,),
    ).fetchone()
    if row is None:
        return None
    try:
        days = int(str(row["value"]))
    except (TypeError, ValueError):
        return None
    return days if 1 <= days <= 3650 else None


def set_security_revoke_days(
    database: Database,
    *,
    days: int,
    now: str,
) -> int:
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("security revoke days must be between 1 and 3650")
    stamp = _normalize_now(now).isoformat()
    with database.transaction() as db:
        db.execute(
            """INSERT INTO settings(key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE
               SET value=excluded.value, updated_at=excluded.updated_at""",
            (SECURITY_REVOKE_DAYS_KEY, str(days), stamp),
        )
    return days


def security_revocation_candidates(
    database: Database,
    *,
    now: str,
    controller_id: int | None = None,
) -> tuple[SecurityRevocationCandidate, ...]:
    """Return printed vouchers eligible for a fresh live revocation check.

    Candidate selection is intentionally conservative:
    - the operator must configure a threshold explicitly;
    - the voucher is still present on UniFi and not reported expired;
    - usage was observed and has never been positive;
    - a controller observation exists *after* the most recent print;
    - the most recent physical print is older than the configured threshold;
    - no previous security-revocation event exists.

    The caller must still perform a fresh controller read immediately before
    deletion and refuse revocation if the live voucher is missing, expired, or
    reports any authorized guest.
    """

    days = security_revoke_days(database)
    if days is None:
        return ()

    cutoff = (_normalize_now(now) - timedelta(days=days)).isoformat()
    params: list[object] = []
    controller_clause = ""
    if controller_id is not None:
        controller_clause = "AND v.controller_id=?"
        params.append(int(controller_id))
    params.append(cutoff)

    rows = database.connection.execute(
        f"""SELECT
                v.id AS voucher_id,
                v.controller_id,
                c.name AS controller_name,
                v.unifi_id,
                v.code,
                v.name,
                MAX(vp.printed_at) AS last_printed_at,
                v.last_seen_at,
                v.last_synced_at
            FROM vouchers AS v
            JOIN controllers AS c ON c.id=v.controller_id
            JOIN voucher_prints AS vp ON vp.voucher_id=v.id
            WHERE v.archived_at IS NULL
              AND v.present_on_controller=1
              AND v.expired=0
              AND v.usage_observed=1
              AND v.ever_used=0
              AND v.authorized_guest_count=0
              AND NOT EXISTS (
                  SELECT 1
                  FROM voucher_events AS ve
                  WHERE ve.voucher_id=v.id
                    AND ve.event_type IN (
                        'SECURITY_REVOKE_REQUESTED',
                        'SECURITY_REVOKED'
                    )
              )
              {controller_clause}
            GROUP BY
                v.id, v.controller_id, c.name, v.unifi_id, v.code, v.name,
                v.last_seen_at, v.last_synced_at
            HAVING MAX(vp.printed_at) <= ?
               AND v.last_seen_at IS NOT NULL
               AND julianday(v.last_seen_at) > julianday(MAX(vp.printed_at))
            ORDER BY MAX(vp.printed_at) ASC, v.id ASC""",
        tuple(params),
    ).fetchall()

    return tuple(
        SecurityRevocationCandidate(
            voucher_id=int(row["voucher_id"]),
            controller_id=int(row["controller_id"]),
            controller_name=str(row["controller_name"] or "Controller"),
            unifi_id=str(row["unifi_id"]),
            code=str(row["code"]),
            recipient=str(row["name"] or ""),
            last_printed_at=str(row["last_printed_at"] or ""),
            last_seen_at=str(row["last_seen_at"] or ""),
            last_synced_at=str(row["last_synced_at"] or ""),
        )
        for row in rows
    )


def live_security_revocation_allowed(
    candidate: SecurityRevocationCandidate,
    live_voucher,
) -> bool:
    """Return whether a fresh UniFi read still permits destructive revocation.

    The controller read is authoritative at the mutation boundary.  Historical
    SQLite eligibility only decides which rows are worth checking; it never
    authorizes DELETE by itself.
    """

    if str(getattr(live_voucher, "id", "") or "").strip() != candidate.unifi_id:
        return False
    if str(getattr(live_voucher, "status", "") or "").upper() == "EXPIRED":
        return False
    try:
        used = int(getattr(live_voucher, "used", 0) or 0)
    except (TypeError, ValueError):
        return False
    return used == 0


def record_security_revocation_request(
    database: Database,
    *,
    voucher_id: int,
    requested_at: str,
    windows_user: str,
) -> bool:
    """Persist a durable intent marker immediately before the remote DELETE.

    If the process dies after UniFi receives DELETE but before the local
    confirmation commit, this marker prevents the mutation from becoming an
    invisible historical gap.  A subsequent reconciliation can inspect the
    pending request rather than blindly replaying DELETE.
    """

    stamp = _normalize_now(requested_at).isoformat()
    operator = str(windows_user or "").strip()
    if not operator:
        raise ValueError("windows user is required")

    with database.transaction() as db:
        row = db.execute(
            """SELECT id FROM vouchers
               WHERE id=? AND archived_at IS NULL""",
            (int(voucher_id),),
        ).fetchone()
        if row is None:
            return False

        confirmed = db.execute(
            """SELECT 1 FROM voucher_events
               WHERE voucher_id=? AND event_type='SECURITY_REVOKED'
               LIMIT 1""",
            (int(voucher_id),),
        ).fetchone()
        if confirmed is not None:
            return True

        pending = db.execute(
            """SELECT 1 FROM voucher_events
               WHERE voucher_id=? AND event_type='SECURITY_REVOKE_REQUESTED'
               LIMIT 1""",
            (int(voucher_id),),
        ).fetchone()
        if pending is not None:
            return True

        db.execute(
            """INSERT INTO voucher_events(
                   event_uuid, voucher_id, event_type, occurred_at,
                   source, windows_user, details_json
               ) VALUES (?, ?, 'SECURITY_REVOKE_REQUESTED', ?, 'OPERATOR', ?, ?)""",
            (
                str(uuid4()),
                int(voucher_id),
                stamp,
                operator,
                Database.encode_event_details(
                    {
                        "reason": "printed_unused_threshold",
                        "remote_delete_confirmed": False,
                    }
                ),
            ),
        )
    return True


def pending_security_revocation_ids(
    database: Database,
    *,
    controller_id: int | None = None,
) -> tuple[int, ...]:
    """Return durable requests that do not yet have a confirmed revocation."""

    params: list[object] = []
    controller_clause = ""
    if controller_id is not None:
        controller_clause = "AND v.controller_id=?"
        params.append(int(controller_id))
    rows = database.connection.execute(
        f"""SELECT DISTINCT v.id
            FROM vouchers AS v
            JOIN voucher_events AS req
              ON req.voucher_id=v.id
             AND req.event_type='SECURITY_REVOKE_REQUESTED'
            WHERE NOT EXISTS (
                SELECT 1 FROM voucher_events AS done
                WHERE done.voucher_id=v.id
                  AND done.event_type='SECURITY_REVOKED'
            )
            {controller_clause}
            ORDER BY v.id""",
        tuple(params),
    ).fetchall()
    return tuple(int(row["id"]) for row in rows)


def revoke_security_candidates_live(
    database: Database,
    *,
    client,
    candidates: list[SecurityRevocationCandidate] | tuple[SecurityRevocationCandidate, ...],
    revoked_at: str,
    windows_user: str,
) -> SecurityRevocationBatchResult:
    """Fresh-read, revoke and audit candidates one by one.

    Every candidate is read by UUID immediately before mutation.  A durable
    SECURITY_REVOKE_REQUESTED event is committed before DELETE.  Known
    ineligible rows are skipped; read/delete failures are isolated per row.
    If the remote DELETE succeeds but the confirmation commit fails, the row is
    reported separately and the durable request marker remains for recovery.
    """

    revoked: list[int] = []
    skipped: list[int] = []
    failed: list[int] = []
    persistence_failed: list[int] = []

    for candidate in candidates:
        try:
            live = client.get_voucher(candidate.unifi_id)
        except Exception:
            failed.append(candidate.voucher_id)
            continue

        if not live_security_revocation_allowed(candidate, live):
            skipped.append(candidate.voucher_id)
            continue

        try:
            requested = record_security_revocation_request(
                database,
                voucher_id=candidate.voucher_id,
                requested_at=revoked_at,
                windows_user=windows_user,
            )
        except Exception:
            persistence_failed.append(candidate.voucher_id)
            continue
        if not requested:
            skipped.append(candidate.voucher_id)
            continue

        try:
            client.delete_vouchers([candidate.unifi_id])
        except Exception:
            # Keep SECURITY_REVOKE_REQUESTED deliberately: the transport may
            # have failed after the mutation boundary and replaying DELETE
            # automatically would be unsafe.
            failed.append(candidate.voucher_id)
            continue

        try:
            record_security_revocations(
                database,
                voucher_ids=[candidate.voucher_id],
                revoked_at=revoked_at,
                windows_user=windows_user,
            )
        except Exception:
            persistence_failed.append(candidate.voucher_id)
            continue
        revoked.append(candidate.voucher_id)

    return SecurityRevocationBatchResult(
        revoked_ids=tuple(revoked),
        skipped_ids=tuple(skipped),
        failed_ids=tuple(failed),
        local_persistence_failed_ids=tuple(persistence_failed),
    )


def reconcile_pending_security_revocations(
    database: Database,
    *,
    controller_id: int,
    live_voucher_ids: set[str] | frozenset[str],
    observed_at: str,
    windows_user: str,
) -> tuple[int, ...]:
    """Confirm pending revocations that are absent from a fresh full snapshot.

    This function must only be called after a successful, complete UniFi voucher
    list operation.  Absence from that snapshot confirms that the credential no
    longer exists remotely; pending requests that are still present remain
    blocked and are never replayed automatically.
    """

    live_ids = {str(value).strip() for value in live_voucher_ids if str(value).strip()}
    pending = database.connection.execute(
        """SELECT DISTINCT v.id, v.unifi_id
           FROM vouchers AS v
           JOIN voucher_events AS req
             ON req.voucher_id=v.id
            AND req.event_type='SECURITY_REVOKE_REQUESTED'
           WHERE v.controller_id=?
             AND NOT EXISTS (
                 SELECT 1 FROM voucher_events AS done
                 WHERE done.voucher_id=v.id
                   AND done.event_type='SECURITY_REVOKED'
             )
           ORDER BY v.id""",
        (int(controller_id),),
    ).fetchall()
    confirmed = [
        int(row["id"])
        for row in pending
        if str(row["unifi_id"] or "").strip() not in live_ids
    ]
    still_present = [
        int(row["id"])
        for row in pending
        if str(row["unifi_id"] or "").strip() in live_ids
    ]

    # A complete successful snapshot that still contains the voucher proves the
    # prior uncertain DELETE did not remove it. Close the pending marker as a
    # non-applied attempt so a future fresh-read revocation may be tried again.
    if still_present:
        stamp = _normalize_now(observed_at).isoformat()
        operator = str(windows_user or "").strip()
        if not operator:
            raise ValueError("windows user is required")
        placeholders = ",".join("?" for _ in still_present)
        with database.transaction() as db:
            rows = db.execute(
                f"""SELECT id, voucher_id
                    FROM voucher_events
                    WHERE voucher_id IN ({placeholders})
                      AND event_type='SECURITY_REVOKE_REQUESTED'""",
                tuple(still_present),
            ).fetchall()
            for row in rows:
                db.execute(
                    """UPDATE voucher_events
                       SET event_type='SECURITY_REVOKE_NOT_APPLIED',
                           occurred_at=?, source='SYSTEM',
                           windows_user=?, details_json=?
                       WHERE id=?""",
                    (
                        stamp,
                        operator,
                        Database.encode_event_details(
                            {
                                "reason": "printed_unused_threshold",
                                "remote_delete_confirmed": False,
                                "fresh_snapshot_confirmed_present": True,
                            }
                        ),
                        int(row["id"]),
                    ),
                )

    if not confirmed:
        return ()
    return record_security_revocations(
        database,
        voucher_ids=confirmed,
        revoked_at=observed_at,
        windows_user=windows_user,
    )


def reconcile_pending_security_revocations_to_path(
    database_path,
    *,
    controller_id: int,
    live_voucher_ids: set[str] | frozenset[str],
    observed_at: str,
    windows_user: str,
) -> tuple[int, ...]:
    """Worker-safe path wrapper for pending revocation reconciliation."""

    database = Database(database_path)
    try:
        database.initialize()
        return reconcile_pending_security_revocations(
            database,
            controller_id=controller_id,
            live_voucher_ids=live_voucher_ids,
            observed_at=observed_at,
            windows_user=windows_user,
        )
    finally:
        database.close()


def record_security_revocations(
    database: Database,
    *,
    voucher_ids: list[int] | tuple[int, ...],
    revoked_at: str,
    windows_user: str,
) -> tuple[int, ...]:
    """Record confirmed UniFi revocations without minimizing local data."""

    stamp = _normalize_now(revoked_at).isoformat()
    operator = str(windows_user or "").strip()
    if not operator:
        raise ValueError("windows user is required")
    requested = tuple(dict.fromkeys(int(value) for value in voucher_ids))
    if not requested:
        return ()

    recorded: list[int] = []
    with database.transaction() as db:
        for voucher_id in requested:
            row = db.execute(
                """SELECT id FROM vouchers
                   WHERE id=? AND archived_at IS NULL""",
                (voucher_id,),
            ).fetchone()
            if row is None:
                continue

            already = db.execute(
                """SELECT 1 FROM voucher_events
                   WHERE voucher_id=? AND event_type='SECURITY_REVOKED'
                   LIMIT 1""",
                (voucher_id,),
            ).fetchone()
            if already is not None:
                recorded.append(voucher_id)
                continue

            # Remote revocation is a lifecycle fact, not a privacy operation.
            # Keep code, recipient, nominality and every local audit field.
            db.execute(
                """UPDATE vouchers
                   SET present_on_controller=0, last_synced_at=?
                   WHERE id=?""",
                (stamp, voucher_id),
            )
            pending = db.execute(
                """SELECT id FROM voucher_events
                   WHERE voucher_id=? AND event_type='SECURITY_REVOKE_REQUESTED'
                   ORDER BY id DESC LIMIT 1""",
                (voucher_id,),
            ).fetchone()
            details = Database.encode_event_details(
                {
                    "reason": "printed_unused_threshold",
                    "remote_delete_confirmed": True,
                    "credential_preserved_locally": True,
                }
            )
            if pending is not None:
                db.execute(
                    """UPDATE voucher_events
                       SET event_type='SECURITY_REVOKED',
                           occurred_at=?, source='OPERATOR',
                           windows_user=?, details_json=?
                       WHERE id=?""",
                    (stamp, operator, details, int(pending["id"])),
                )
            else:
                db.execute(
                    """INSERT INTO voucher_events(
                           event_uuid, voucher_id, event_type, occurred_at,
                           source, windows_user, details_json
                       ) VALUES (?, ?, 'SECURITY_REVOKED', ?, 'OPERATOR', ?, ?)""",
                    (
                        str(uuid4()),
                        voucher_id,
                        stamp,
                        operator,
                        details,
                    ),
                )
            recorded.append(voucher_id)

    return tuple(recorded)
