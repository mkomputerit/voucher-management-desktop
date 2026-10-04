"""Operational voucher alerts derived from the durable local history.

This module is deliberately separate from privacy retention and from destructive
security revocation.  It identifies vouchers that are still live on UniFi,
positively aligned as NOT_PRINTED, and older than the operator-selected
preparation threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .database import Database, PRINT_STATE_NOT_PRINTED


UNPRINTED_WARNING_DAYS_KEY = "operational_unprinted_warning_days"


@dataclass(frozen=True)
class UnprintedWarningCandidate:
    voucher_id: int
    controller_id: int
    controller_name: str
    unifi_id: str
    recipient: str
    created_at: str
    last_seen_at: str
    last_synced_at: str
    origin: str


def _normalize_now(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("now is required")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def unprinted_warning_days(database: Database) -> int | None:
    row = database.connection.execute(
        "SELECT value FROM settings WHERE key=?",
        (UNPRINTED_WARNING_DAYS_KEY,),
    ).fetchone()
    if row is None:
        return None
    try:
        days = int(str(row["value"]))
    except (TypeError, ValueError):
        return None
    return days if 1 <= days <= 3650 else None


def set_unprinted_warning_days(
    database: Database,
    *,
    days: int,
    now: str,
) -> int:
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("unprinted warning days must be between 1 and 3650")
    stamp = _normalize_now(now).isoformat()
    with database.transaction() as db:
        db.execute(
            """INSERT INTO settings(key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value,
                   updated_at=excluded.updated_at""",
            (UNPRINTED_WARNING_DAYS_KEY, str(days), stamp),
        )
    return days


def unprinted_warning_candidates(
    database: Database,
    *,
    now: str,
    controller_id: int | None = None,
) -> tuple[UnprintedWarningCandidate, ...]:
    """Return old live vouchers positively known to have never been printed.

    The function is alert-only.  It never authorizes or performs deletion.
    Unknown/not-yet-aligned print history is excluded rather than guessed.
    """

    days = unprinted_warning_days(database)
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
                v.name,
                v.created_at,
                v.last_seen_at,
                v.last_synced_at,
                v.origin
            FROM vouchers AS v
            JOIN controllers AS c ON c.id=v.controller_id
            WHERE v.archived_at IS NULL
              AND v.present_on_controller=1
              AND v.expired=0
              AND v.usage_observed=1
              AND v.ever_used=0
              AND v.authorized_guest_count=0
              AND v.alignment_completed_at IS NOT NULL
              AND v.print_state=?
              AND v.created_at IS NOT NULL
              AND julianday(v.created_at) <= julianday(?)
              AND NOT EXISTS (
                  SELECT 1
                  FROM voucher_prints AS vp
                  WHERE vp.voucher_id=v.id
              )
              {controller_clause}
            ORDER BY julianday(v.created_at) ASC, v.id ASC""",
        (PRINT_STATE_NOT_PRINTED, *params),
    ).fetchall()

    return tuple(
        UnprintedWarningCandidate(
            voucher_id=int(row["voucher_id"]),
            controller_id=int(row["controller_id"]),
            controller_name=str(row["controller_name"] or "Controller"),
            unifi_id=str(row["unifi_id"]),
            recipient=str(row["name"] or "").strip(),
            created_at=str(row["created_at"] or ""),
            last_seen_at=str(row["last_seen_at"] or ""),
            last_synced_at=str(row["last_synced_at"] or ""),
            origin=str(row["origin"] or "UNKNOWN"),
        )
        for row in rows
    )


__all__ = [
    "UNPRINTED_WARNING_DAYS_KEY",
    "UnprintedWarningCandidate",
    "unprinted_warning_days",
    "set_unprinted_warning_days",
    "unprinted_warning_candidates",
]
