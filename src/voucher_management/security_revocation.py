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
    params: list[object] = [cutoff]
    controller_clause = ""
    if controller_id is not None:
        controller_clause = "AND v.controller_id=?"
        params.append(int(controller_id))

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
                    AND ve.event_type='SECURITY_REVOKED'
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
                    Database.encode_event_details(
                        {
                            "reason": "printed_unused_threshold",
                            "credential_preserved_locally": True,
                        }
                    ),
                ),
            )
            recorded.append(voucher_id)

    return tuple(recorded)
